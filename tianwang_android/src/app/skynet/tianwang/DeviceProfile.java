package app.skynet.tianwang;

import android.content.Context;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.util.Locale;

/**
 * 天网 · 设备能力档案（一次性检测，长期有效）。
 *
 * 为什么要有这个类（主人 2026-10-23 的原话）：「检测完手机后就是默认你这个手机，
 * 难道手机还能一直变吗？根本不可能变吧，电脑都不可能一直变。」
 * ⇒ 硬件不会变，所以检测**只跑一次**，结果落盘当默认配置用，之后不再打扰人。
 *
 * 铁律：**只认实测值，不按机型名猜。**
 * 教训（2026-10-23）：我曾把 K80 至尊版写成 Adreno，实际是天玑 9400+ /
 * Immortalis-G925——按机型名推结论必错。所以这里只有三个输入：
 *   1. Vulkan 设备清单（SdBridge.listDevices()，sd.cpp 自己枚举的，不猜）
 *   2. MemTotal（/proc/meminfo 实测）
 *   3. 基准实测 s/步（真跑一张小图量出来的）
 *
 * 结果写 filesDir/device_profile.json：既能当配置读，也能 adb pull 出来
 * 事后裁定"当时到底测到了什么"。
 */
public final class DeviceProfile {

    private static final String TAG = "fleet-dev";
    private static final String FILE = "device_profile.json";

    // ---- 实测到的硬件 ----
    public boolean hasVulkan;
    public String vulkanDevice = "";    // 例如 "Vulkan0"
    public String vulkanDesc = "";      // 例如 "Mali-G925-Immortalis MC12"
    public long memTotalMb;
    public int cpuCores;

    // ---- 基准实测 ----
    public boolean benched;
    public float secPerStep;      // 去噪每步秒数
    public float vaeSec;          // VAE 解码秒数（实测：512² SD1.5 曾吃掉 134s）
    public int benchSize;
    public int benchSteps;
    public String benchModel = "";
    public String backend = "vulkan";

    // ---- 档案元信息 ----
    public long detectAtMs;
    public String appVersion = "";

    /** 质量档位。 */
    public enum Tier { FAST, STD, FINE }

    /** 一次出图的完整参数——档位算出来的结果，UI 直接拿去用。 */
    public static final class Plan {
        public int size;
        public int steps;
        public boolean vaeTiling;
        public String backend;
        public float estSec;
        public String reason;    // 依据，必须显示在 UI 上（不许让人盲信自动选择）

        @Override public String toString() {
            return String.format(Locale.US, "%dx%d · %d步 · tiling=%s · %s · 预估 %.0fs",
                    size, size, steps, vaeTiling ? "开" : "关", backend, estSec);
        }
    }

    // ---------------------------------------------------------------- 检测

    /** 只读硬件，不跑推理：快（<100ms），可随时调。 */
    public static DeviceProfile probe(Context ctx) {
        DeviceProfile p = new DeviceProfile();
        p.cpuCores = Runtime.getRuntime().availableProcessors();
        p.memTotalMb = readMemTotalMb();

        String list = SdBridge.listDevices();
        Log.i(TAG, "设备清单原文：\n" + list);
        if (list != null && !list.trim().isEmpty()) {
            String[] lines = list.split("\n");
            for (String ln : lines) {
                String[] kv = ln.split("\t");
                String name = kv.length > 0 ? kv[0].trim() : "";
                String desc = kv.length > 1 ? kv[1].trim() : "";
                if (name.toLowerCase(Locale.US).startsWith("vulkan")) {
                    p.hasVulkan = true;
                    p.vulkanDevice = name;
                    p.vulkanDesc = desc;
                    break;   // 记第一个 Vulkan 设备：多 GPU 手机少见，多了也用不上
                }
            }
        }
        p.backend = p.hasVulkan ? "vulkan" : "cpu";
        p.detectAtMs = System.currentTimeMillis();
        try {
            p.appVersion = ctx.getPackageManager()
                    .getPackageInfo(ctx.getPackageName(), 0).versionName;
        } catch (Exception ignored) { }
        Log.i(TAG, String.format(Locale.US,
                "探测：vulkan=%s(%s %s) mem=%dMB cores=%d backend=%s",
                p.hasVulkan, p.vulkanDevice, p.vulkanDesc, p.memTotalMb, p.cpuCores, p.backend));
        return p;
    }

    private static long readMemTotalMb() {
        RandomAccessFile f = null;
        try {
            f = new RandomAccessFile("/proc/meminfo", "r");
            String line = f.readLine();     // MemTotal:       11556580 kB
            if (line == null) return 0;
            String[] parts = line.split("\\s+");
            return Long.parseLong(parts[1]) / 1024L;
        } catch (Exception e) {
            Log.w(TAG, "读 /proc/meminfo 失败：" + e);
            return 0;
        } finally {
            try { if (f != null) f.close(); } catch (Exception ignored) { }
        }
    }

    // ---------------------------------------------------------------- 档位推算

    /**
     * 按实测数据算档位。
     *
     * 尺寸上限只由 MemTotal 定（实测出图 RSS 峰值 2.67GB @512²，1024² 会再翻）：
     *   ≥ 8GB：放开 1024（但 SD1.5/Turbo 原生 512，超了画糊，所以按模型原生尺寸封顶）
     *   4–8GB：768
     *   < 4GB：512 + VAE 分块
     * 步数由模型自己给（少步模型 4 步就够，常规模型 20 步起）——不在这里拍脑袋。
     */
    public Plan plan(ModelManager.Spec spec, Tier tier) {
        Plan pl = new Plan();
        pl.backend = backend;

        int cap;
        if (memTotalMb >= 8 * 1024) cap = 1024;
        else if (memTotalMb >= 4 * 1024) cap = 768;
        else cap = 512;
        int want = spec.nativeSize;
        if (tier == Tier.FINE) want = Math.max(spec.nativeSize, 768);
        if (tier == Tier.FAST) want = Math.min(spec.nativeSize, 512);
        pl.size = Math.min(want, Math.min(cap, 1024));

        pl.steps = (tier == Tier.FAST) ? spec.fastSteps
                 : (tier == Tier.FINE) ? spec.fineSteps : spec.stdSteps;

        // VAE 分块只在内存紧张时开：开了慢一截，但能防 OOM。
        pl.vaeTiling = memTotalMb > 0 && memTotalMb < 6 * 1024;

        float s = benched && secPerStep > 0 ? secPerStep : 0f;
        float v = benched && vaeSec > 0 ? vaeSec : 0f;
        pl.estSec = s * pl.steps + v;

        StringBuilder r = new StringBuilder();
        r.append("后端=").append(backend);
        if (hasVulkan) r.append("（").append(vulkanDesc.isEmpty() ? vulkanDevice : vulkanDesc).append("）");
        r.append(" 内存=").append(memTotalMb).append("MB");
        if (benched) {
            r.append(String.format(Locale.US, " 实测 %.1fs/步 + VAE %.0fs", s, v));
        } else {
            r.append(" ⚠️未跑基准，预估不可用");
        }
        pl.reason = r.toString();
        return pl;
    }

    /** 默认档：没跑过基准就给"标准"，不擅自选"快速"（快速画质明显差，得人点头）。 */
    public Tier defaultTier() { return Tier.STD; }

    // ---------------------------------------------------------------- 落盘 / 读回

    public File save(Context ctx) {
        File f = new File(ctx.getFilesDir(), FILE);
        try {
            JSONObject o = new JSONObject();
            o.put("hasVulkan", hasVulkan);
            o.put("vulkanDevice", vulkanDevice);
            o.put("vulkanDesc", vulkanDesc);
            o.put("memTotalMb", memTotalMb);
            o.put("cpuCores", cpuCores);
            o.put("benched", benched);
            o.put("secPerStep", (double) secPerStep);
            o.put("vaeSec", (double) vaeSec);
            o.put("benchSize", benchSize);
            o.put("benchSteps", benchSteps);
            o.put("benchModel", benchModel);
            o.put("backend", backend);
            o.put("detectAtMs", detectAtMs);
            o.put("appVersion", appVersion);
            FileOutputStream os = new FileOutputStream(f);
            os.write(o.toString(2).getBytes("UTF-8"));
            os.close();
            Log.i(TAG, "档案已写入 " + f.getAbsolutePath());
            return f;
        } catch (Exception e) {
            Log.e(TAG, "档案写入失败：" + e);
            return null;
        }
    }

    public static DeviceProfile load(Context ctx) {
        File f = new File(ctx.getFilesDir(), FILE);
        if (!f.isFile()) return null;
        try {
            byte[] b = new byte[(int) f.length()];
            RandomAccessFile raf = new RandomAccessFile(f, "r");
            raf.readFully(b);
            raf.close();
            JSONObject o = new JSONObject(new String(b, "UTF-8"));
            DeviceProfile p = new DeviceProfile();
            p.hasVulkan = o.optBoolean("hasVulkan", false);
            p.vulkanDevice = o.optString("vulkanDevice", "");
            p.vulkanDesc = o.optString("vulkanDesc", "");
            p.memTotalMb = o.optLong("memTotalMb", 0);
            p.cpuCores = o.optInt("cpuCores", 0);
            p.benched = o.optBoolean("benched", false);
            p.secPerStep = (float) o.optDouble("secPerStep", 0);
            p.vaeSec = (float) o.optDouble("vaeSec", 0);
            p.benchSize = o.optInt("benchSize", 0);
            p.benchSteps = o.optInt("benchSteps", 0);
            p.benchModel = o.optString("benchModel", "");
            p.backend = o.optString("backend", "vulkan");
            p.detectAtMs = o.optLong("detectAtMs", 0);
            p.appVersion = o.optString("appVersion", "");
            return p;
        } catch (Exception e) {
            Log.e(TAG, "档案读取失败：" + e);
            return null;
        }
    }

    /** 硬件变了才需要重测（换机/系统大版本）；平时直接读档案。 */
    public boolean matchesHardware(DeviceProfile other) {
        return other != null && other.hasVulkan == hasVulkan
                && other.memTotalMb == memTotalMb && other.cpuCores == cpuCores;
    }

    @Override public String toString() {
        return String.format(Locale.US,
                "vulkan=%s %s | mem=%dMB | cores=%d | bench=%s %.2fs/步 VAE %.0fs",
                hasVulkan, vulkanDesc, memTotalMb, cpuCores, benched, secPerStep, vaeSec);
    }
}
