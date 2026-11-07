package app.skynet.tianwang.next

import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 把一句自然语言目标整单交给本机本地模型执行。
 *
 * 这是集群的最后一层：控制方只说「做什么」，执行方自己选工具、自己跑完、只回结果。
 * 与 `self.peer.call` 的区别 —— 那边派的是单个工具，这边派的是一整单目标。
 *
 * 🔴 三条边界：
 * 1. 执行期间独占本地模型（同一台手机不并发跑两个目标）；
 * 2. 不把「整单目标 / 回派集群」再暴露给本地模型 —— 会形成 A→B→A 的循环；
 * 3. 不暴露会切断当前连接的工具（停 LAN、换口令、重启），否则回包永远到不了控制方。
 */
object LocalGoal {
    private val busy = AtomicBoolean(false)

    internal val blockedNames = setOf(
        "self_agent_goal", "self_peer_goal", "self_peer_call",
        "self_lan_start", "self_lan_stop", "self_owner_reboot",
    )

    suspend fun run(goal: String, maxSteps: Int = 3): ToolResult {
        if (goal.isBlank()) return ToolResult(false, err = "缺 goal：写清要让这台手机做什么")
        if (!busy.compareAndSet(false, true)) {
            return ToolResult(false, err = "本机正在执行另一个目标；等它跑完再派")
        }
        try {
            // 服务先于 Activity 启动时 modelDir 可能还没初始化，这里补一次。
            if (Demo.modelDir.isBlank()) {
                Demo.modelDir = File(App.ctx.getExternalFilesDir(null), "models").absolutePath
            }
            val models = ModelCatalog.local()
            if (models.isEmpty()) {
                return ToolResult(false, err = "本机没有 GGUF 模型。先在「模型」页下载一个，再把目标派过来")
            }
            val profile = models.firstOrNull { it.id == Prefs.lastModelId } ?: models.first()
            val tools = PhoneMcp.toolList().filterNot { it.name in blockedNames }
            // 外层通道（中继只读闸门 / LAN 口令）已经决定过权限，这里不再叠加第二档。
            val answer = LocalAgent.run(
                profile = profile,
                history = listOf(ChatMsg(role = ChatMsg.Role.USER, text = goal)),
                tools = tools,
                allowActions = true,
                maxSteps = maxSteps.coerceIn(1, 6),
                execute = { name, args -> PhoneMcp.executeTool(name, args) },
                onDelta = {},
                onTool = {},
            ).getOrElse { e ->
                return ToolResult(false, err = "本地目标执行失败：${e.message ?: e.javaClass.simpleName}")
            }
            return ToolResult(true, text = "由本机模型 ${profile.label} 完成：\n$answer")
        } finally {
            busy.set(false)
        }
    }
}
