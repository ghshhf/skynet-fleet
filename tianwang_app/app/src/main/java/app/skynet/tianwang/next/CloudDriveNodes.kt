package app.skynet.tianwang.next

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.Inet4Address
import java.util.concurrent.TimeUnit

/**
 * 对等节点发现：网盘聚合**不认主节点**。
 *
 * 每台设备都是一模一样的节点，任意节点都可以自己跑一份 OpenList / WebDAV，
 * 任意节点也都能调用其它节点。这里只做「把可达的节点列出来」：
 *  - 自节点（127.0.0.1）：手机上自己跑的服务，或 USB 调试转发进来的对端服务；
 *  - 集群名册 fleetPeers：已经在编队里、互相握过手的对等节点；
 *  - 本机网络的网关 / DNS：局域网里最可能开着聚合服务的地址（不扫整段，省电省时）。
 *
 * 发现只回答「谁在」，不预设账号密码、不预设谁提供服务；
 * 加不进列表、填什么凭据，都由本机用户决定，且只落本机。
 */
object CloudDriveNodes {
    const val PORT = 5244

    data class Hint(val name: String, val host: String)

    private val client = OkHttpClient.Builder()
        .connectTimeout(700, TimeUnit.MILLISECONDS)
        .readTimeout(900, TimeUnit.MILLISECONDS)
        .followRedirects(true)
        .build()

    /** 候选地址：不做任何网络请求，纯本机推算，界面上可以立刻列出。 */
    fun hints(): List<Hint> {
        val out = LinkedHashMap<String, Hint>()
        out["http://127.0.0.1:$PORT"] = Hint("自节点", "http://127.0.0.1:$PORT")

        // 编队名册里的每一台都是平级节点，没有「服务端」和「客户端」之分。
        runCatching {
            LanHub.peers().forEach { p ->
                val h = p.host.trim().removePrefix("[").removeSuffix("]")
                if (h.isBlank()) return@forEach
                val host = if (h.contains(":")) "http://[$h]:$PORT" else "http://$h:$PORT"
                out[host] = Hint(p.name, host)
            }
        }

        // 局域网线索：网关和 DNS 服务器本身也可能是节点。
        runCatching {
            val cm = App.ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val lp: LinkProperties? = cm?.activeNetwork?.let { cm.getLinkProperties(it) }
            val ips = LinkedHashSet<String>()
            lp?.dnsServers?.forEach { ip ->
                if (ip is Inet4Address && !ip.isLoopbackAddress) ips += ip.hostAddress.orEmpty()
            }
            lp?.routes?.forEach { r ->
                val ip = r.gateway
                if (ip is Inet4Address && !ip.isLoopbackAddress) ips += ip.hostAddress.orEmpty()
            }
            ips.filter { it.isNotBlank() }.forEach { ip ->
                val host = "http://$ip:$PORT"
                out[host] = Hint("局域网 · $ip", host)
            }
        }
        return out.values.toList()
    }

    /** 并发探活：只认 OpenList 的公开设置接口，避免把任意 80/5244 网页误当节点。 */
    suspend fun discover(): List<Hint> = withContext(Dispatchers.IO) {
        val all = hints()
        coroutineScope {
            all.map { h -> async { h.takeIf { alive(it.host) } } }.mapNotNull { it.await() }
        }
    }

    private fun alive(host: String): Boolean = runCatching {
        val req = Request.Builder()
            .url(host.trimEnd('/') + "/api/public/settings")
            .get()
            .header("User-Agent", "TianwangNext/1.0")
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return@use false
            val body = r.body?.string().orEmpty()
            val o = runCatching { org.json.JSONObject(body) }.getOrNull() ?: return@use false
            o.optInt("code", 200) == 200
        }
    }.getOrDefault(false)

    /** 节点管理页：各网盘账号在这台节点上登录/扫码/输验证码，手机内嵌 WebView 打开即可。 */
    fun manageUrl(host: String): String = host.trimEnd('/') + "/@manage"
}
