package app.skynet.tianwang.next

import java.util.UUID

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
 * 本地（llama.cpp / GGUF）。
 *
 * 原型阶段先落成可编译的桩：真实现要接 llama.cpp 的 JNI——
 * 老工程 `tianwang_android/jni/` 已经有 whisper_jni / sd_jni 的构建经验，
 * 下一个 JNI 模块按同一套 CMake 加，不另起炉灶。
 */
class LocalBackend : ModelBackend {
    override val kind = BackendKind.LOCAL
    override suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> {
        if (!profile.down) return Result.failure(IllegalStateException("这个模型还没下载完"))
        // TODO(jni): llama_model_load + llama_decode 流式回调 onDelta
        onDelta("（本地引擎待接 JNI：${profile.label}）")
        return Result.success("")
    }

    override suspend fun probe(profile: ModelProfile): Result<String> =
        if (profile.down) Result.success("GGUF 就绪：${profile.target}")
        else Result.failure(IllegalStateException("文件不在：${profile.target}"))
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
