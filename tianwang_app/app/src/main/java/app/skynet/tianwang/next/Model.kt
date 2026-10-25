package app.skynet.tianwang.next

import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 双后端模型层。
 *
 * 🔴 为什么要写成「一个接口 + 两个实现」，而不是学上游各做一套：
 *   - pocketpal-ai 只跑本地 GGUF（llama.cpp），断网能用、但手机上 3B 以上就吃力；
 *   - Operit 的架子是给云端 API 铺的（OpenAI/Anthropic/Gemini），强、但没网就废，
 *     而且隐私数据全出手机。
 *   两个各有各的死角，所以真正的产品必须**一个界面、两个引擎、随时切**：
 *   闲聊/敏感内容走本地，重活/长上下文走云端，甚至同一轮里云端规划、本地收尾。
 *   ⇒ 上层 UI 只认 [ModelBackend]，永远不写 `if (是不是本地)`。
 */

enum class BackendKind { LOCAL, CLOUD }

/** 一个可选的模型（本地是一个 GGUF 文件，云端是一条端点+模型名）。 */
data class ModelProfile(
    val id: String = UUID.randomUUID().toString(),
    val label: String,
    val kind: BackendKind,
    /** 本地：GGUF 绝对路径；云端：baseUrl（如 https://api.deepseek.com/v1） */
    val target: String = "",
    /** 云端：模型名（本地留空，从文件读） */
    val model: String = "",
    val ctx: Int = 4096,
    val nGpuLayers: Int = 0,
    val temperature: Float = 0.7f,
    val topP: Float = 0.9f,
    val maxTokens: Int = 1024,
    /** 是否带工具调用（云端原生 function calling；本地靠提示词约束） */
    val tools: Boolean = true,
    val down: Boolean = false,          // 本地模型是否已下载完
    val sizeMb: Int = 0,
    val note: String = "",
)

/** 统一的推理后端。两个实现见 [LocalBackend] / [CloudBackend]。 */
interface ModelBackend {
    val kind: BackendKind
    suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit = {},
    ): Result<String>
    suspend fun probe(profile: ModelProfile): Result<String>
}

/**
 * 本地（llama.cpp / GGUF）——真实现，JNI 静态链进 app 的单 .so（libfleet_llama.so）。
 *
 * 🔴 「一个 APP 解决所有」的本地那一半：模型文件在 /sdcard/tianwang/models/，
 * 加载/推理/模板全在 app 进程内完成，不借外部 app、不连外部 server。
 *
 * 句柄按 profile.id 缓存：同一模型连问多轮不重复 mmap 2.5GB 权重；
 * 每轮 native 侧会清 KV 重放全量历史（KV 复用留给下一版）。
 */
class LocalBackend : ModelBackend {
    override val kind = BackendKind.LOCAL

    /** profile.id → native 会话句柄。切模型/换文件先 free 旧的。 */
    private val handles = HashMap<String, Long>()

    private fun sessionFor(profile: ModelProfile): Result<Long> {
        if (!profile.down) return Result.failure(IllegalStateException("这个模型还没下载完"))
        synchronized(handles) { handles[profile.id] }?.let { return Result.success(it) }
        if (!LlamaNative.ensureLoaded())
            return Result.failure(IllegalStateException("native 库没加载上（缺 libfleet_llama.so？）"))
        val threads = (Runtime.getRuntime().availableProcessors()).coerceAtMost(6)
        val h = LlamaNative.nativeLoadModel(profile.target, profile.ctx, threads)
        if (h == 0L) return Result.failure(IllegalStateException("模型加载失败：${profile.target}"))
        synchronized(handles) {
            handles.remove(profile.id)?.let { LlamaNative.nativeFreeModel(it) }
            handles[profile.id] = h
        }
        return Result.success(h)
    }

    override suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> = withContext(Dispatchers.IO) {
        val h = sessionFor(profile).getOrElse { return@withContext Result.failure(it) }
        val roles = ArrayList<String>(messages.size)
        val contents = ArrayList<String>(messages.size)
        for (m in messages) {
            if (m.text.isBlank()) continue
            roles.add(
                when (m.role) {
                    ChatMsg.Role.USER -> "user"
                    ChatMsg.Role.ASSISTANT -> "assistant"
                    ChatMsg.Role.SYSTEM -> "system"
                }
            )
            contents.add(m.text)
        }
        if (roles.isEmpty()) return@withContext Result.failure(IllegalStateException("没有可发送的内容"))
        try {
            val full = LlamaNative.nativeChatMessages(
                h,
                roles.toTypedArray(),
                contents.toTypedArray(),
                profile.maxTokens,
                LlamaNative.TokenCallback { onDelta(it) },
            )
            Result.success(full)
        } catch (e: Throwable) {
            Result.failure(e)
        }
    }

    override suspend fun probe(profile: ModelProfile): Result<String> = withContext(Dispatchers.IO) {
        val h = sessionFor(profile).getOrElse { return@withContext Result.failure(it) }
        Result.success("GGUF 就绪：${profile.target}（句柄 $h）")
    }
}

/** 云端（OpenAI 兼容 /v1/chat/completions，SSE 流式）。 */
class CloudBackend : ModelBackend {
    override val kind = BackendKind.CLOUD
    override suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> {
        if (profile.target.isBlank() || profile.model.isBlank()) {
            return Result.failure(IllegalStateException("端点和模型名都得填"))
        }
        // TODO(http): POST {target}/chat/completions，stream=true，按 data: 行回调 onDelta
        onDelta("（云引擎待接 HTTP 流式：${profile.model} @ ${profile.target}）")
        return Result.success("")
    }

    override suspend fun probe(profile: ModelProfile): Result<String> =
        Result.success("端点已配置：${profile.target}")
}

/** 按 profile 取后端。唯一一处 if，别让这个判断漏到 UI 里去。 */
fun backendFor(profile: ModelProfile): ModelBackend =
    if (profile.kind == BackendKind.LOCAL) LocalBackend() else CloudBackend()
