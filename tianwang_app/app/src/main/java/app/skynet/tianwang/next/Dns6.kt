package app.skynet.tianwang.next

import java.net.Inet6Address
import java.net.InetAddress
import java.net.UnknownHostException

/**
 * 名字 → 地址。动态 IPv6 的真正入口。
 *
 * 手机的全局 IPv6 会随网络切换、租约、隐私地址轮换而变，把地址写死在对端配置里
 * 一定会过期。稳定的做法只有一种：**对端记名字，本机把当前地址发布到这个名字上**。
 * 这里只负责「把名字解析成一组地址，并优先试 IPv6」；发布在 [Ddns]。
 *
 * 纯 JVM，不碰 Android，单测能直接跑。
 */
object Dns6 {
    data class Addr(val host: String, val family: LanProto.Family) {
        fun line(): String = "$host  ${family.label}"
    }

    /**
     * 只按字面量格式判断，绝不把域名交给 getByName —— 那会在「判断是不是 IP」时
     * 顺手发起一次 DNS 查询，既慢又把「离线」变成「解析超时」。
     */
    fun isLiteral(host: String): Boolean {
        val h = host.trim().removePrefix("[").removeSuffix("]").substringBefore('%')
        if (h.count { it == ':' } >= 2) return true
        return Regex("""^\d{1,3}(\.\d{1,3}){3}$""").matches(h)
    }

    /**
     * 解析域名并按「跨网直连优先」排序：
     * 全局 v6 > ULA v6 > 私网 v4 > 公网 v4 > 链路本地 v6。
     *
     * 公网 v4 排在私网 v4 后面：手机在运营商 NAT 后面通常根本不可入站，
     * 同一 Wi-Fi 的私网 v4 反而更可能通。链路本地 v6 最后：DNS 给的 v6
     * 没有 `%网卡`，就算排前面也连不上。
     */
    fun resolve(host: String): List<InetAddress> {
        val h = host.trim().removePrefix("[").removeSuffix("]")
        if (h.isEmpty()) throw UnknownHostException("地址为空")
        if (isLiteral(h)) return listOf(InetAddress.getByName(h))
        val all = runCatching { InetAddress.getAllByName(h).toList() }
            .getOrElse { throw UnknownHostException("解析不了 $h：${it.message ?: it.javaClass.simpleName}") }
        if (all.isEmpty()) throw UnknownHostException("$h 没有地址记录")
        return all.distinctBy { it.hostAddress }.sortedWith(
            compareByDescending<InetAddress> { score(it) }.thenBy { it.hostAddress }
        )
    }

    fun score(addr: InetAddress): Int = when (LanProto.familyOf(addr)) {
        LanProto.Family.GLOBAL_V6 -> 60
        LanProto.Family.ULA_V6 -> 50
        LanProto.Family.PRIVATE_V4 -> 40
        LanProto.Family.PUBLIC_V4 -> 30
        LanProto.Family.LINK_LOCAL_V6 -> 10
        else -> 0
    }

    fun describe(host: String): String {
        val list = runCatching { resolve(host) }.getOrElse {
            return "$host → ${it.message ?: "解析失败"}"
        }
        return list.joinToString("\n") { addr ->
            val fam = LanProto.familyOf(addr)
            "${LanProto.bracket(addr.hostAddress ?: "?")}  ${fam.label}"
        }
    }

    /** 能被公网 DNS 指向的地址：只取全局 IPv6。链路本地 / ULA 发出去也没有意义。 */
    fun globalV6(): List<String> = LanProto.localAddresses()
        .filter { it.family == LanProto.Family.GLOBAL_V6 }
        .map { it.host }

    /** 链路本地地址补上 `%网卡`；DNS 返回的地址没有作用域，补不出来就原样返回。 */
    fun scoped(addr: InetAddress): String {
        val host = addr.hostAddress ?: return "?"
        if (addr is Inet6Address && addr.isLinkLocalAddress && addr.scopeId == 0) {
            val iface = runCatching { java.net.NetworkInterface.getByInetAddress(addr)?.name }.getOrNull()
            if (!iface.isNullOrBlank()) return "$host%$iface"
        }
        return host
    }
}
