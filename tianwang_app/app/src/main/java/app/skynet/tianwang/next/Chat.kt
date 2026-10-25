package app.skynet.tianwang.next

import java.util.UUID

/** 会话里的一条消息。工具调用不是"另一种消息"，而是挂在助手消息上的条目—— */
/** 这样重放历史时，用户看到的顺序就是模型当时真实走过的顺序。 */
data class ChatMsg(
    val id: String = UUID.randomUUID().toString(),
    val role: Role,
    val text: String = "",
    val thought: String = "",          // 思维链（Operit 那套"可展开的思考"）
    val tools: List<ToolCall> = emptyList(),
    val at: Long = System.currentTimeMillis(),
    val streaming: Boolean = false,
    val failed: String = "",
    val media: List<Media> = emptyList(),   // 生成的图/视频 —— 手机端已具备生成能力，这里只渲染
) {
    enum class Role { USER, ASSISTANT, SYSTEM }
}

/**
 * 一次工具调用。状态机只有三个终态，别再加"部分成功"——
 * 那是给人看的措辞，机器只需要知道"要不要重试"。
 */
data class ToolCall(
    val id: String = UUID.randomUUID().toString(),
    val name: String,
    val args: String = "",
    val result: String = "",
    val state: State = State.RUNNING,
    val ms: Long = 0,
) {
    enum class State { RUNNING, OK, FAIL }
}

/** 一个会话（Operit 有会话分组/分支，我们先要"能同时开多个"这一层）。 */
data class Session(
    val id: String = UUID.randomUUID().toString(),
    val title: String = "新会话",
    val msgs: List<ChatMsg> = emptyList(),
    val modelId: String = "",
    val at: Long = System.currentTimeMillis(),
    val tokens: Int = 0,
)

/** 天网节点（手机端要能看见自己在编队里的位置，这是上游两家都没有的东西）。 */
data class NodeInfo(
    val name: String,
    val host: String,
    val online: Boolean,
    val caps: Int = 0,
    val note: String = "",
)

object Demo {
    /**
     * 模型落点 = **app 私有外置目录**，由 MainActivity 在 onCreate 里用
     * `getExternalFilesDir(null)` 注入真实绝对路径。
     *
     * 🔴 **不能硬编码 `/sdcard/...`**（2026-10-25 实测踩到）：
     * targetSdk 35 下 app 进程按 `/sdcard` 这个 FUSE 视图访问自己的
     * `Android/data/<pkg>/files` 会被拒，`File.exists()` 直接返回 false，
     * 表现就是「模型明明推上去了，UI 却说还没下载完」。
     * 只有系统给的 `/storage/emulated/0/Android/data/<pkg>/files` 才稳。
     *
     * 推送：`adb push xxx.gguf /sdcard/Android/data/app.skynet.tianwang.next/files/models/`
     */
    @Volatile
    var modelDir: String = "/sdcard/Android/data/app.skynet.tianwang.next/files/models"

    /** 文件真在才敢说就绪——不拿常量骗 UI（加载失败会白等一轮）。 */
    private fun exists(name: String): Boolean = java.io.File("$modelDir/$name").exists()

    /** 每次取都重新探盘：模型推到手机后不用重启 app 就能亮起来。 */
    val models: List<ModelProfile>
        get() = listOf(
        ModelProfile(
            label = "Qwen3-4B-Instruct (本地)", kind = BackendKind.LOCAL,
            target = "$modelDir/qwen3-4b-q4.gguf", ctx = 8192,
            nGpuLayers = 0, down = exists("qwen3-4b-q4.gguf"), sizeMb = 2500,
            note = "断网可用 · 4B Q4_K_M · 手机上约 6~10 tok/s",
        ),
        ModelProfile(
            label = "SmolLM2-360M (本地)", kind = BackendKind.LOCAL,
            target = "$modelDir/smollm2-360m-q8.gguf", ctx = 4096,
            down = exists("smollm2-360m-q8.gguf"), sizeMb = 386,
            note = "极速兜底 · 适合摘取/分类这种短任务",
        ),
        ModelProfile(
            label = "Qwen3-1.7B (本地)", kind = BackendKind.LOCAL,
            target = "$modelDir/qwen3-1.7b-q4.gguf", ctx = 8192,
            down = exists("qwen3-1.7b-q4.gguf"), sizeMb = 1107,
            note = "轻量主力 · 比 4B 快一倍，中文够用",
        ),
        ModelProfile(
            label = "deepseek-chat (云)", kind = BackendKind.CLOUD,
            target = "https://api.deepseek.com/v1", model = "deepseek-chat",
            ctx = 65536, note = "长上下文 · 工具调用稳定",
        ),
        ModelProfile(
            label = "qwen-max (云)", kind = BackendKind.CLOUD,
            target = "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model = "qwen-max", ctx = 32768, note = "中文强 · 走兼容模式端点",
        ),
    )

    val nodes = listOf(
        NodeInfo("1号机 · 总控", "192.168.0.100", true, 149, "控制面 + 存储 + 常驻模型"),
        NodeInfo("2号机 · 游戏工具箱", "192.168.0.103", true, 61, "Lecoo / Win11 · 编队节点"),
        NodeInfo("本机 · 手机", "127.0.0.1", true, 34, "你正在用的这台"),
        NodeInfo("旧笔记本", "192.168.0.117", false, 0, "上次心跳 3 天前"),
    )

    val sessions = listOf(
        Session(title = "手机版界面重构", modelId = "", tokens = 12840),
        Session(title = "逆向：字节层协议", modelId = "", tokens = 3390),
        Session(title = "Mekanism 优化", modelId = "", tokens = 812),
    )

    fun seedMessages(): List<ChatMsg> = listOf(
        ChatMsg(
            role = ChatMsg.Role.USER,
            text = "帮我看下 2 号机现在的状态，然后把它的 model 目录列出来",
        ),
        ChatMsg(
            role = ChatMsg.Role.ASSISTANT,
            thought = "需要先确认节点在不在线，再走 file.list 拿目录。两个动作可以串行，" +
                "第一个失败就没必要发第二个。",
            tools = listOf(
                ToolCall(
                    name = "fleet.nodes", args = "{\"detail\": true}",
                    result = "2 台在线 / 共 3 台：DESKTOP-1BQS174 ✓、PC2-local ✓、旧笔记本 ✗",
                    state = ToolCall.State.OK, ms = 340,
                ),
                ToolCall(
                    name = "node.exec",
                    args = "{\"node\":\"DESKTOP-1BQS174\",\"cmd\":\"dir D:\\\\models\"}",
                    result = "qwen3-4b-q4.gguf  2.4 GB\r\nsmollm2-360m-q8.gguf  386 MB",
                    state = ToolCall.State.OK, ms = 890,
                ),
            ),
            text = "2 号机在线，`D:\\models` 下有两个模型：\n\n" +
                "- **qwen3-4b-q4.gguf** — 2.4 GB\n" +
                "- **smollm2-360m-q8.gguf** — 386 MB\n\n" +
                "要我把 4B 那个推到手机本地跑吗？推 2.4 GB 走局域网大概 40 秒。",
        ),
    )
}

/** 网关（云端引擎的真实状态 —— 来自云侧 /api/providers）。 */
data class Gateway(
    val id: String = "",
    val status: String = "死",
    val endpoint: String = "",
    val latencyMs: Int = 0,
    val modelCount: Int = 0,
    val note: String = "",
)

/** 一项能力（工具箱的数据源 —— 来自云侧 /api/caps，按 domain 分组）。 */
data class Capability(
    val name: String = "",
    val title: String = "",
    val domain: String = "",
    val params: List<CapParam> = emptyList(),
    val sideEffect: Boolean = false,
    val destructive: Boolean = false,
    val approval: String = "auto",
    val note: String = "",
)

data class CapParam(
    val name: String = "",
    val type: String = "str",
    val desc: String = "",
    val required: Boolean = false,
)

/** 工具单点执行的结果（工具箱点一下之后的回显）。 */
data class ToolResult(
    val ok: Boolean = false,
    val text: String = "",
    val err: String = "",
)

/** 项目任务进度（来自云侧 /api/tasks）。 */
data class Phase(
    val id: String = "",
    val name: String = "",
    val done: Boolean = false,
)

data class TaskBoard(
    val phases: List<Phase> = emptyList(),
    val recentCommits: List<String> = emptyList(),
    val branch: String = "",
)

/** 一条媒体附件（生成的图片 / 视频 —— 手机端已具备生成能力，这里只负责渲染）。 */
data class Media(
    val kind: Kind = Kind.IMAGE,
    val uri: String = "",        // http(s) 或 file:// 或 content://
    val caption: String = "",
) {
    enum class Kind { IMAGE, VIDEO }
}

/** 云侧连接配置（存在 SharedPreferences，设置页读写）。 */
data class AgentConfig(
    val url: String = "http://127.0.0.1:9124",
    val token: String = "",
) {
    fun valid(): Boolean = url.isNotBlank() && token.isNotBlank()
}

