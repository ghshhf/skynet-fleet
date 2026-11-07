package app.skynet.tianwang.next

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * 动态 IPv6 → 稳定名字。
 *
 * 手机的全局 IPv6 是自动分配的，也**必然会变**：换 Wi-Fi、换运营商、租约到期、
 * 隐私地址轮换都会换一个。对端如果记 IP，隔天就失效；记名字就不会。
 *
 * 做法只有一条：把当前全局 IPv6 发布到一个 DNS 名字（DuckDNS / 自建更新接口），
 * 对端解析名字拿当前地址。这里不做「自己发明一套地址簿」——
 * DNS 本来就是干这个的，缓存、TTL、多地址回退全都是现成的。
 *
 * 运营商拦入站是另一层问题：DDNS 只解决「找得到」，不解决「进得来」。
 * 进不来时仍然回落中继，这条不隐藏、不假装成功。
 */
object Ddns {
    const val PROVIDER_DUCK = "duckdns"
    const val PROVIDER_CUSTOM = "custom"
    const val PROVIDER_OFF = "off"

    data class Cfg(
        val provider: String,
        val domain: String,
        val token: String,
        val url: String,
        val enabled: Boolean,
    )

    fun cfg(): Cfg = Cfg(
        provider = Prefs.ddnsProvider,
        domain = Prefs.ddnsDomain,
        token = Prefs.ddnsToken,
        url = Prefs.ddnsUrl,
        enabled = Prefs.ddnsEnabled,
    )

    /** `myphone` / `myphone.duckdns.org` 都收，只留合法子域。 */
    fun normalizeDuckDomain(raw: String): String? {
        val d = raw.trim().lowercase().removeSuffix(".")
            .removeSuffix(".duckdns.org")
            .removeSuffix(".duckdns.org")
        if (d.isEmpty()) return null
        if (!Regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$").matches(d)) return null
        return d
    }

    fun nameOf(c: Cfg): String = when (c.provider) {
        PROVIDER_DUCK -> "${c.domain}.duckdns.org"
        PROVIDER_CUSTOM -> {
            val base = c.url.substringBefore('?').ifBlank { "自定义地址" }
            if (c.token.isBlank()) base else base.replace(c.token, "<已隐藏>")
        }
        else -> "未配置"
    }

    /**
     * 更新 URL。DuckDNS 用官方 GET 接口；自定义模板必须含 `{ipv6}`，可选 `{token}`。
     * 返回 null 表示配置不完整。
     */
    fun updateUrl(c: Cfg, ipv6: String): String? {
        val ip = enc(ipv6)
        return when (c.provider) {
            PROVIDER_DUCK -> {
                if (c.domain.isBlank() || c.token.isBlank()) return null
                "https://www.duckdns.org/update?domains=${enc(c.domain)}" +
                    "&token=${enc(c.token)}&ipv6=$ip&verbose=true"
            }
            PROVIDER_CUSTOM -> {
                if (!c.url.contains("{ipv6}")) return null
                c.url.replace("{ipv6}", ip).replace("{token}", enc(c.token))
            }
            else -> null
        }
    }

    /** 配置或改配置。token 传空表示「沿用已存的」，避免每次改域名都要重打密钥。 */
    fun save(
        provider: String,
        domain: String?,
        token: String?,
        url: String?,
        enabled: Boolean,
    ): ToolResult {
        val p = provider.trim().lowercase().ifBlank { PROVIDER_OFF }
        if (p == PROVIDER_OFF) {
            Prefs.ddnsProvider = PROVIDER_OFF
            Prefs.ddnsEnabled = false
            stopWatch()
            return ToolResult(true, text = "DDNS 已关闭；不会再向任何地址发布本机 IPv6。")
        }
        if (p != PROVIDER_DUCK && p != PROVIDER_CUSTOM) {
            return ToolResult(false, err = "provider 只支持 duckdns｜custom｜off，收到：$p")
        }
        if (p == PROVIDER_DUCK) {
            val d = normalizeDuckDomain(domain ?: "")
                ?: return ToolResult(false, err = "DuckDNS 子域不合法：只允许字母、数字、连字符，例如 myphone 或 myphone.duckdns.org")
            val t = token?.trim().takeUnless { it.isNullOrBlank() } ?: Prefs.ddnsToken
            if (t.isBlank()) return ToolResult(false, err = "缺 DuckDNS token：登录 duckdns.org 首页就能看到")
            Prefs.ddnsProvider = p
            Prefs.ddnsDomain = d
            Prefs.ddnsToken = t
        } else {
            val u = url?.trim().takeUnless { it.isNullOrBlank() } ?: Prefs.ddnsUrl
            if (u.isBlank() || !u.contains("{ipv6}")) {
                return ToolResult(false, err = "custom 需要 url 模板且必须含 {ipv6}，例如 https://ddns.example.com/update?ip={ipv6}")
            }
            Prefs.ddnsProvider = p
            Prefs.ddnsUrl = u
            Prefs.ddnsToken = token?.trim() ?: Prefs.ddnsToken
        }
        Prefs.ddnsEnabled = enabled
        if (enabled) startWatch(App.ctx) else stopWatch()
        return ToolResult(true, text = buildString {
            append("DDNS 已配置：").append(nameOf(cfg())).append('\n')
            append("密钥：").append(if (Prefs.ddnsToken.isBlank()) "未配置" else "已保存（不回显）").append('\n')
            append(if (enabled) "自动发布：开（网络变化/地址变化时更新）" else "自动发布：关，需手动 self.ddns.publish")
        })
    }

    fun setWatch(on: Boolean): ToolResult {
        Prefs.ddnsEnabled = on
        if (on) {
            startWatch(App.ctx)
            return ToolResult(true, text = "DDNS 自动发布已开启；当前名字：${nameOf(cfg())}")
        }
        stopWatch()
        return ToolResult(true, text = "DDNS 自动发布已关闭（配置保留，可手动 publish）。")
    }

    fun statusText(): String {
        val c = cfg()
        val sb = StringBuilder()
        sb.append("DDNS：")
        when {
            c.provider == PROVIDER_OFF -> sb.append("未启用")
            !c.enabled -> sb.append("已配置但自动发布关闭")
            c.provider == PROVIDER_DUCK && (c.domain.isBlank() || c.token.isBlank()) ->
                sb.append("配置不完整（缺域名或 token）")
            c.provider == PROVIDER_CUSTOM && !c.url.contains("{ipv6}") ->
                sb.append("配置不完整（url 缺 {ipv6}）")
            else -> sb.append("运行中")
        }
        sb.append('\n')
        if (c.provider != PROVIDER_OFF) {
            sb.append("名字：").append(nameOf(c)).append('\n')
            sb.append("密钥：").append(if (c.token.isBlank()) "未配置" else "已配置（不回显）").append('\n')
            sb.append("上次发布：")
            if (Prefs.ddnsLastAt <= 0L) {
                sb.append("还没发布过\n")
            } else {
                val at = runCatching {
                    SimpleDateFormat("MM-dd HH:mm", Locale.US).format(Date(Prefs.ddnsLastAt))
                }.getOrDefault("")
                sb.append(at).append(" · ").append(Prefs.ddnsLastIp.ifBlank { "?" })
                    .append(" · ").append(Prefs.ddnsLastResult.ifBlank { "无回执" }).append('\n')
            }
        }
        val v6 = Dns6.globalV6()
        sb.append("本机全局 IPv6：")
        if (v6.isEmpty()) {
            sb.append("无（只有 fe80/ULA 时外网解析不到，先连 Wi-Fi 或蜂窝）\n")
        } else {
            sb.append('\n')
            v6.forEach { sb.append("  ").append(LanProto.bracket(it)).append('\n') }
        }
        sb.append("提醒：DDNS 只解决「找得到」，运营商拦入站时仍要走中继。")
        return sb.toString().trimEnd()
    }

    /**
     * 立刻发布当前全局 IPv6。
     * 成功只代表「DNS 那边收到了」，不代表运营商允许入站 —— 返回文案分开说。
     */
    @Synchronized
    fun publish(): ToolResult {
        val c = cfg()
        if (c.provider == PROVIDER_OFF) {
            return ToolResult(false, err = "DDNS 未启用：先跑 self.ddns.config provider=duckdns domain=... token=...")
        }
        if (!c.enabled) {
            return ToolResult(false, err = "DDNS 自动发布已关；先 self.ddns.watch enabled=true，或直接发布一次")
        }
        val ips = Dns6.globalV6()
        if (ips.isEmpty()) {
            return ToolResult(
                false,
                err = "本机没有全局 IPv6，发布没有意义。链路本地 fe80:: 不能被公网 DNS 指向；先连 Wi-Fi/蜂窝再试。",
            )
        }
        val ip = ips.first()
        val url = updateUrl(c, ip)
            ?: return ToolResult(false, err = "DDNS 配置不完整：检查域名/token，或自定义 url 是否含 {ipv6}")
        val (code, body) = try {
            httpGet(url)
        } catch (t: Throwable) {
            val msg = safe("${t.javaClass.simpleName}: ${t.message ?: "请求失败"}", c.token)
            Prefs.ddnsLastAt = System.currentTimeMillis()
            Prefs.ddnsLastResult = "失败：$msg"
            return ToolResult(false, err = "发布失败：$msg")
        }
        val ok = when (c.provider) {
            PROVIDER_DUCK -> code in 200..299 && body.trim().startsWith("OK", ignoreCase = true)
            else -> code in 200..299 &&
                !body.trim().startsWith("KO", ignoreCase = true) &&
                !body.trim().startsWith("error", ignoreCase = true)
        }
        val shown = safe(body.trim().take(120), c.token)
        Prefs.ddnsLastAt = System.currentTimeMillis()
        Prefs.ddnsLastResult = "HTTP $code ${shown.take(80)}"
        if (!ok) {
            // 失败不写 lastIp：否则 maybePublish 会以为「已发布过」，再也不重试。
            return ToolResult(false, err = "发布被拒绝：HTTP $code · $shown")
        }
        Prefs.ddnsLastIp = ip
        return ToolResult(true, text = buildString {
            append("已发布：").append(nameOf(c)).append(" → ").append(ip).append('\n')
            append("对端现在用 host=").append(nameOf(c)).append(" 即可，不用记 IP。\n")
            append("注意：这只保证解析到新地址；运营商若拦入站，仍需中继兜底。")
        })
    }

    // ---------------- 自动发布：网络变化 + 定期检查 ----------------

    private var scope: CoroutineScope? = null
    private var callback: ConnectivityManager.NetworkCallback? = null

    fun startWatch(ctx: Context) {
        if (!Prefs.ddnsEnabled) return
        if (scope != null) return
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return
        val s = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = s
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                s.launch { delay(3_000); maybePublish() }
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                    s.launch { delay(3_000); maybePublish() }
                }
            }
        }
        // 只关心「默认网络变了」，不逐张网卡注册；失败就退化成手动发布。
        runCatching {
            cm.registerNetworkCallback(
                NetworkRequest.Builder()
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    .build(),
                cb,
            )
            callback = cb
        }
        s.launch {
            while (isActive) {
                delay(10 * 60_000)
                maybePublish()
            }
        }
    }

    fun stopWatch() {
        callback?.let { cb ->
            runCatching {
                App.ctx.getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(cb)
            }
        }
        callback = null
        scope?.cancel()
        scope = null
    }

    /** 只在「开着 + 地址真变了」时发请求，避免每 10 分钟刷一次 DNS。 */
    private fun maybePublish() {
        runCatching {
            val c = cfg()
            if (!c.enabled || c.provider == PROVIDER_OFF) return
            val ip = Dns6.globalV6().firstOrNull() ?: return
            if (ip == Prefs.ddnsLastIp) return
            publish()
        }
    }

    private fun httpGet(url: String): Pair<Int, String> {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = 8_000
            readTimeout = 8_000
            requestMethod = "GET"
            instanceFollowRedirects = true
            setRequestProperty("User-Agent", "tianwang-next/1")
        }
        return try {
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val body = stream?.bufferedReader()?.use { it.readText().take(400) } ?: ""
            code to body
        } finally {
            conn.disconnect()
        }
    }

    private fun safe(text: String, token: String): String =
        if (token.isBlank()) text else text.replace(token, "<已隐藏>")

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")
}
