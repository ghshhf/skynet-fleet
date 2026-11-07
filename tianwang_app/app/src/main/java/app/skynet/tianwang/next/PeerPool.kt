package app.skynet.tianwang.next

/**
 * 双机并行算力池的纯策略层：先选一波机器同时跑，第一波没结果再升级到名册里剩下的机器。
 *
 * 这里刻意不碰网络、不碰 Android —— 「谁先上、什么时候加机器、结果怎么合并」
 * 全部可以在 JVM 里断言，真机上只剩「把请求发出去」。
 *
 * 🔴 只并行**只读**问题（例如让两台手机各自的本地模型回答同一句话）。
 *    写操作不并行：同一动作在两台机器上一起执行，就是重复点击 / 重复发送。
 */
object PeerPool {
    enum class Mode {
        /** 谁先回用谁；其余结果只做脚注。 */
        RACE,
        /** 全部回包都列出来，适合对照两个模型的答案。 */
        ALL,
    }

    /** 这块算力在哪：手机 / 电脑 / 云端 API。 */
    enum class Lane { PHONE, PC, API }

    data class Node(
        val name: String,
        val battery: Int = -1,
        val charging: Boolean = false,
        val lastSeen: Long = 0L,
        val lane: Lane = Lane.PHONE,
    )

    data class Attempt(
        val name: String,
        val ok: Boolean,
        val ms: Long,
        val text: String = "",
        val err: String = "",
        val lane: Lane = Lane.PHONE,
    )

    /** 默认两台：本机 + 一台对端。再多就先把结果拿回来，别一上来把整个名册叫醒。 */
    const val DEFAULT_PARALLEL = 1

    /** 升级后的硬上限（含本机最多 5 台）。手机不是服务器，风扇和电池都有限。 */
    const val MAX_PARALLEL = 4

    fun mode(raw: String?): Mode =
        if (raw?.trim()?.equals("all", ignoreCase = true) == true) Mode.ALL else Mode.RACE

    /**
     * 第一波候选：充电中 > 电量高 > 最近见到。
     * 电量低于 [minBattery] 且没充电的机器不叫醒 —— 借算力不能把对端的电借没。
     */
    fun firstWave(
        nodes: List<Node>,
        parallel: Int = DEFAULT_PARALLEL,
        minBattery: Int = 15,
    ): List<Node> = nodes
        .filter { it.lane == Lane.PHONE }
        .filter { it.charging || it.battery < 0 || it.battery >= minBattery }
        .sortedWith(
            compareByDescending<Node> { it.charging }
                .thenByDescending { if (it.battery < 0) 1 else it.battery }
                .thenByDescending { it.lastSeen }
                .thenBy { it.name.lowercase() }
        )
        .take(parallel.coerceIn(1, MAX_PARALLEL))

    /**
     * 超限升级：第一波没跑出可用结果（或本机模型明确报上下文/显存不够）时，
     * 把名册里还没试过的机器全拉进来。只升一级，不无限扩散。
     */
    fun escalateWave(
        nodes: List<Node>,
        tried: Set<String>,
        minBattery: Int = 15,
    ): List<Node> = firstWave(nodes, MAX_PARALLEL, minBattery)
        .filterNot { it.name in tried }
        .take(MAX_PARALLEL)

    /** 电脑/API 车道：电脑排前面（通常更快、更可控），API 按名字排。 */
    fun remoteWave(nodes: List<Node>): List<Node> = nodes
        .filter { it.lane != Lane.PHONE }
        .sortedWith(compareBy<Node> { if (it.lane == Lane.PC) 0 else 1 }.thenBy { it.name })

    /**
     * 是否值得再叫一波机器。
     * - 第一波全失败 ⇒ 升级；
     * - 本机模型报「上下文 / 显存 / 内存」不够 ⇒ 即使对端成功也升级，让更多机器参与；
     * - 只升一次，避免网络抖动时把整个名册反复叫醒。
     */
    fun shouldEscalate(
        attempts: List<Attempt>,
        enabled: Boolean,
        alreadyEscalated: Boolean,
        localName: String = "本机",
    ): Boolean {
        if (!enabled || alreadyEscalated || attempts.isEmpty()) return false
        if (attempts.none { it.ok }) return true
        return attempts.any { it.name == localName && !it.ok && overflow(it.err) }
    }

    /** 本地模型的「这台带不动」类错误：上下文、显存、内存、token 上限。 */
    fun overflow(err: String): Boolean {
        val e = err.lowercase()
        return listOf("上下文", "context", "显存", "内存", "oom", "out of memory", "token 上限")
            .any { e.contains(it) }
    }

    /**
     * 合并回包。每段正文截断，防止两台手机各回几千字把控制方上下文撑爆。
     */
    fun aggregate(attempts: List<Attempt>, mode: Mode, clip: Int = 1200): String {
        if (attempts.isEmpty()) return "没有机器接单"
        val ok = attempts.filter { it.ok }.sortedBy { it.ms }
        val bad = attempts.filterNot { it.ok }
        if (ok.isEmpty()) {
            return buildString {
                append("全部失败（").append(attempts.size).append(" 台）：")
                bad.forEach { append("\n✗ ").append(it.name).append("：").append(oneLine(it.err)) }
            }
        }
        val out = StringBuilder()
        if (mode == Mode.RACE) {
            val first = ok.first()
            out.append("由 ").append(first.name).append(" 先回（").append(first.ms).append("ms）\n")
            out.append(clipText(first.text, clip))
            if (ok.size > 1) {
                out.append("\n\n另外 ").append(ok.size - 1).append(" 台也回了：")
                ok.drop(1).forEach { out.append("\n· ").append(it.name).append("（").append(it.ms).append("ms）") }
            }
        } else {
            out.append(ok.size).append(" 台成功（").append(attempts.size).append(" 台参与）：")
            ok.forEach {
                out.append("\n\n—— ").append(it.name).append(" · ").append(it.ms).append("ms ——\n")
                out.append(clipText(it.text, clip))
            }
        }
        if (bad.isNotEmpty()) {
            out.append("\n\n没回的：")
            bad.forEach { out.append("\n✗ ").append(it.name).append("：").append(oneLine(it.err)) }
        }
        return out.toString()
    }

    private fun clipText(s: String, n: Int): String {
        val t = s.trim()
        return if (t.length <= n) t else t.take(n) + "\n…（已截断 ${t.length - n} 字）"
    }

    private fun oneLine(s: String, n: Int = 160): String {
        val t = s.replace('\n', ' ').trim()
        return if (t.length <= n) t else t.take(n) + "…"
    }
}
