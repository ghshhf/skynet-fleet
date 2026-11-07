package app.skynet.tianwang.next

import android.content.Context
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.json.JSONObject

/**
 * 手机直连总开关：LAN 服务 + mDNS 发现 + 本机地址清单。
 *
 * 为什么不走云中继：同一 Wi-Fi 下链路本地 IPv6 是一跳直达，
 * 实测能跑满 Wi-Fi 的几百 Mbps；绕一圈中继只会把「手机控手机」变成慢动作。
 * 中继的角色只剩两个：跨网络找不到地址时的信令，和 IPv6 不可达时的兜底。
 */
object LanHub {
    @Volatile
    var running: Boolean = false
        private set

    @Volatile
    var port: Int = LanProto.DEFAULT_PORT
        private set

    @Volatile
    var lastError: String = ""
        private set

    @Volatile
    var startedAt: Long = 0L
        private set

    private var server: LanServer? = null
    private var discovery: LanDiscovery? = null

    fun start(ctx: Context, requestedPort: Int = Prefs.lanPort): ToolResult {
        lastError = ""
        val token = Prefs.ensureLanToken()
        if (running) {
            if (server?.boundPort == requestedPort) {
                return ToolResult(true, text = "局域网直连已在运行（端口 $port）。\n" + pairingText(token))
            }
            stop(ctx, keepService = true)
        }
        val s = LanServer(token, requestedPort, Prefs.deviceName) { PhoneMcp.handleLine(it) }
        if (!s.start()) {
            lastError = s.lastError.ifBlank { "端口绑定失败" }
            return ToolResult(false, err = "局域网直连启动失败：$lastError")
        }
        server = s
        port = s.boundPort
        running = true
        startedAt = System.currentTimeMillis()
        Prefs.lanPort = port

        val d = LanDiscovery(ctx, port, Prefs.deviceName, Prefs.deviceId)
        discovery = d
        val mdns = d.start()
        if (!mdns) lastError = d.lastError

        // 尽量把进程钉住；Android 12+ 后台启动前台服务可能被拒，失败不影响本次会话。
        runCatching { PhoneLinkService.start(ctx) }

        return ToolResult(true, text = buildString {
            append("局域网直连已启动：端口 $port\n")
            append(if (mdns) "mDNS 已广播 " else "mDNS 广播失败：${d.lastError}\n")
            append(pairingText(token))
        })
    }

    fun stop(ctx: Context? = null, keepService: Boolean = false) {
        discovery?.stop()
        discovery = null
        server?.stop()
        server = null
        running = false
        port = 0
        if (!keepService && ctx != null && !Prefs.mcpEnabled && !Prefs.ddnsEnabled) {
            runCatching { ctx.stopService(android.content.Intent(ctx, PhoneLinkService::class.java)) }
        }
    }

    fun statusText(): String {
        val sb = StringBuilder()
        if (running) {
            val at = runCatching {
                SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(startedAt))
            }.getOrDefault("")
            sb.append("局域网直连：运行中 · 端口 $port")
            if (at.isNotBlank()) sb.append(" · 启动于 $at")
            sb.append(" · 已接受 ${server?.connections ?: 0} 次连接\n")
            sb.append("监听：${server?.boundHost?.ifBlank { "::" }}（IPv6 绑定，通常 v4/v6 双栈）\n")
        } else {
            sb.append("局域网直连：未启动。跑 self.lan.start 开服务。\n")
        }
        sb.append("\n本机地址（IPv6 优先）：\n")
        val addrs = LanProto.localAddresses()
        if (addrs.isEmpty()) {
            sb.append("  （没有可入站地址：先连 Wi-Fi，或确认系统没关 IPv6）\n")
        } else {
            addrs.forEach { sb.append("  ").append(it.line()).append('\n') }
            val first = addrs.first()
            sb.append("  对端可连：").append(LanProto.bracket(first.host))
            if (running) sb.append(':').append(port) else sb.append("（端口待启动）")
            sb.append('\n')
        }
        sb.append("\n口令：").append(mask(Prefs.lanToken))
            .append("（完整口令只在 self.lan.start 回执里出现）\n")
        val peers = runCatching { discovery?.peerList() ?: emptyList() }.getOrDefault(emptyList())
        sb.append("发现：")
        if (discovery == null) {
            sb.append("未广播\n")
        } else {
            sb.append("mDNS ${LanProto.SERVICE_TYPE} · 已发现 ${peers.size} 台\n")
            peers.take(8).forEach {
                sb.append("  ").append(it.name).append(" · ")
                    .append(LanProto.bracket(it.host)).append(':').append(it.port)
                    .append(" · ").append(LanProto.familyLabel(it.host)).append('\n')
            }
        }
        if (lastError.isNotBlank()) sb.append("最近错误：").append(lastError).append('\n')
        if (Prefs.ddnsProvider != Ddns.PROVIDER_OFF) {
            sb.append("稳定名字：").append(Ddns.nameOf(Ddns.cfg()))
            if (Prefs.ddnsEnabled) sb.append("（自动发布）") else sb.append("（自动发布关闭）")
            sb.append('\n')
        }
        sb.append("\n提示：同一 Wi-Fi 优先走 fe80 链路本地（必须带 %网卡）；")
        sb.append("跨网络要求两端都有全局 IPv6，且运营商/路由器不拦入站。")
        return sb.toString().trimEnd()
    }

    fun call(args: Map<String, Any?>): ToolResult {
        val target = targetOf(args) ?: return ToolResult(false, err = "host 为空：填对端 self.lan.status 里的地址")
        val token = str(args["token"])
        if (token.isBlank()) {
            return ToolResult(false, err = "缺 token：在手机 B 上跑 self.lan.start，把回执里的完整口令填进来")
        }
        val tool = str(args["tool"]).replace('.', '_')
        if (tool.isBlank()) return ToolResult(false, err = "缺 tool：填 MCP 工具名，如 self_status / self_ui_dump")
        val argText = str(args["args"])
        val argObj = if (argText.isBlank()) JSONObject() else runCatching { JSONObject(argText) }.getOrNull()
            ?: return ToolResult(false, err = "args 不是合法 JSON 对象：${argText.take(80)}")
        val request = LanProto.toolRequest(tool, argObj)
        val r = LanClient.call(target, token, request, int(args["timeout"], 30_000).coerceIn(1_000, 120_000))
        if (!r.ok) return ToolResult(false, err = "直连 ${target.endpoint()} 失败：${r.err}")
        val text = LanProto.summarizeMcpReply(r.text)
        val via = if (r.via.isBlank()) "" else " · 经 ${r.via}"
        return ToolResult(true, text = "对端 ${target.endpoint()} · ${tool} · ${r.ms}ms$via\n$text")
    }

    fun speed(args: Map<String, Any?>): ToolResult {
        val target = targetOf(args) ?: return ToolResult(false, err = "host 为空：填对端 self.lan.status 里的地址")
        val token = str(args["token"])
        if (token.isBlank()) return ToolResult(false, err = "缺 token：在手机 B 上跑 self.lan.start 拿完整口令")
        val mb = int(args["mb"], 8).coerceIn(1, 64)
        val down = args["down"] == true || str(args["down"]) == "true"
        val r = LanClient.speed(target, token, mb * 1024 * 1024, down)
        if (!r.ok) return ToolResult(false, err = "测速 ${target.endpoint()} 失败：${r.err}")
        val via = if (r.via.isBlank()) "" else " · 经 ${r.via}"
        return ToolResult(
            true,
            text = "直连 ${target.endpoint()}$via · ${if (down) "下行" else "上行"} ${mb}MB · ${r.text}",
        )
    }

    private fun targetOf(args: Map<String, Any?>): LanProto.Target? {
        val raw = str(args["host"])
        if (raw.isBlank()) return null
        val port = int(args["port"], LanProto.DEFAULT_PORT)
        val parsed = LanProto.parse(raw, port) ?: return null
        // host 里已经写了端口就用它；否则用 port 参数。别让 `[fe80::1]:9000` 被默认端口覆盖。
        val explicit = raw.let { s ->
            (s.startsWith("[") && s.substringAfter(']', "").startsWith(":")) ||
                s.count { it == ':' } == 1
        }
        return LanProto.Target(parsed.host, if (explicit) parsed.port else port)
    }

    private fun pairingText(token: String): String = buildString {
        append("完整口令：").append(token).append('\n')
        append("对端在 self.lan.call / self.lan.speed 里填 host、port=$port、token=上面这串。\n")
        append("地址：\n")
        LanProto.localAddresses().take(6).forEach {
            append("  ").append(LanProto.bracket(it.host)).append(':').append(port)
                .append("  ").append(it.family.label).append('\n')
        }
        append("口令只在本机与对端之间用；不要发到群里或写进截图。")
    }

    private fun mask(token: String): String =
        if (token.length <= 8) "未设置" else token.take(4) + "…" + token.takeLast(4)

    private fun str(v: Any?): String = when (v) {
        null -> ""
        is String -> v.trim()
        else -> v.toString().trim()
    }

    private fun int(v: Any?, def: Int): Int = when (v) {
        is Number -> v.toInt()
        is String -> v.trim().toIntOrNull() ?: def
        else -> def
    }
}
