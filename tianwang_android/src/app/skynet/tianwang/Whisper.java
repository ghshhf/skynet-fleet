package app.skynet.tianwang;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;

/**
 * 手机本地语音识别（whisper.cpp，arm64 CPU）。
 *
 * 存在的理由：原来「听」这一步必须把录音传回电脑，电脑那边跑 whisper。
 * 那样手机只要离开局域网就是个哑巴。这里把识别搬进手机，飞行模式下也能出字。
 *
 * 模型不放 assets：base 有一百多 MB，assets 会被原样打进 APK 且无法按需删除，
 * 装一次就永久占着。改成放应用私有目录，首次需要时才从电脑推过来（见 README）。
 *
 * 全部方法都是阻塞的，必须在工作线程调用；一次识别几百毫秒到几秒，不要放主线程。
 */
public final class Whisper {

    private static final String TAG = "fleet-whisper";

    static {
        // 只加载一个 .so：whisper 和 ggml 已静态链进来，没有依赖顺序问题。
        System.loadLibrary("fleet_whisper");
    }

    private long handle;

    /** 模型是否已装进内存。 */
    public boolean isReady() {
        return handle != 0L;
    }

    /**
     * 加载模型。返回 false 时 {@link #lastError} 里有原因。
     *
     * 同一个实例重复 load 会先释放旧的，避免连点两次把内存撑爆。
     */
    public boolean load(String modelPath) {
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
        if (f.length() < 1024 * 1024) {
            // 小于 1MB 基本就是下载中断留下的半截文件，加载下去只会拿到一堆乱码
            lastError = "模型文件太小（" + f.length() + " 字节），多半没下完";
            return false;
        }
        long t0 = System.currentTimeMillis();
        handle = nativeLoad(modelPath);
        if (handle == 0L) {
            lastError = "底层加载失败：" + modelPath;
            return false;
        }
        loadMs = System.currentTimeMillis() - t0;
        Log.i(TAG, "模型加载耗时 " + loadMs + "ms：" + modelPath);
        return true;
    }

    /**
     * 识别一段 16kHz 单声道 16bit PCM。
     *
     * 采样率必须是 16000：whisper 内部按 16k 做梅尔谱，喂 44.1k 会变速变调，
     * 出来的是通顺但完全不对的句子——这种错误最难查，所以在入口直接拦。
     *
     * @return 识别结果；失败返回空串，原因在 {@link #lastError}
     */
    public String transcribe(short[] pcm, int sampleRate, String lang) {
        if (handle == 0L) {
            lastError = "模型还没加载";
            return "";
        }
        if (sampleRate != 16000) {
            lastError = "采样率必须是 16000，收到 " + sampleRate;
            return "";
        }
        if (pcm == null || pcm.length < 16000 / 4) {
            lastError = "音频太短（不足 250ms）";
            return "";
        }
        int threads = Math.max(1, Math.min(6, Runtime.getRuntime().availableProcessors() / 2));
        long t0 = System.currentTimeMillis();
        String text = nativeTranscribe(handle, pcm, threads, lang == null ? "zh" : lang);
        lastMs = System.currentTimeMillis() - t0;
        Log.i(TAG, "识别 " + pcm.length + " 样本耗时 " + lastMs + "ms -> " + text);
        if (text == null || text.isEmpty()) {
            lastError = "没听出内容（静音或模型不匹配）";
            return "";
        }
        lastError = "";
        return text;
    }

    public void close() {
        if (handle != 0L) {
            nativeFree(handle);
            handle = 0L;
        }
    }

    // ---- 诊断用 ----
    public String lastError = "";
    public long loadMs;
    public long lastMs;

    /** whisper.cpp 版本号，用来确认 .so 和头文件是一套。 */
    public static String version() {
        return nativeVersion();
    }

    /** 模型文件名。换 small 只需改这里和手机上放的文件名。 */
    public static final String MODEL_NAME = "ggml-base.bin";

    /**
     * 默认模型路径。
     *
     * 优先「外部私有目录」`Android/data/<pkg>/files/whisper/`：
     * 这条路径可以用 `adb push` 直接部署模型，不用把 App 改成 debuggable，
     * 也不用为「导模型」专门写一套文件选择 UI。内部私有目录作为兜底。
     *
     * 两条都是应用私有空间，不需要任何存储权限，卸载时一起清掉。
     */
    public static String defaultModelPath(Context ctx) {
        File ext = ctx.getExternalFilesDir(null);
        if (ext != null) {
            File f = new File(new File(ext, "whisper"), MODEL_NAME);
            if (f.isFile() && f.length() > 0) return f.getAbsolutePath();
        }
        return new File(modelDir(ctx), MODEL_NAME).getAbsolutePath();
    }

    /** 模型目录（给它建好，省得调用方到处 mkdirs）。 */
    public static File modelDir(Context ctx) {
        File dir = new File(ctx.getFilesDir(), "whisper");
        if (!dir.isDirectory() && !dir.mkdirs()) {
            Log.w(TAG, "建不了模型目录：" + dir);
        }
        return dir;
    }

    /**
     * 从 assets 里的模型复制到私有目录（只用于「模型打进 APK」这种场景）。
     * 当前不用，留着是因为以后要发独立安装包时省得重写。
     */
    public static boolean copyFromAssets(Context ctx, String assetName, File dst) throws IOException {
        if (dst.isFile() && dst.length() > 0) return true;
        File tmp = new File(dst.getAbsolutePath() + ".part");
        try (InputStream in = ctx.getAssets().open(assetName);
             FileOutputStream out = new FileOutputStream(tmp)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            out.getFD().sync();
        }
        return tmp.renameTo(dst);
    }

    /** 读一小段文件头，判断是不是 whisper 的 ggml 格式（magic: 6c6d6767 = "ggml"）。 */
    public static boolean looksLikeGgml(File f) {
        if (!f.isFile() || f.length() < 4) return false;
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[4];
            if (in.read(b) != 4) return false;
            return (b[0] & 0xff) == 0x6c && (b[1] & 0xff) == 0x6d
                    && (b[2] & 0xff) == 0x67 && (b[3] & 0xff) == 0x67;
        } catch (IOException e) {
            return false;
        }
    }

    private static native String nativeVersion();

    private static native long nativeLoad(String path);

    private static native String nativeTranscribe(long handle, short[] pcm, int threads, String lang);

    private static native void nativeFree(long handle);
}
