package app.skynet.tianwang.next

import java.net.Inet6Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.security.SecureRandom

/**
 * 手机直连的地址规则与帧格式。纯 JVM，不碰 Android，单测能直接跑。
 *
 * 🔴 IPv6 是这条链路的主路，不是备选：
 *   - 同 Wi-Fi 的 `fe80::` 链路本地地址一跳直达，没有 NAT，也没有中继；
 *   - 有全局 IPv6 时两台手机可以跨网络直连，只要运营商/路由器不拦入站；
 *   - IPv4 只在没有 IPv6 时兜底，热点/运营商 NAT 下经常根本连不进来。
 *
 * 所以这里的第一原则是：**地址必须带网卡作用域，端口必须和地址分开**。
 * `fe80::1%wlan0:18765` 这种写法是错的，正确是 `[fe80::1%wlan0]:18765`。
 */
object LanProto {
    const val DEFAULT_PORT = 18765
    const val MAX_LINE = 256 * 1024
    const val SERVICE_TYPE = "_skynet._tcp."
    const val MAX_SPEED_BYTES = 64 * 1024 * 1024

    data class Target(val host: String, val port: Int = DEFAULT_PORT) {
        /** `[v6]:port` 或 `v4:port`，日志与工具入参都用这一种写法。 */
        fun endpoint(): String = "${bracket(host)}:$port"
    }

    /** IPv6 字面量必须加方括号，否则 `::1:18765` 会被解析成一个地址。 */
    fun bracket(host: String): String {
        val h = host.trim().removePrefix("[").removeSuffix("]")
        return if (h.contains(':')) "[$h]" else h
    }

    /**
     * 解析 `host`、`host:port`、`[v6]:port`、裸 IPv6。
     * 裸 IPv6 不带端口时用 [defaultPort]；别用 `split(":")`，IPv6 会被切碎。
     */
    fun parse(raw: String, defaultPort: Int = DEFAULT_PORT): Target? {
        val s = raw.trim()
        if (s.isEmpty()) return null
        if (s.startsWith("[")) {
            val end = s.indexOf(']')
            if (end <= 0) return null
            val host = s.substring(1, end)
            val rest = s.substring(end + 1)
            val port = if (rest.startsWith(":")) {
                rest.drop(1).toIntOrNull() ?: return null
            } else defaultPort
            return if (host.isNotBlank() && port in 1..65535) Target(host, port) else null
        }
        // 只有一个冒号才可能是 v4:port；两个以上说明是裸 IPv6。
        if (s.count { it == ':' } == 1) {
            val host = s.substringBeforeLast(':')
            val port = s.substringAfterLast(':').toIntOrNull() ?: return null
            return if (host.isNotBlank() && port in 1..65535) Target(host, port) else null
        }
        return if (s.isNotBlank()) Target(s, defaultPort) else null
    }

    enum class Family(val label: String) {
        LOOPBACK("本机回环"),
        LINK_LOCAL_V6("IPv6 链路本地（同 Wi-Fi 最快，带 %网卡）"),
        ULA_V6("IPv6 私有地址（同网段）"),
        GLOBAL_V6("IPv6 公网（可跨网络直连）"),
        PRIVATE_V4("IPv4 局域网（可能被 NAT / 热点隔离）"),
        PUBLIC_V4("IPv4 公网（手机通常不可入站）"),
        OTHER("其他"),
    }

    fun familyOf(addr: InetAddress): Family = when {
        addr.isLoopbackAddress -> Family.LOOPBACK
        addr is Inet6Address -> when {
            addr.isLinkLocalAddress -> Family.LINK_LOCAL_V6
            isUla(addr) -> Family.ULA_V6
            addr.isAnyLocalAddress || addr.isMulticastAddress -> Family.OTHER
            else -> Family.GLOBAL_V6
        }
        addr.isSiteLocalAddress -> Family.PRIVATE_V4
        addr.isAnyLocalAddress || addr.isMulticastAddress -> Family.OTHER
        else -> Family.PUBLIC_V4
    }

    /** Java 没把 fc00::/7 标成 site-local，这里自己判。 */
    private fun isUla(addr: Inet6Address): Boolean {
        val b = addr.address
        return b.size == 16 && (b[0].toInt() and 0xfe) == 0xfc
    }

    /** 展示顺序：公网 v6 > 私有 v6 > 链路本地 v6 > 私有 v4 > 公网 v4。 */
    fun score(f: Family): Int = when (f) {
        Family.GLOBAL_V6 -> 5
        Family.ULA_V6 -> 4
        Family.LINK_LOCAL_V6 -> 3
        Family.PRIVATE_V4 -> 2
        Family.PUBLIC_V4 -> 1
        else -> 0
    }

    data class LocalAddr(val iface: String, val host: String, val family: Family, val prefix: Int) {
        fun line(): String = "$iface  ${bracket(host)}  ${family.label}  /$prefix"
    }

    /**
     * 本机所有可入站地址，按「跨网直连优先」排序。
     *
     * 链路本地地址一定要带 `%网卡名`，否则对端 connect 时内核不知道走哪张网卡。
     */
    fun localAddresses(): List<LocalAddr> {
        val out = ArrayList<LocalAddr>()
        runCatching {
            val nifs = NetworkInterface.getNetworkInterfaces() ?: return@runCatching
            for (nif in nifs) {
                if (!nif.isUp || nif.isLoopback) continue
                for (ia in nif.interfaceAddresses) {
                    val addr = ia.address ?: continue
                    val fam = familyOf(addr)
                    if (fam == Family.LOOPBACK || fam == Family.OTHER) continue
                    val host = if (addr is Inet6Address && addr.isLinkLocalAddress) {
                        "${addr.hostAddress}%${nif.name}"
                    } else {
                        addr.hostAddress ?: continue
                    }
                    out += LocalAddr(nif.name, host, fam, ia.networkPrefixLength.toInt())
                }
            }
        }
        return out.sortedWith(
            compareByDescending<LocalAddr> { score(it.family) }.thenBy { it.iface }
        )
    }

    /** 从地址字符串猜类型，给「对端给的地址能不能直连」这类提示用。 */
    fun familyLabel(host: String): String = runCatching {
        familyOf(InetAddress.getByName(host)).label
    }.getOrDefault("无法解析")

    fun newToken(): String {
        val b = ByteArray(16)
        SecureRandom().nextBytes(b)
        return b.joinToString("") { "%02x".format(it) }
    }

    /** 字节数与毫秒数 → 人话。手机 Wi-Fi 6 实测常在 300–900 Mbps，别拿慢当常态。 */
    fun speedLine(bytes: Int, ms: Double): String {
        if (ms <= 0.0) return "$bytes 字节"
        val mbps = bytes * 8.0 / (ms / 1000.0) / 1_000_000.0
        val mbs = bytes / 1_000_000.0 / (ms / 1000.0)
        return String.format(java.util.Locale.US, "%.1f MB/s（%.0f Mbps）", mbs, mbps)
    }

    /** 常量时间比较，别让「口令错在第几位」从耗时里漏出去。 */
    fun tokenEquals(got: String, want: String): Boolean {
        if (got.isEmpty() || want.isEmpty()) return false
        val a = got.toByteArray(Charsets.UTF_8)
        val b = want.toByteArray(Charsets.UTF_8)
        if (a.size != b.size) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].toInt() xor b[i].toInt())
        return diff == 0
    }

    /** 一条 tools/call 请求。手机直连和云中继共用同一个 MCP 协议。 */
    fun toolRequest(tool: String, args: org.json.JSONObject, id: Int = 1): String = org.json.JSONObject()
        .put("jsonrpc", "2.0")
        .put("id", id)
        .put("method", "tools/call")
        .put("params", org.json.JSONObject().put("name", tool).put("arguments", args))
        .toString()

    /**
     * 把 MCP 的 tools/call 回包压成一句人话。
     * 图片不回 base64 正文 —— 那是几 MB 的字符，塞进聊天记录只会把上下文撑爆。
     */
    fun summarizeMcpReply(raw: String): String {
        val j = runCatching { org.json.JSONObject(raw) }.getOrNull() ?: return raw
        j.optJSONObject("error")?.let { return "对端错误：${it.optString("message", "未知错误")}" }
        val result = j.optJSONObject("result") ?: return raw
        val content = result.optJSONArray("content") ?: return raw
        val sb = StringBuilder()
        for (i in 0 until content.length()) {
            val item = content.optJSONObject(i) ?: continue
            when (item.optString("type")) {
                "text" -> sb.append(item.optString("text"))
                "image" -> sb.append("[图片 ${item.optString("mimeType", "image/*")}，")
                    .append(item.optString("data").length).append(" 字符 base64]")
            }
        }
        return if (sb.isEmpty()) raw else sb.toString()
    }
}
