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
    /** 这一轮的实测性能（本地推理才有）。UI 拿它显示 tok/s，别让人凭感觉猜快慢。 */
    val stats: GenStats? = null,
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

/**
 * 编队快照（来自云侧 /api/nodes）。
 * [self] 是云侧那台机器，[nodes] 是控制面注册表里的成员，
 * [err] 非空表示这一屏现在没有可信数据 —— 宁可空着，也不拿假节点顶上去。
 */
data class FleetSnapshot(
    val self: NodeInfo? = null,
    val nodes: List<NodeInfo> = emptyList(),
    val cpAvailable: Boolean = false,
    val cpErr: String = "",
    val err: String = "",
) {
    val onlineCount: Int get() = nodes.count { it.online } + (if (self != null) 1 else 0)
    val totalCount: Int get() = nodes.size + (if (self != null) 1 else 0)
}

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
    var modelDir: String = ""

    /**
     * 🔴 这里原本写死了四条节点和三个会话（「1号机/2号机/旧笔记本」「手机版界面重构」…），
     * 2026-10-25 全删：节点改由云侧 /api/nodes 供给（见 Net.getNodes），
     * 会话目前还没做持久化，就老实空着 —— 让人看见「空」比看见「假」有价值。
     */
    val sessions: List<Session> = emptyList()
}

/**
 * 模型目录扫描。
 *
 * 🔴 为什么不写死三个文件名（2026-10-25 主人定则：写死就是逻辑错误）：
 *   换一台手机、换一个模型、用户自己塞一个 GGUF 进来 —— 写死的列表立刻失效，
 *   而且会「假装那个文件存在」。这里改成扫目录：有什么就认什么，
 *   显示名从文件名推，体积从文件本身量。
 */
object ModelCatalog {

    private val SIZE_RE = Regex("""(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])""")

    /** 扫本地目录，返回所有可用的 GGUF。空目录就是空列表 —— 不编造。 */
    fun local(): List<ModelProfile> {
        val dir = Demo.modelDir.takeIf { it.isNotBlank() }?.let { java.io.File(it) } ?: return emptyList()
        if (!dir.isDirectory) return emptyList()
        return dir.listFiles { f -> f.isFile && f.name.endsWith(".gguf", true) }
            ?.sortedByDescending { it.length() }
            ?.map { f -> fromFile(f) }
            ?: emptyList()
    }

    private fun fromFile(f: java.io.File): ModelProfile {
        val raw = f.name.removeSuffix(".gguf").removeSuffix(".GGUF")
        val sizeMb = (f.length() / (1024 * 1024)).toInt()
        // 从文件名里认参数规模，认不出就不写（不猜）
        val scale = SIZE_RE.find(raw)?.value?.uppercase()?.replace(" ", "") ?: ""
        val quant = listOf("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q4_0", "Q3_K_M", "Q2_K", "F16")
            .firstOrNull { raw.contains(it, true) } ?: ""

        val label = buildString {
            // 只把连字符换空格，下划线必须留着 —— 量化名是 Q8_0 / Q4_K_M，
            // 全换成空格会变成「Q8 0」这种看不懂的东西（实测踩过）。
            append(raw.replace('-', ' ').trim())
            if (scale.isNotEmpty() && !raw.contains(scale, true)) append(" $scale")
            append(" (本地)")
        }
        return ModelProfile(
            // 🔴 id 必须是**稳定**的（2026-10-26 实测踩到）：
            //    原来用 ModelProfile 默认的 UUID.randomUUID()，每次扫描都换一个 id，
            //    于是「上次选的模型」和「会话里记的模型」重启后永远对不上 ——
            //    表现就是恢复历史时模型莫名跳回别的那一个。
            //    用绝对路径当 id：同一个文件永远同一个 id，换了盘符/目录才是新模型。
            //    顺带让 LocalBackend 的句柄缓存也能真命中（否则每次都是新 key，白 mmap 一遍）。
            id = f.absolutePath,
            label = label,
            kind = BackendKind.LOCAL,
            target = f.absolutePath,
            ctx = 0,                 // 0 = 交给 Device 层按可用内存 + 模型训练长度算
            maxTokens = 0,           // 0 = 用全局偏好
            down = true,
            sizeMb = sizeMb,
            note = buildList {
                add("断网可用")
                if (scale.isNotEmpty()) add(scale)
                if (quant.isNotEmpty()) add(quant)
                add("${sizeMb} MB")
            }.joinToString(" · "),
        )
    }

    /**
     * 从云侧网关快照生成可选云模型。
     * 网关活着且有模型列表就展开成一条条可选项；没模型就只给一条「网关」占位，
     * 让用户至少能点进端点 —— 但绝不虚构模型名。
     */
    fun cloud(gateways: List<Gateway>, token: String): List<ModelProfile> {
        if (gateways.isEmpty()) return emptyList()
        val out = mutableListOf<ModelProfile>()
        for (gw in gateways) {
            val base = "http://${gw.endpoint}"
            // 🔴 说清楚「选云端意味着什么」（2026-10-26）：
            //    云端这条不是"另一台模型"，而是**电脑侧整套 Agent** ——
            //    它有 149 项能力、能调工具、能反过来操作这台手机。
            //    不写这句，用户会以为只是换了个更聪明的聊天对象，指令那半就白瞎了。
            val capNote = if (gw.modelCount > 0) {
                "在电脑侧执行 · 可调工具操作手机和电脑"
            } else {
                "在电脑侧执行（这个网关没报模型数）"
            }
            if (gw.modelCount > 0) {
                // 具体模型名要再问一次 /v1/models，这里先按数量占位不可行（不编造）
                // ⇒ 交给 Net.getGatewayModels 补全；此处只记录网关本身
                out.add(
                    ModelProfile(
                        label = "${gw.id} · 网关（${gw.modelCount} 模型）",
                        kind = BackendKind.CLOUD,
                        target = base,
                        model = "",
                        ctx = 0,
                        note = "${gw.endpoint} · ${gw.status} · $capNote",
                    )
                )
            } else {
                out.add(
                    ModelProfile(
                        label = "${gw.id} · 网关",
                        kind = BackendKind.CLOUD,
                        target = base,
                        model = "",
                        ctx = 0,
                        note = "${gw.endpoint} · ${gw.status} · $capNote" +
                            if (gw.note.isBlank()) "" else " · " + gw.note,
                    )
                )
            }
        }
        return out
    }
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

