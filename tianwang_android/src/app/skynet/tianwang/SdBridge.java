package app.skynet.tianwang;

import android.util.Log;

import java.io.File;

/**
 * 天网 · 手机本地出图（stable-diffusion.cpp + Vulkan，arm64）。
 *
 * 存在的理由：原来「画」这一步只能靠电脑（这台机还没独显）。把推理搬进手机，
 * K80 Ultra 的 Immortalis-G925 + LPDDR5X 干这个比这台 PC 强，且不新增网络出口。
 *
 * 模型不放 assets：q8_0 有一百多 MB 到 1GB+，会被原样打进 APK 且无法按需删除。
 * 放应用私有目录，首次由 ModelManager 卡确认闸后下载。
 *
 * 全部推理方法都是阻塞的且以分钟计，必须在工作线程调用；
 * 进度经 {@link Progress} 回调上来，UI 拿它刷新，别去轮询。
 */
public final class SdBridge {

    private static final String TAG = "fleet-sd";
    private static final int MIN_MODEL_BYTES = 256 * 1024 * 1024;  // 完整模型都在 1.6GB 以上，小于 256MB 必是残缺文件

    // 采样方法：取值必须与 jni/sd_jni.cpp 的 to_sample_method() 一致（那里是白名单映射，
    // 越界回退 EULER_A 并打日志）。改动要两边同步。
    public static final int SAMPLE_EULER      = 0;
    public static final int SAMPLE_EULER_A    = 1;
    public static final int SAMPLE_HEUN       = 2;
    public static final int SAMPLE_DPM2       = 3;
    public static final int SAMPLE_DPMPP2S_A  = 4;
    public static final int SAMPLE_DPMPP2M    = 5;
    public static final int SAMPLE_DPMPP2MV2  = 6;
    public static final int SAMPLE_IPNDM      = 7;
    public static final int SAMPLE_IPNDM_V    = 8;
    public static final int SAMPLE_LCM        = 9;   // SD-Turbo / LCM 专用
    public static final int SAMPLE_DDIM_TRAIL = 10;
    public static final int SAMPLE_TCD        = 11;

    static {
        // 只加载一个 .so：sd.cpp + ggml(+vulkan) 已静态链进来，没有依赖顺序问题。
        System.loadLibrary("fleet_sd");
    }

    /** 推理进度回调。在 generate 的调用线程上同步触发，实现里别做重活。 */
    public interface Progress {
        void onProgress(int step, int steps);
    }

    private long handle;

    /** 模型是否已装进内存（加载过一次且没释放）。 */
    public boolean isReadyLoaded() {
        return handle != 0L;
    }

    // ---- 诊断用 ----
    public String lastError = "";
    public long loadMs;
    public long lastMs;

    /** 底层系统信息原文（含 Vulkan 设备计数）。用来诚实回答「到底用上 GPU 没有」。 */
    public static String systemInfo() {
        return nativeSystemInfo();
    }

    /** 可用后端设备清单，一行一个 `名字\t描述`；名字就是 backend 参数能填的值。 */
    public static String listDevices() {
        return nativeListDevices();
    }

    public static void setProgressListener(Progress p) {
        nativeSetProgressListener(p);
    }

    /**
     * 加载模型。返回 false 时 {@link #lastError} 里有原因。
     * 同一个实例重复 load 会先释放旧的（连点两次不会把内存撑爆）。
     */
    /**
     * backend 传 null/空串 = 用 sd.cpp 默认（CPU）。
     * 传 "vulkan" 走 GPU；底层加载失败会自动退回 CPU 并写日志。
     */
    /**
     * @param taesd TAESD 权重路径（9.3MB 的蒸馏解码器）。传 null/空 = 用原 VAE。
     *              实测原 VAE 解码 512² 要 134 秒，TAESD 把它压到秒级，代价是画质略软。
     */
    public boolean load(String modelPath, int threads, String backend, String taesd) {
        close();
        if (modelPath == null || modelPath.isEmpty()) {
            lastError = "模型路径是空的";
            return false;
        }
        File f = new File(modelPath);
        if (!f.isFile()) {
            lastError = "模型文件不存在：" + modelPath;
            return false;
        }
        if (f.length() < MIN_MODEL_BYTES) {
            lastError = "模型文件太小（" + f.length() + " 字节），多半没下完";
            return false;
        }
        long t0 = System.currentTimeMillis();
        handle = nativeInit(modelPath, threads, false, backend, taesd);
        if (handle == 0L) {
            lastError = "底层加载失败：" + nativeLastError();
            return false;
        }
        loadMs = System.currentTimeMillis() - t0;
        Log.i(TAG, "模型加载耗时 " + loadMs + "ms：" + modelPath);
        return true;
    }

    /**
     * 出一张图，返回 PNG 字节；失败返回 null，原因在 {@link #lastError}。
     *
     * seed < 0 表示随机（换种子重出用）。vaeTiling 只在低内存机型开，
     * 开了会慢一截但不容易 OOM。
     */
    public byte[] generate(String prompt, String neg, int width, int height,
                           int steps, long seed, float cfg, boolean vaeTiling,
                           int sampleMethod, Progress p) {
        if (handle == 0L) {
            lastError = "模型还没加载";
            return null;
        }
        if (prompt == null || prompt.trim().isEmpty()) {
            lastError = "prompt 是空的";
            return null;
        }
        setProgressListener(p);
        long t0 = System.currentTimeMillis();
        byte[] png = nativeGenerate(handle, prompt, neg == null ? "" : neg,
                width, height, steps, seed, cfg, vaeTiling, sampleMethod);
        lastMs = System.currentTimeMillis() - t0;
        setProgressListener(null);
        if (png == null || png.length == 0) {
            lastError = "推理失败：" + nativeLastError();
            return null;
        }
        lastError = "";
        Log.i(TAG, "出图耗时 " + lastMs + "ms，PNG " + png.length + " 字节");
        return png;
    }

    /** 请求中断当前生成。下一批采样点生效，不是立刻停。 */
    public void cancel() {
        if (handle != 0L) nativeCancel(handle);
    }

    /** 稳态基准摘要：复用 systemInfo + listDevices，不新增 JNI native。 */
    public static String benchSummary() {
        return "systemInfo=" + systemInfo() + " | " + "devices=" + listDevices();
    }

    /** SDXL 1024 原生分辨率路由：把宽高 clamp 到模型原生尺寸。 */
    public static int[] clampToNative(int w, int h, int nativeSize) {
        if (nativeSize <= 0) nativeSize = 1024;
        if (w > nativeSize) w = nativeSize;
        if (h > nativeSize) h = nativeSize;
        if (w < 64) w = 64;
        if (h < 64) h = 64;
        return new int[]{w, h};
    }

    public void close() {
        if (handle != 0L) {
            nativeFree(handle);
            handle = 0L;
        }
    }

    // ---- native（全部静态，C 侧签名见 jni/sd_jni.cpp）----
    private static native String nativeSystemInfo();
    private static native String nativeListDevices();
    private static native void nativeSetProgressListener(Progress p);
    private static native long nativeInit(String modelPath, int threads, boolean vaeTiling,
                                          String backend, String taesd);
    private static native byte[] nativeGenerate(long handle, String prompt, String neg,
                                                int width, int height, int steps,
                                                long seed, float cfg, boolean vaeTiling,
                                                int sampleMethod);
    private static native void nativeCancel(long handle);
    private static native void nativeFree(long handle);
    private static native String nativeLastError();
}
