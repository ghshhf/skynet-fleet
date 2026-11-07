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
        val s = LanServer(
            token, requestedPort, Prefs.deviceName,
            meta = { LanMeta.hello() },
            handler = { PhoneMcp.handleLine(it) },
        )
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

    // ---------------- 手机集群：名册 / 探测 / 选路 / 派单 ----------------

    fun peers(): List<PeerBook.Peer> = PeerBook.decode(Prefs.fleetPeers)

    private fun savePeers(list: List<PeerBook.Peer>) {
        Prefs.fleetPeers = PeerBook.encode(list)
    }

    fun peerAdd(args: Map<String, Any?>): ToolResult {
        val name = str(args["name"])
        if (name.isBlank()) return ToolResult(false, err = "缺 name：给这台对端起个短名，如 b-phone")
        val token = str(args["token"])
        if (token.isBlank()) {
            return ToolResult(false, err = "缺 token：在手机 B 上跑 self.lan.start，把回执里的完整口令填进来")
        }
        val t = targetOf(args) ?: return ToolResult(
            false,
            err = "host 为空或格式不对：支持 <域名>、<IPv4>、<IPv6>、[<IPv6>]:端口",
        )
        val list = peers()
        val old = PeerBook.find(list, name)
        val p = (old ?: PeerBook.Peer(name = name, host = t.host, port = t.port))
            .copy(name = name, host = t.host, port = t.port, token = token, lastError = "")
        savePeers(PeerBook.upsert(list, p))
        return ToolResult(true, text = buildString {
            append(if (old == null) "已加入集群：" else "已更新：").append(name)
                .append(" · ").append(t.endpoint()).append('\n')
            append("口令：").append(PeerBook.mask(token)).append('\n')
            append("下一步跑 self.peer.probe 拿电量与工具表，再 self.peer.pick / self.peer.call。")
        })
    }

    fun peerRemove(args: Map<String, Any?>): ToolResult {
        val name = str(args["name"])
        if (name.isBlank()) return ToolResult(false, err = "缺 name")
        val list = peers()
        if (PeerBook.find(list, name) == null) return ToolResult(false, err = "名册里没有 $name")
        savePeers(PeerBook.remove(list, name))
        return ToolResult(true, text = "已移出集群：$name（只删本机名册，不影响对端）")
    }

    fun peerList(): ToolResult {
        val list = peers()
        val sb = StringBuilder()
        if (list.isEmpty()) {
            sb.append("集群名册为空。\n")
            sb.append("在 B 机跑 self.lan.start 拿地址与完整口令，然后在 A 机：\n")
            sb.append("  self.peer.add name=b-phone host=<B 的地址> token=<口令>\n")
        } else {
            sb.append("集群名册：").append(list.size).append(" 台\n\n")
            list.forEach { sb.append(PeerBook.line(it)).append("\n\n") }
            sb.append("写操作只派给 self.peer.probe 后 actions=允许 的机器；失败不自动换机重试。")
        }
        val found = runCatching { discovery?.peerList() ?: emptyList() }.getOrDefault(emptyList())
        val unknown = found.filter { f -> list.none { it.name.equals(f.name, true) } }
        if (unknown.isNotEmpty()) {
            sb.append("\n\nmDNS 发现但未入册：\n")
            unknown.take(8).forEach {
                sb.append("  ").append(it.name).append(" · ")
                    .append(LanProto.bracket(it.host)).append(':').append(it.port).append('\n')
            }
            sb.append("  入册需要它的口令：self.peer.add name=... host=... token=...")
        }
        return ToolResult(true, text = sb.toString().trimEnd())
    }

    fun peerProbe(): ToolResult {
        val list = peers()
        if (list.isEmpty()) return ToolResult(false, err = "集群名册为空，先 self.peer.add")
        val out = StringBuilder()
        var good = 0
        val updated = list.map { p ->
            val r = LanClient.probe(LanProto.Target(p.host, p.port), p.token)
            if (!r.ok) {
                out.append("✗ ").append(p.name).append("：").append(r.err).append('\n')
                p.copy(lastError = r.err)
            } else {
                good++
                val u = applyHello(p, r.hello)
                out.append("✓ ").append(PeerBook.line(u)).append("\n\n")
                u
            }
        }
        savePeers(updated)
        return ToolResult(
            good > 0,
            text = out.toString().trimEnd(),
            err = if (good == 0) "没有一台能握手成功" else "",
        )
    }

    fun peerPick(args: Map<String, Any?>): ToolResult {
        val tool = str(args["tool"]).replace('.', '_')
        if (tool.isBlank()) return ToolResult(false, err = "缺 tool：填 MCP 工具名，如 self_ui_tap")
        val meta = PhoneMcp.toolList().firstOrNull { it.name == tool }
            ?: return ToolResult(false, err = "本机没有这个工具：$tool")
        val list = peers()
        if (list.isEmpty()) return ToolResult(false, err = "集群名册为空，先 self.peer.add")
        val ranked = PeerBook.rank(list, tool, meta.sideEffect, int(args["min_battery"], 15))
        if (ranked.isEmpty()) {
            return ToolResult(false, err = buildString {
                append("没有能接 $tool 的机器。")
                if (meta.sideEffect) append("写操作要求对端 actions=允许，先 self.peer.probe。")
                else append("先 self.peer.probe 确认在线与工具表。")
            })
        }
        val sb = StringBuilder()
        sb.append("按 suitability 排序（").append(tool)
        if (meta.sideEffect) sb.append(" · 写操作，只派一台且失败不重试")
        sb.append("）：\n")
        ranked.forEachIndexed { i, p ->
            sb.append(i + 1).append(". ").append(p.name).append(" · ").append(p.endpoint())
            sb.append(" · ").append(if (p.battery < 0) "电量未知" else "${p.battery}%")
            if (p.charging) sb.append("（充电中）")
            sb.append(if (p.actions) " · 可写" else " · 只读").append('\n')
        }
        return ToolResult(true, text = sb.toString().trimEnd())
    }

    fun peerCall(args: Map<String, Any?>): ToolResult {
        val tool = str(args["tool"]).replace('.', '_')
        if (tool.isBlank()) return ToolResult(false, err = "缺 tool：填 MCP 工具名")
        val meta = PhoneMcp.toolList().firstOrNull { it.name == tool }
            ?: return ToolResult(false, err = "本机没有这个工具：$tool")
        val argText = str(args["args"])
        val argObj = if (argText.isBlank()) JSONObject() else runCatching { JSONObject(argText) }.getOrNull()
            ?: return ToolResult(false, err = "args 不是合法 JSON 对象：${argText.take(80)}")
        val all = peers()
        if (all.isEmpty()) return ToolResult(false, err = "集群名册为空，先 self.peer.add")

        val named = str(args["name"])
        val candidates = if (named.isNotBlank()) {
            val one = PeerBook.find(all, named)
                ?: return ToolResult(false, err = "名册里没有 $named")
            listOf(one)
        } else {
            PeerBook.rank(all, tool, meta.sideEffect, int(args["min_battery"], 15))
        }
        if (candidates.isEmpty()) {
            return ToolResult(false, err = "没有能接 $tool 的机器；先 self.peer.probe，写操作还要对端 actions=允许")
        }
        // 写操作只派一台：回包丢了不代表没执行，换机重试就是重复点击/重复发送。
        val plan = if (meta.sideEffect) candidates.take(1) else candidates
        val timeout = int(args["timeout"], 30_000).coerceIn(1_000, 120_000)
        val request = LanProto.toolRequest(tool, argObj)
        val tried = StringBuilder()
        plan.forEachIndexed { i, p ->
            val r = LanClient.call(LanProto.Target(p.host, p.port), p.token, request, timeout)
            if (r.ok) {
                touch(p, r)
                val via = if (r.via.isBlank()) "" else " · 经 ${r.via}"
                return ToolResult(true, text = buildString {
                    append("由 ").append(p.name).append(" 完成（第 ").append(i + 1)
                        .append(" 个候选）· ").append(tool).append(" · ").append(r.ms).append("ms")
                        .append(via).append('\n')
                    append(LanProto.summarizeMcpReply(r.text))
                })
            }
            touch(p, r)
            tried.append("✗ ").append(p.name).append("：").append(r.err).append('\n')
            if (meta.sideEffect) {
                return ToolResult(false, err = buildString {
                    append("已派给 ").append(p.name).append(" 但没拿到回包：").append(r.err).append('\n')
                    append("写操作可能已经执行。按规则不自动换机重试；先人工确认对端状态。")
                })
            }
            if (i == plan.lastIndex) {
                return ToolResult(false, err = "全部候选失败：\n" + tried.toString().trimEnd())
            }
        }
        return ToolResult(false, err = "没有执行任何候选")
    }

    private fun touch(p: PeerBook.Peer, r: LanClient.Reply) {
        val cur = peers()
        val old = PeerBook.find(cur, p.name) ?: p
        val u = if (r.ok) applyHello(old, r.hello) else old.copy(lastError = r.err)
        savePeers(PeerBook.upsert(cur, u))
    }

    private fun applyHello(p: PeerBook.Peer, hello: JSONObject?): PeerBook.Peer {
        if (hello == null) return p.copy(lastSeen = System.currentTimeMillis(), lastError = "")
        val tools = HashSet<String>()
        hello.optJSONArray("tools")?.let { arr ->
            for (i in 0 until arr.length()) {
                val v = arr.optString(i).trim()
                if (v.isNotEmpty()) tools.add(v)
            }
        }
        return p.copy(
            battery = hello.optInt("battery", p.battery).coerceIn(-1, 100),
            charging = hello.optBoolean("charging", p.charging),
            screenOn = hello.optBoolean("screenOn", p.screenOn),
            caps = hello.optInt("caps", p.caps),
            tools = if (tools.isEmpty()) p.tools else tools,
            actions = hello.optBoolean("actions", p.actions),
            platform = hello.optString("platform", p.platform),
            android = hello.optString("android", p.android),
            lastSeen = System.currentTimeMillis(),
            lastError = "",
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
