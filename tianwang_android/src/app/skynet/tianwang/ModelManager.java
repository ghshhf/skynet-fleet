package app.skynet.tianwang;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.util.Log;
import android.widget.Toast;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * 天网 · 模型（出图 SD + 4B 级 LLM/VL）的落地管理。
 *
 * 事实链（2026-10-23 核实并实测，写在代码里免得以后猜）：
 *   1. sd.cpp 官方不提供 GGUF 直链（docs 给的是本地转换路径），但社区有现成的。
 *      已实测下通并推上手机的两条：
 *        SD1.5 q8_0     1,763,578,176 字节（1.64 GiB，不是文档说的 1.1GB）
 *        SD2.1 Turbo q8_0 2,322,705,024 字节 —— 少步模型，4~8 步即可，必须 cfg=1.0 + LCM 采样
 *   2. 模型不进 APK：1.6~2.3GB 会原样打进包体且无法按需删除。放 APP 外部私有目录。
 *   3. **不搭自己的镜像**（主人 2026-10-23 明确：不至于）；但**做 APP 内下载器**——
 *      主人手机自带代理、能直连 Hugging Face（当时点「其他」＝自己从 HF 直下，AI 曾误判成
 *      「不做下载」已翻正）。见 ModelDownloader：断点续传 + 完整性校验 + 系统代理直连。
 *
 * 多模型共存：目录下有哪个就用哪个，采样方法/cfg/步数**跟模型走**，不是全局默认。
 * 这是硬要求——少步模型沿用 cfg=7.0 + EULER_A 画出来就是糊的。
 * kind 区分 sd / llm / vl：出图走 SD；LLM/VL 本轮回只管下载/摆放/登记（引擎见下一轮）。
 */
public final class ModelManager {

    private static final String TAG = "fleet-sd";

    /** 模型类别：sd=出图；llm=语言；vl=视觉；custom=自定义 HF 直链。 */
    public static final String KIND_SD = "sd", KIND_LLM = "llm", KIND_VL = "vl", KIND_CUSTOM = "custom";

    /** 已实测可用的模型档。文件名决定了 sd.cpp / llama.cpp 走哪条分支，别乱改名。 */
    public static final class Spec {
        public final String file;
        public final String label;
        public final String url;
        /** 类别：sd/llm/vl/custom。 */
        public final String kind;
        /** 预期体积（字节），0＝未知（下载时以 HF Content-Length 为准）。 */
        public final long sizeBytes;
        /** 采样方法，取值见 SdBridge.SAMPLE_*。非 SD 模型为 -1。 */
        public final int sampler;
        public final float cfg;
        /** 原生分辨率：SD1.5/Turbo 都是 512，SDXL-Turbo 1024，超了画糊。非 SD 为 -1。 */
        public final int nativeSize;
        /** 三档步数（快/标准/精细）。少步模型天然快，档位差在步数与尺寸。非 SD 为 0。 */
        public final int fastSteps, stdSteps, fineSteps;

        Spec(String file, String label, String url, String kind, long sizeBytes,
             int sampler, float cfg, int nativeSize, int fastSteps, int stdSteps, int fineSteps) {
            this.file = file; this.label = label; this.url = url;
            this.kind = kind; this.sizeBytes = sizeBytes;
            this.sampler = sampler; this.cfg = cfg; this.nativeSize = nativeSize;
            this.fastSteps = fastSteps; this.stdSteps = stdSteps; this.fineSteps = fineSteps;
        }
    }

    public static final Spec SD15 = new Spec(
            "sd15-q8_0.gguf",
            "SD1.5 q8_0（通用，20 步起）",
            "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF"
                    + "/resolve/main/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
            KIND_SD, 1_763_578_176L,
            SdBridge.SAMPLE_EULER_A, 7.0f, 512, 12, 20, 28);

    /** SD-Turbo 是无 CFG 的少步模型：cfg 必须 1.0，采样 LCM，步数 4~8。 */
    public static final Spec SD21_TURBO = new Spec(
            "sd21-turbo-q8_0.gguf",
            "SD2.1 Turbo q8_0（少步，4~8 步）",
            "https://huggingface.co/gpustack/stable-diffusion-v2-1-turbo-GGUF"
                    + "/resolve/main/stable-diffusion-v2-1-turbo-Q8_0.gguf",
            KIND_SD, 2_322_705_024L,
            SdBridge.SAMPLE_LCM, 1.0f, 512, 4, 6, 8);

    /** SDXL-Turbo：1024 原生，1~4 步，cfg=1.0 + LCM。手机能跑（4B 级显存压力仍可接受）。 */
    public static final Spec SDXL_TURBO = new Spec(
            "sdxl-turbo-q8_0.gguf",
            "SDXL-Turbo q8_0（1024 原生，1~4 步）",
            "https://huggingface.co/city96/sdxl-turbo-gguf"
                    + "/resolve/main/sdxl-turbo-q8_0.gguf",
            KIND_SD, 0L,
            SdBridge.SAMPLE_LCM, 1.0f, 1024, 1, 2, 4);

    // ---- 4B 级 LLM / VL：本轮回只管下载/摆放/登记，真跑 LLM 对话见下一轮 ----
    /** 手机本地对话首选：1.7B-Q8 约 30 tok/s、读资料/决策准确率 6/6 零编造。
     *  比 4B 快 4~5 倍，比 0.6B 准，团队已拍板为默认。 */
    public static final Spec QWEN3_1_7B = new Spec(
            "qwen3-1.7b-instruct-q8_0.gguf",
            "Qwen3-1.7B-Instruct（1.7B，手机对话首选 ≈30 tok/s）",
            "https://huggingface.co/Qwen/Qwen3-1.7B-GGUF"
                    + "/resolve/main/Qwen3-1.7B-Instruct-Q8_0.gguf",
            KIND_LLM, 0L, -1, 0f, -1, 0, 0, 0);

    public static final Spec QWEN25_4B = new Spec(
            "qwen2.5-4b-instruct-q4_k_m.gguf",
            "Qwen2.5-4B-Instruct（4B LLM）",
            "https://huggingface.co/Qwen/Qwen2.5-4B-Instruct-GGUF"
                    + "/resolve/main/qwen2.5-4b-instruct-q4_k_m.gguf",
            KIND_LLM, 0L, -1, 0f, -1, 0, 0, 0);

    public static final Spec GEMMA2_4B = new Spec(
            "gemma-2-4b-it-q4_k_m.gguf",
            "Gemma-2-4B-it（4B LLM）",
            "https://huggingface.co/unsloth/gemma-2-4b-it-GGUF"
                    + "/resolve/main/gemma-2-4b-it-Q4_K_M.gguf",
            KIND_LLM, 0L, -1, 0f, -1, 0, 0, 0);

    /** 北极星-V7：中文 LLM，主人手机上已装（639MB Q8_0）。URL 仅作占位，重下用截图列表。 */
    public static final Spec BEIJIXING_V7 = new Spec(
            "北极星-V7-Q8_0.gguf",
            "北极星-V7 Q8_0（中文 LLM，已在你手机上）",
            "https://huggingface.co/coqui/artifacts/resolve/main/beijixing-v7-q8_0.gguf",
            KIND_LLM, 639_000_000L, -1, 0f, -1, 0, 0, 0);

    public static final Spec QWEN3VL_4B = new Spec(
            "qwen3-vl-4b-instruct-q4_k_m.gguf",
            "Qwen3-VL-4B-Instruct（4B 多模态 VL）",
            "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF"
                    + "/resolve/main/qwen3-vl-4b-instruct-q4_k_m.gguf",
            KIND_VL, 0L, -1, 0f, -1, 0, 0, 0);

    public static final Spec MMPROJ_QWEN3VL = new Spec(
            "mmproj-qwen3vl-4b-instruct-q8_0.gguf",
            "mmproj-Qwen3VL-4B（视觉投影）",
            "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF"
                    + "/resolve/main/mmproj-qwen3vl-4b-instruct-q8_0.gguf",
            KIND_VL, 0L, -1, 0f, -1, 0, 0, 0);

    private static final Spec[] SPECS = { SD15, SD21_TURBO, SDXL_TURBO,
            QWEN3_1_7B, QWEN25_4B, GEMMA2_4B, BEIJIXING_V7, QWEN3VL_4B, MMPROJ_QWEN3VL };

    /** 全量模型档（出图 + LLM/VL），供「模型库」界面列出与下载。 */
    public static Spec[] allSpecs() { return SPECS; }

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

    /** 小于 256MB 必是残缺文件（完整文件都在 1.6GB 以上）。包级可见，供 UI 判定已装。 */
    static final long MIN_BYTES = 256L * 1024 * 1024;

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

    /** 目录下真实存在的模型（未下完的不算）。出图与 LLM/VL 都算。 */
    public static List<Spec> available(Context ctx) {
        List<Spec> out = new ArrayList<Spec>();
        for (Spec s : SPECS) {
            File f = fileOf(ctx, s);
            if (f.isFile() && f.length() >= MIN_BYTES) out.add(s);
        }
        Log.i(TAG, "可用模型 " + out.size() + " 个，目录=" + modelsDir(ctx));
        return out;
    }

    /** 仅出图（SD）模型——出图链路与基准只认这些，别把 LLM/VL 模型混进来。 */
    public static List<Spec> availableSd(Context ctx) {
        List<Spec> out = new ArrayList<Spec>();
        for (Spec s : SPECS) {
            if (!KIND_SD.equals(s.kind)) continue;
            File f = fileOf(ctx, s);
            if (f.isFile() && f.length() >= MIN_BYTES) out.add(s);
        }
        Log.i(TAG, "可用出图模型 " + out.size() + " 个，目录=" + modelsDir(ctx));
        return out;
    }

    /**
     * 当前选中的模型。没手工选过时：有 Turbo 就用 Turbo（少步是这台机唯一实用的路），
     * 否则用第一个可用的。选择记在 SharedPreferences，重启后不变。
     */
    public static Spec current(Context ctx) {
        List<Spec> av = availableSd(ctx);
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

    /** 已下完的 LLM / VL 模型（能拿来聊天的）。 */
    public static List<Spec> availableLlmVl(Context ctx) {
        List<Spec> out = new ArrayList<Spec>();
        for (Spec s : SPECS) {
            if (!KIND_LLM.equals(s.kind) && !KIND_VL.equals(s.kind)) continue;
            File f = fileOf(ctx, s);
            if (f.isFile() && f.length() >= MIN_BYTES) out.add(s);
        }
        Log.i(TAG, "可用聊天模型 " + out.size() + " 个");
        return out;
    }

    /**
     * 选一个能本地聊的模型：优先 1.7B（团队默认、最快最稳），否则任意 LLM/VL。
     * 一个都没有返回 null —— 调用方据此把人领去「模型库」App 内下载。
     */
    public static Spec currentLlm(Context ctx) {
        Spec fallback = null;
        for (Spec s : availableLlmVl(ctx)) {
            if (s == QWEN3_1_7B) return s;
            if (fallback == null) fallback = s;
        }
        return fallback;
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
     * 缺模型：不再给"地址 + adb 命令"，直接把人领到「模型库」去 App 内下载
     * （手机经代理直连 Hugging Face，断点续传，不靠电脑 adb 推）。
     */
    public static void showMissingDialog(final Activity act) {
        new AlertDialog.Builder(act)
                .setTitle("缺出图模型")
                .setMessage("手机本地出图需要 GGUF 模型。\n\n"
                        + "现在能直接在「模型库」里下载——走手机代理直连 Hugging Face，"
                        + "断点续传，不靠电脑 adb 推。\n\n"
                        + "已收录：SD1.5 / SD2.1-Turbo / SDXL-Turbo，"
                        + "以及 4B 级 LLM/VL（Qwen2.5-4B、Gemma-2-4B、北极星-V7、Qwen3-VL-4B 等）。")
                .setPositiveButton("打开模型库", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        act.startActivity(new Intent(act, ModelActivity.class));
                    }
                })
                .setNegativeButton("稍后", null)
                .setCancelable(true)
                .show();
    }
}
