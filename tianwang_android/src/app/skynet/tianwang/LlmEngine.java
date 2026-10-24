package app.skynet.tianwang;

import android.util.Log;

import java.io.File;

/**
 * 天网 · 手机本地大模型推理（llama.cpp，arm64 CPU，内嵌 libfleet_llm.so）。
 *
 * 存在的理由（用户原话级）：原来「聊」只能把问题甩回电脑跑，手机自己下好的
 * 模型一直调不动；也不该依赖 Termux 这种「别的软件」去起一个外部服务——
 * 跨应用取目录麻烦、还得陪着跑脚本。这里把推理引擎直接打进 App，
 * 模型由 ModelManager 管（App 内下载、放在 App 自己的私有目录），
 * 聊天时引擎在 App 进程里原地跑，断网也能聊。
 *
 * 全部推理方法都是阻塞的且以秒~分钟计，必须在工作线程调用；
 * 增量文本经 {@link TokenListener#onToken(String)} 流式回传，UI 拿它刷新气泡。
 *
 * 不做的事（和 whisper/sd 同一个纪律）：不在这里判角色、不猜模型能不能聊、
 * 不吞错误——出问题时原因必须在 Java 侧看得见。
 */
public final class LlmEngine {

    private static final String TAG = "fleet-llm";
    private static final long MIN_MODEL_BYTES = 8L * 1024 * 1024;  // 小于 8MB 必是半截文件

    static {
        // 只加载一个 .so：llama + ggml 已静态链进来，没有 .so 依赖顺序问题。
        System.loadLibrary("fleet_llm");
    }

    /** 增量文本回调。在 generate 的调用线程上同步触发，实现里别做重活。 */
    public interface TokenListener {
        void onToken(String text);   // 新出的正文片段（可能是一个字、一个词）
        void onDone();               // 正常结束
        void onError(String msg);    // 出错（含 EOS 之外的异常）
    }

    private long handle;

    /** 模型是否已装进内存。 */
    public boolean isReady() {
        return handle != 0L;
    }

    // ---- 诊断用 ----
    public String lastError = "";
    public long loadMs;
    public long lastMs;

    /** llama.cpp 版本号，用来确认 .so 和头文件是一套。 */
    public static String version() {
        return nativeVersion();
    }

    /**
     * 加载模型。返回 false 时 {@link #lastError} 里有原因。
     * 同一个实例重复 load 会先释放旧的，避免连点两次把内存撑爆。
     *
     * @param nCtx    上下文窗口（token 数），4096 够日常多轮。
     * @param threads 推理线程数，传 0 = 让底层按 CPU 核数自己定。
     */
    public boolean load(String modelPath, int nCtx, int threads) {
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
        handle = nativeLoad(modelPath, nCtx, threads);
        if (handle == 0L) {
            lastError = "底层加载失败：" + nativeLastError();
            return false;
        }
        loadMs = System.currentTimeMillis() - t0;
        Log.i(TAG, "模型加载耗时 " + loadMs + "ms：" + modelPath);
        return true;
    }

    /**
     * 生成。messagesJson 是 OpenAI 风格消息数组的 JSON 字符串
     *（[{ "role":"system"|"user"|"assistant", "content":"..." }, ...]），
     * 底层会按模型自带的 chat 模板渲染、tokenize、逐 token 解码，
     * 每出新文本片段回调 {@link TokenListener#onToken}；遇到 EOS 或异常回调 onDone/onError。
     *
     * 必须在工作线程调用。listener 不可为 null。
     */
    public void generate(String messagesJson, TokenListener listener) {
        if (handle == 0L) {
            if (listener != null) listener.onError("模型还没加载");
            return;
        }
        if (listener == null) {
            lastError = "listener 是 null";
            return;
        }
        if (messagesJson == null || messagesJson.trim().isEmpty()) {
            listener.onError("messages 是空的");
            return;
        }
        long t0 = System.currentTimeMillis();
        nativeGenerate(handle, messagesJson, listener);
        lastMs = System.currentTimeMillis() - t0;
        Log.i(TAG, "一轮生成耗时 " + lastMs + "ms");
    }

    public void close() {
        if (handle != 0L) {
            nativeFree(handle);
            handle = 0L;
        }
    }

    // ---- JNI ----

    /** @return 模型句柄，0 表示失败（原因见 nativeLastError）。 */
    private static native long nativeLoad(String modelPath, int nCtx, int threads);

    /** 阻塞式生成；通过 listener 回调增量文本。线程安全：每次调用用自己的 listener。 */
    private static native void nativeGenerate(long handle, String messagesJson, TokenListener listener);

    private static native void nativeFree(long handle);

    private static native String nativeLastError();

    private static native String nativeVersion();
}
