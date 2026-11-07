package app.skynet.tianwang.next

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import java.net.Inet6Address
import java.net.NetworkInterface
import java.util.Collections

/**
 * 手机之间的 mDNS 发现。只广播「我在哪个端口、我叫什么」，**不广播口令**。
 *
 * 口令必须走另一条可信通道（用户自己看 `self.lan.start` 的回执，或以后做二维码）。
 * 把口令塞进 TXT 记录等于在同一 Wi-Fi 里公开，谁都能连。
 */
class LanDiscovery(
    ctx: Context,
    private val port: Int,
    private val displayName: String,
    private val deviceId: String,
) {
    data class Peer(
        val id: String,
        val name: String,
        val host: String,
        val port: Int,
        val seenAt: Long,
        val serviceName: String = "",
    )

    @Volatile
    var lastError: String = ""
        private set

    private val nsd = ctx.applicationContext.getSystemService(Context.NSD_SERVICE) as? NsdManager
    private val peers = Collections.synchronizedMap(LinkedHashMap<String, Peer>())
    private val resolving = Collections.synchronizedSet(HashSet<String>())
    private var registration: NsdManager.RegistrationListener? = null
    private var discovery: NsdManager.DiscoveryListener? = null

    fun start(): Boolean {
        val m = nsd ?: run { lastError = "系统没有 NsdManager"; return false }
        if (registration != null) return true

        val serviceName = "skynet-" + deviceId.take(6).ifBlank { "phone" }
        val info = NsdServiceInfo().apply {
            this.serviceName = serviceName
            serviceType = LanProto.SERVICE_TYPE
            this.port = this@LanDiscovery.port
            setAttribute("id", deviceId.ifBlank { serviceName })
            setAttribute("name", displayName.take(40))
            setAttribute("v", "1")
        }
        val reg = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {}
            override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {
                lastError = "mDNS 广播失败（$errorCode）"
            }
            override fun onServiceUnregistered(info: NsdServiceInfo) {}
            override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {
                lastError = "mDNS 注销失败（$errorCode）"
            }
        }
        val ok = runCatching { m.registerService(info, NsdManager.PROTOCOL_DNS_SD, reg) }
            .onFailure { lastError = "mDNS 广播异常：${it.javaClass.simpleName}" }
            .isSuccess
        if (!ok) return false
        registration = reg

        val disc = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                lastError = "mDNS 发现启动失败（$errorCode）"
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onDiscoveryStopped(serviceType: String) {}

            override fun onServiceFound(info: NsdServiceInfo) {
                val key = info.serviceName ?: return
                if (!resolving.add(key)) return
                runCatching {
                    @Suppress("DEPRECATION")
                    m.resolveService(info, object : NsdManager.ResolveListener {
                        override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                            resolving.remove(key)
                        }

                        override fun onServiceResolved(info: NsdServiceInfo) {
                            resolving.remove(key)
                            val addr = info.host ?: return
                            val host = hostWithScope(addr)
                            val id = info.attributes?.get("id")?.toString(Charsets.UTF_8)
                                ?: info.serviceName ?: return
                            val name = info.attributes?.get("name")?.toString(Charsets.UTF_8)
                                ?: info.serviceName ?: "手机"
                            peers[id] = Peer(
                                id, name, host, info.port, System.currentTimeMillis(),
                                info.serviceName ?: "",
                            )
                        }
                    })
                }.onFailure { resolving.remove(key) }
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                val key = info.serviceName ?: return
                synchronized(peers) {
                    peers.entries.removeAll { it.value.serviceName == key || it.key == key }
                }
            }
        }
        val discOk = runCatching {
            m.discoverServices(LanProto.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, disc)
        }.onFailure { lastError = "mDNS 发现异常：${it.javaClass.simpleName}" }.isSuccess
        if (discOk) discovery = disc
        return discOk
    }

    /** 链路本地 IPv6 必须带 `%网卡名`，否则对端 connect 会报 No route to host。 */
    private fun hostWithScope(addr: java.net.InetAddress): String {
        val raw = addr.hostAddress ?: return ""
        if (addr !is Inet6Address || !addr.isLinkLocalAddress) return raw
        if (raw.contains('%')) return raw
        val nif = runCatching { NetworkInterface.getByInetAddress(addr)?.name }.getOrNull()
        return if (nif.isNullOrBlank()) raw else "$raw%$nif"
    }

    fun stop() {
        val m = nsd ?: return
        discovery?.let { runCatching { m.stopServiceDiscovery(it) } }
        registration?.let { runCatching { m.unregisterService(it) } }
        discovery = null
        registration = null
        peers.clear()
        resolving.clear()
    }

    fun peerList(): List<Peer> = synchronized(peers) {
        peers.values.sortedByDescending { it.seenAt }
    }
}
