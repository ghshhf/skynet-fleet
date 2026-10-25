package app.skynet.tianwang.next

import android.util.Log

/**
 * 本地推理 native 入口（llama.cpp 编进 app 的单 .so：libfleet_llama.so）。
 *
 * 🔴 这是「一个 APP 解决所有」的本地那一半：不借 PocketPal、不连外部 server，
 * 模型由天网 app 自己加载、自己生成。升级只改自己这一份。
 *
 * 首版纯 CPU（arm64-v8a）。GPU 卸载（Vulkan）留给下一版，接口不变。
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

    /** 加载模型，返回会话句柄（0 = 失败）。nThreads 建议 = 大核数（4~6）。 */
    external fun nativeLoadModel(path: String, nCtx: Int, nThreads: Int): Long

    /** 释放会话。 */
    external fun nativeFreeModel(handle: Long)

    /**
     * 生成一轮回复，逐段回调 [TokenCallback.onDelta]（native 线程上来，
     * 已做 UTF-8 边界对齐，中文不会被 BPE 拆半字节）。返回完整文本。
     *
     * [roles]/[contents] 等长，按序即多轮对话；聊天模板由 native 用模型自带的
     * 套（Qwen3 的 ChatML 等），Kotlin 不自己拼模板。
     */
    external fun nativeChatMessages(
        handle: Long,
        roles: Array<String>,
        contents: Array<String>,
        maxTokens: Int,
        callback: TokenCallback,
    ): String

    /** 流式增量回调。 */
    fun interface TokenCallback {
        fun onDelta(s: String)
    }
}
