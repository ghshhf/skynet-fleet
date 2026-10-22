package app.skynet.tianwang;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.DialogInterface;
import android.util.Log;
import android.widget.Toast;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * 天网 · 出图模型的落地管理。
 *
 * 事实链（2026-10-23 核实并实测，写在代码里免得以后猜）：
 *   1. sd.cpp 官方不提供 GGUF 直链（docs 给的是本地转换路径），但社区有现成的。
 *      已实测下通并推上手机的两条：
 *        SD1.5 q8_0     1,763,578,176 字节（1.64 GiB，不是文档说的 1.1GB）
 *        SD2.1 Turbo q8_0 2,322,705,024 字节 —— 少步模型，4~8 步即可，必须 cfg=1.0 + LCM 采样
 *   2. 模型不进 APK：1.6~2.3GB 会原样打进包体且无法按需删除。放 APP 外部私有目录。
 *   3. **不搭自己的镜像**（主人 2026-10-23 明确：不至于），也不做 APP 内下载器
 *      （主人明确不做）。弹窗给确切地址 + PC 命令，人自己下自己推。
 *
 * 多模型共存：目录下有哪个就用哪个，采样方法/cfg/步数**跟模型走**，不是全局默认。
 * 这是硬要求——少步模型沿用 cfg=7.0 + EULER_A 画出来就是糊的。
 */
public final class ModelManager {

    private static final String TAG = "fleet-sd";

    /** 已实测可用的模型档。文件名决定了 sd.cpp 走哪条模型分支，别乱改名。 */
    public static final class Spec {
        public final String file;
        public final String label;
        public final String url;
        /** 采样方法，取值见 SdBridge.SAMPLE_*。 */
        public final int sampler;
        public final float cfg;
        /** 原生分辨率：SD1.5/Turbo 都是 512，超了画糊。 */
        public final int nativeSize;
        /** 三档步数（快/标准/精细）。少步模型天然快，档位差在步数与尺寸。 */
        public final int fastSteps, stdSteps, fineSteps;

        Spec(String file, String label, String url, int sampler, float cfg,
             int nativeSize, int fastSteps, int stdSteps, int fineSteps) {
            this.file = file; this.label = label; this.url = url;
            this.sampler = sampler; this.cfg = cfg; this.nativeSize = nativeSize;
            this.fastSteps = fastSteps; this.stdSteps = stdSteps; this.fineSteps = fineSteps;
        }
    }

    public static final Spec SD15 = new Spec(
            "sd15-q8_0.gguf",
            "SD1.5 q8_0（通用，20 步起）",
            "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF"
                    + "/resolve/main/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
            SdBridge.SAMPLE_EULER_A, 7.0f, 512, 12, 20, 28);

    /** SD-Turbo 是无 CFG 的少步模型：cfg 必须 1.0，采样 LCM，步数 4~8。 */
    public static final Spec SD21_TURBO = new Spec(
            "sd21-turbo-q8_0.gguf",
            "SD2.1 Turbo q8_0（少步，4~8 步）",
            "https://huggingface.co/gpustack/stable-diffusion-v2-1-turbo-GGUF"
                    + "/resolve/main/stable-diffusion-v2-1-turbo-Q8_0.gguf",
            SdBridge.SAMPLE_LCM, 1.0f, 512, 4, 6, 8);

    private static final Spec[] SPECS = { SD15, SD21_TURBO };

    /**
     * TAESD：9.3MB 的蒸馏解码器，替代原 VAE。
     * 实测（2026-10-23）原 VAE 解码 512² 要 **134 秒**，是单张图最大的一块；
     * TAESD 把它压到秒级，代价是解码画质略软。放在模型目录里，有就用。
     */
    public static final String TAESD_FILE = "taesd.safetensors";

    private static final String TAESD_URL =
            "https://huggingface.co/madebyollin/taesd/resolve/main/diffusion_pytorch_model.safetensors";

    /** 存在就返回路径，不存在返回 null（调用方据此决定用不用）。 */
    public static String taesdPath(Context ctx) {
        File f = new File(modelsDir(ctx), TAESD_FILE);
        boolean ok = f.isFile() && f.length() > 1024 * 1024;   // 9.3MB
        Log.i(TAG, "taesd " + (ok ? "可用：" : "没有：") + f.getAbsolutePath());
        return ok ? f.getAbsolutePath() : null;
    }

    /** 小于 256MB 必是残缺文件（完整文件都在 1.6GB 以上）。 */
    private static final long MIN_BYTES = 256L * 1024 * 1024;

    private static final String PREF = "fleet_img";
    private static final String KEY_MODEL = "model_file";

    private ModelManager() {}

    /**
     * 模型目录：外部私有目录优先，内部目录兜底。
     *
     * 为什么必须认外部目录（2026-10-23 实测）：APK 非 debuggable，
     * `adb shell run-as` 报 "package not debuggable"，/data/local/tmp 又不是 APP 路径
     * ⇒ **内部 filesDir 根本推不进去**，只有 /sdcard/Android/data/<pkg>/files 是
     * adb 能写、APP 又能读的。
     * ⚠️ adb 建的 models/ 子目录属 shell（drwxrws---），APP 读不进去，
     *    必须 chmod 777 一次（实测 exists=true canRead=false 就是它）。
     */
    public static File modelsDir(Context ctx) {
        File ext = ctx.getExternalFilesDir(null);
        if (ext != null) {
            File d = new File(ext, "models");
            if (d.exists() && d.canRead()) return d;
        }
        return new File(ctx.getFilesDir(), "models");
    }

    public static File fileOf(Context ctx, Spec s) {
        return new File(modelsDir(ctx), s.file);
    }

    /** 目录下真实存在的模型（未下完的不算）。 */
    public static List<Spec> available(Context ctx) {
        List<Spec> out = new ArrayList<Spec>();
        for (Spec s : SPECS) {
            File f = fileOf(ctx, s);
            if (f.isFile() && f.length() >= MIN_BYTES) out.add(s);
        }
        Log.i(TAG, "可用模型 " + out.size() + " 个，目录=" + modelsDir(ctx));
        return out;
    }

    /**
     * 当前选中的模型。没手工选过时：有 Turbo 就用 Turbo（少步是这台机唯一实用的路），
     * 否则用第一个可用的。选择记在 SharedPreferences，重启后不变。
     */
    public static Spec current(Context ctx) {
        List<Spec> av = available(ctx);
        if (av.isEmpty()) return SD15;   // 一个都没有：返回它好让弹窗给出地址
        String saved = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
                .getString(KEY_MODEL, "");
        for (Spec s : av) {
            if (s.file.equals(saved)) return s;
        }
        for (Spec s : av) {
            if (s == SD21_TURBO) return s;
        }
        return av.get(0);
    }

    public static void choose(Context ctx, Spec s) {
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
                .edit().putString(KEY_MODEL, s.file).apply();
        Log.i(TAG, "选用模型 " + s.file);
    }

    public static boolean isReady(Context ctx) {
        Spec s = current(ctx);
        File f = fileOf(ctx, s);
        boolean ok = f.isFile() && f.length() >= MIN_BYTES;
        Log.i(TAG, "isReady=" + ok + " path=" + f.getAbsolutePath()
                + " len=" + (f.isFile() ? f.length() : -1L));
        return ok;
    }

    public static String modelPath(Context ctx) {
        return fileOf(ctx, current(ctx)).getAbsolutePath();
    }

    /**
     * 缺模型弹窗：给确切地址 + PC 命令，不静默失败。
     * 两个模型都在弹窗里列出，人自己挑（少步模型是提速的主路）。
     */
    public static void showMissingDialog(final Activity act) {
        StringBuilder sb = new StringBuilder();
        sb.append("手机本地出图需要 GGUF 模型。挑一个下到电脑上：\n");
        for (Spec s : SPECS) {
            sb.append("\n· ").append(s.label).append("\n").append(s.url).append("\n");
        }
        sb.append("\n下完推到手机（注意：推完要把目录 chmod 一次，否则 APP 读不到）：\n")
          .append("adb push <名字>.gguf /sdcard/Android/data/app.skynet.tianwang/files/models/\n")
          .append("adb shell chmod 777 /sdcard/Android/data/app.skynet.tianwang/files/models\n")
          .append("\n（不搭镜像、不做 APP 内下载器：主人 2026-10-23 定）");

        new AlertDialog.Builder(act)
                .setTitle("缺出图模型")
                .setMessage(sb.toString())
                .setNeutralButton("复制第一个地址", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        ClipboardManager cm = (ClipboardManager) act
                                .getSystemService(Context.CLIPBOARD_SERVICE);
                        if (cm != null) {
                            cm.setPrimaryClip(ClipData.newPlainText("model url", SPECS[0].url));
                            Toast.makeText(act, "地址已复制", Toast.LENGTH_SHORT).show();
                        }
                        d.dismiss();
                    }
                })
                .setPositiveButton("知道了", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) { d.dismiss(); }
                })
                .setCancelable(true)
                .show();
    }
}
