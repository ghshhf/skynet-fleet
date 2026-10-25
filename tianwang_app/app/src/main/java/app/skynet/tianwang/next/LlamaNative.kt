package app.skynet.tianwang.next

import android.util.Log

/**
 * 本地推理 native 入口（llama.cpp 编进 app 的单 .so：libfleet_llama.so）。
 *
 * 🔴 这是「一个 APP 解决所有」的本地那一半：不借 PocketPal、不连外部 server，
 * 模型由天网 app 自己加载、自己生成。升级只改自己这一份。
 *
 * 🔴 这一层**只管透传**，不含任何策略：线程数、ctx、GPU 层数都由
 * Device.kt 的 Dev.plan() 按实测设备算好再传进来。设备差异不允许沉到这一层写死。
 */
object LlamaNative {

    private const val TAG = "LlamaNative"
    private val lock = Any()

    @Volatile
    private var loaded = false

    val isLoaded: Boolean get() = loaded

    /** 幂等加载 native 库。失败返回 false（缺 .so / ABI 不匹配都会走到这）。 */
    fun ensureLoaded(): Boolean {
        if (loaded) return true
        synchronized(lock) {
            if (loaded) return true
            loaded = try {
                System.loadLibrary("fleet_llama")
                true
            } catch (e: Throwable) {
                Log.e(TAG, "loadLibrary(fleet_llama) 失败：${e.javaClass.simpleName}: ${e.message}")
                false
            }
        }
        return loaded
    }

    /**
     * 加载模型，返回会话句柄（0 = 失败）。
     *
     * [gpuPercent] 0 = 纯 CPU；100 = 全部层；1..99 = 按层数比例。
     * 层数由 native 从 GGUF 头读，上层不需要（也无法）知道。
     */
    external fun nativeLoadModel(path: String, nCtx: Int, nThreads: Int, gpuPercent: Int): Long

    /** 释放会话。 */
    external fun nativeFreeModel(handle: Long)

    /**
     * GPU 能力，返回 "设备数|设备名"。
     * 本构建没有 GPU 后端时设备数为 0 —— 上层据此回落 CPU，而不是硬猜。
     */
    external fun nativeGpuInfo(): String

    /** 请求中止当前生成：下一个 token 边界退出，已产出的内容照常返回。 */
    external fun nativeAbort()

    /** 上一轮统计 "firstMs,totalMs,tokens"。 */
    external fun nativeLastStats(): String

    /**
     * 生成一轮回复，逐段回调 [TokenCallback.onDelta]（native 线程上来，
     * 已做 UTF-8 边界对齐，中文不会被 BPE 拆半字节）。返回完整文本。
     *
     * [roles]/[contents] 等长，按序即多轮对话；聊天模板由 native 用模型自带的
     * 套（Qwen3 的 ChatML 等），Kotlin 不自己拼模板。
     *
     * [noThink] 仅对混合思考模型（模板带 enable_thinking）生效。
     */
    external fun nativeChatMessages(
        handle: Long,
        roles: Array<String>,
        contents: Array<String>,
        maxTokens: Int,
        noThink: Boolean,
        callback: TokenCallback,
    ): String

    /** 流式增量回调。 */
    fun interface TokenCallback {
        fun onDelta(s: String)
    }

    /** GPU 探测结果（解析 native 的 "N|name"）。 */
    fun gpuInfo(): Pair<Int, String> {
        if (!ensureLoaded()) return 0 to ""
        return runCatching {
            val s = nativeGpuInfo()
            val i = s.indexOf('|')
            if (i < 0) s.toIntOrNull()?.let { it to "" } ?: (0 to "")
            else (s.substring(0, i).toIntOrNull() ?: 0) to s.substring(i + 1)
        }.getOrDefault(0 to "")
    }

    /** 上一轮统计。null = 还没跑过。 */
    fun lastStats(): GenStats? {
        if (!ensureLoaded()) return null
        val s = runCatching { nativeLastStats() }.getOrNull() ?: return null
        val p = s.split(',')
        if (p.size < 3) return null
        val first = p[0].toLongOrNull() ?: return null
        val total = p[1].toLongOrNull() ?: return null
        val tokens = p[2].toIntOrNull() ?: return null
        return GenStats(first, total, tokens)
    }
}

/** 一轮生成的实测数据。UI 用它显示「多快」，而不是让用户凭感觉猜。 */
data class GenStats(val firstMs: Long, val totalMs: Long, val tokens: Int) {
    /** 纯解码速度（扣掉首字等待），tok/s。 */
    val tokPerSec: Double
        get() {
            val genMs = totalMs - (if (firstMs > 0) firstMs else 0)
            return if (genMs > 0 && tokens > 1) (tokens - 1) * 1000.0 / genMs else 0.0
        }
}
