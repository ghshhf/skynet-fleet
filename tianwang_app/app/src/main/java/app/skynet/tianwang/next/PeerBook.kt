package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject

/**
 * 手机集群的名册：谁在线、谁能跑什么、谁更适合接这一单。
 *
 * 这里只做**选路**，不做张量级算力切分。手机之间先解决「这台做不了/没电了，
 * 换那台做」——任务级调度对机型、系统版本、模型版本都不敏感，
 * 也不会因为两台手机 CPU 不同就整个跑不起来。
 *
 * 🔴 权限只有一档：**控制方（发起派单的这台）**的 `mcpAllowActions`。
 *    执行方只判断「自己有没有这个工具」，有就接；LAN 口令就是授权凭证。
 *    不再给每台手机单设「只读 / 可写」开关 —— 那等于项目里有两套标准。
 *
 * 🔴 写操作失败**不换机重试** —— 结果可能是「已执行但回包丢了」，
 *    换一台再执行一次就是重复点击 / 重复发送。
 */
object PeerBook {
    data class Peer(
        val name: String,
        val host: String,
        val port: Int = LanProto.DEFAULT_PORT,
        val token: String = "",
        val lastSeen: Long = 0L,
        val battery: Int = -1,
        val charging: Boolean = false,
        val screenOn: Boolean = false,
        val caps: Int = 0,
        val tools: Set<String> = emptySet(),
        val platform: String = "",
        val android: String = "",
        val lastError: String = "",
    ) {
        fun endpoint(): String = LanProto.bracket(host) + ":" + port

        /** 没探测过工具表时按「可能有」处理：先试一次，比直接判死更合理。 */
        fun supports(tool: String): Boolean = tools.isEmpty() || tools.contains(tool)
    }

    fun encode(peers: List<Peer>): String {
        val arr = JSONArray()
        peers.forEach { p ->
            arr.put(
                JSONObject()
                    .put("name", p.name)
                    .put("host", p.host)
                    .put("port", p.port)
                    .put("token", p.token)
                    .put("lastSeen", p.lastSeen)
                    .put("battery", p.battery)
                    .put("charging", p.charging)
                    .put("screenOn", p.screenOn)
                    .put("caps", p.caps)
                    .put("tools", JSONArray(p.tools.toList()))
                    .put("platform", p.platform)
                    .put("android", p.android)
                    .put("lastError", p.lastError)
            )
        }
        return arr.toString()
    }

    fun decode(text: String): List<Peer> {
        if (text.isBlank()) return emptyList()
        val arr = runCatching { JSONArray(text) }.getOrNull() ?: return emptyList()
        val out = ArrayList<Peer>(arr.length())
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val name = o.optString("name").trim()
            val host = o.optString("host").trim()
            if (name.isBlank() || host.isBlank()) continue
            val tools = HashSet<String>()
            o.optJSONArray("tools")?.let { t ->
                for (k in 0 until t.length()) {
                    val v = t.optString(k).trim()
                    if (v.isNotEmpty()) tools.add(v)
                }
            }
            out += Peer(
                name = name,
                host = host,
                port = o.optInt("port", LanProto.DEFAULT_PORT).coerceIn(1, 65535),
                token = o.optString("token"),
                lastSeen = o.optLong("lastSeen", 0L),
                battery = o.optInt("battery", -1).coerceIn(-1, 100),
                charging = o.optBoolean("charging", false),
                screenOn = o.optBoolean("screenOn", false),
                caps = o.optInt("caps", 0),
                tools = tools,
                platform = o.optString("platform"),
                android = o.optString("android"),
                lastError = o.optString("lastError"),
            )
        }
        return out
    }

    fun upsert(peers: List<Peer>, peer: Peer): List<Peer> {
        val out = peers.filterNot { it.name.equals(peer.name, ignoreCase = true) }.toMutableList()
        out += peer
        return out.sortedBy { it.name.lowercase() }
    }

    fun remove(peers: List<Peer>, name: String): List<Peer> =
        peers.filterNot { it.name.equals(name.trim(), ignoreCase = true) }

    fun find(peers: List<Peer>, name: String): Peer? =
        peers.firstOrNull { it.name.equals(name.trim(), ignoreCase = true) }

    /**
     * 适合接单的顺序：充电中 > 电量高 > 最近见过。
     * - 没探测过电量（-1）不直接排除，但排在已知电量之后；
     * - 只按「对端有没有这个工具」过滤；能不能执行由控制方这一档决定；
     * - 电量低于 [minBattery] 且没充电的机器不派活。
     */
    fun rank(
        peers: List<Peer>,
        tool: String,
        minBattery: Int = 15,
    ): List<Peer> = peers
        .filter { it.supports(tool) }
        .filter { it.charging || it.battery < 0 || it.battery >= minBattery }
        .sortedWith(
            compareByDescending<Peer> { it.charging }
                .thenByDescending { if (it.battery < 0) 1 else it.battery }
                .thenByDescending { it.lastSeen }
                .thenBy { it.name.lowercase() }
        )

    /** 展示用：口令只露头尾，任何列表/审计都不回显完整口令。 */
    fun mask(token: String): String =
        if (token.length <= 8) "未设置" else token.take(4) + "…" + token.takeLast(4)

    fun line(p: Peer): String = buildString {
        append(p.name).append(" · ").append(p.endpoint())
        if (p.platform.isNotBlank()) append(" · ").append(p.platform)
        if (p.android.isNotBlank()) append(" · Android ").append(p.android)
        append('\n')
        append("  电量：")
        if (p.battery < 0) append("未知") else append(p.battery).append('%')
        if (p.charging) append("（充电中）")
        append(" · 屏幕：").append(if (p.screenOn) "亮" else "灭/未知")
        append(" · 能力：").append(if (p.caps > 0) "${p.caps} 个工具" else "未探测")
        if (p.lastSeen > 0) {
            val ago = (System.currentTimeMillis() - p.lastSeen) / 1000
            append(" · 最近见到：").append(ago).append(" 秒前")
        }
        if (p.lastError.isNotBlank()) append("\n  上次错误：").append(p.lastError)
    }
}
