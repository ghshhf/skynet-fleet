package app.skynet.tianwang.next

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import org.json.JSONObject

/**
 * 手机直连客户端：连另一台手机的 [LanServer]。
 *
 * 先 hello 拿口令确认，再发一条 JSON-RPC；连接不重用 —— 手机之间的调用是低频的，
 * 为省一次握手去维护长连接池，反而会引入「看起来在线、其实半死」的假状态。
 */
object LanClient {
    data class Reply(
        val ok: Boolean,
        val text: String,
        val err: String,
        val ms: Long,
        val via: String = "",
        val hello: JSONObject? = null,
    )
    data class Speed(val ok: Boolean, val text: String, val err: String, val via: String = "")

    private const val CONNECT_TIMEOUT_MS = 8_000

    /** 单个地址的等待上限。域名可能同时有 v6/v4，别让一个坏地址把整次调用拖死。 */
    private const val PER_ADDR_TIMEOUT_MS = 4_000

    /**
     * 调对端一个 MCP 工具。[request] 是单行 JSON-RPC。
     * 回包可能带截屏图片（base64），所以上限放到 8MB，超过就明确截断。
     */
    fun call(
        target: LanProto.Target,
        token: String,
        request: String,
        timeoutMs: Int = 30_000,
    ): Reply {
        val started = System.nanoTime()
        var outText = ""
        val r = withSocket(target, token, timeoutMs) { input, output, _ ->
            writeLine(output, request)
            val line = readLine(input) ?: throw IllegalStateException("对端没回包（连接被关）")
            outText = line
            Unit
        }
        val ms = (System.nanoTime() - started) / 1_000_000
        if (!r.ok) return Reply(false, "", r.err, ms, r.via)
        if (outText.length > 8 * 1024 * 1024) {
            return Reply(true, outText.take(8 * 1024 * 1024) + "\n…（回包超过 8MB，已截断）", "", ms, r.via)
        }
        return Reply(true, outText, "", ms, r.via)
    }

    /**
     * 只握手：拿对端 hello 自述（电量/充电/工具表/动作开关），不发工具调用。
     * 集群选路前先 probe，避免把写操作派给一台只读的手机。
     */
    fun probe(target: LanProto.Target, token: String, timeoutMs: Int = 8_000): Reply =
        withSocket(target, token, timeoutMs) { _, _, _ -> }

    /**
     * IPv6 / Wi-Fi 有效载荷测速。默认上行（本机往对端灌），`down=true` 测下行。
     * 结果同时给 MB/s 和 Mbps —— 手机 WiFi 6 跑几百 Mbps 是正常的，不要按中继的体感设预期。
     */
    fun speed(
        target: LanProto.Target,
        token: String,
        bytes: Int,
        down: Boolean,
        timeoutMs: Int = 60_000,
    ): Speed {
        val n = bytes.coerceIn(1024, LanProto.MAX_SPEED_BYTES)
        var reported = ""
        val r = withSocket(target, token, timeoutMs) { input, output, socket ->
            socket.sendBufferSize = 1024 * 1024
            socket.receiveBufferSize = 1024 * 1024
            val head = JSONObject()
                .put("lan", "speed")
                .put("token", token)
                .put("bytes", n)
                .put("dir", if (down) "down" else "up")
                .toString()
            writeLine(output, head)
            val started = System.nanoTime()
            val buf = ByteArray(64 * 1024)
            if (down) {
                val ack = readLine(input) ?: throw IllegalStateException("对端没回测速头")
                val ackJson = JSONObject(ack)
                if (ackJson.optString("lan") == "error") throw IllegalStateException(ackJson.optString("err"))
                var left = n
                while (left > 0) {
                    val got = input.read(buf, 0, minOf(left, buf.size))
                    if (got < 0) break
                    left -= got
                }
            } else {
                var left = n
                while (left > 0) {
                    val k = minOf(left, buf.size)
                    output.write(buf, 0, k)
                    left -= k
                }
                output.flush()
            }
            val ms = (System.nanoTime() - started) / 1_000_000.0
            val tail = readLine(input) ?: ""
            reported = if (tail.isNotBlank()) {
                runCatching { JSONObject(tail).optString("human") }.getOrDefault("")
            } else ""
            if (reported.isBlank()) reported = LanProto.speedLine(n, ms)
        }
        return if (r.ok) Speed(true, reported, "", r.via) else Speed(false, "", r.err, r.via)
    }

    private fun withSocket(
        target: LanProto.Target,
        token: String,
        timeoutMs: Int,
        block: (InputStream, OutputStream, Socket) -> Unit,
    ): Reply {
        val started = System.nanoTime()
        var socket: Socket? = null
        var lastErr: Throwable? = null
        var lastAddr: InetAddress? = null
        var tried = 0
        var helloMeta: JSONObject? = null
        return try {
            val addrs = Dns6.resolve(target.host)
            for (addr in addrs) {
                tried++
                lastAddr = addr
                val cand = Socket()
                try {
                    cand.tcpNoDelay = true
                    cand.connect(
                        InetSocketAddress(addr, target.port),
                        minOf(CONNECT_TIMEOUT_MS, PER_ADDR_TIMEOUT_MS),
                    )
                    socket = cand
                    break
                } catch (t: Throwable) {
                    lastErr = t
                    runCatching { cand.close() }
                }
            }
            val s = socket ?: throw (lastErr ?: IllegalStateException("没有可用地址"))
            s.soTimeout = timeoutMs
            val input = BufferedInputStream(s.getInputStream(), 64 * 1024)
            val output = BufferedOutputStream(s.getOutputStream(), 64 * 1024)

            writeLine(output, JSONObject()
                .put("lan", "hello")
                .put("token", token)
                .toString())
            val hello = readLine(input) ?: throw IllegalStateException("对端没回 hello")
            val helloJson = JSONObject(hello)
            if (helloJson.optString("lan") != "ok") {
                throw IllegalStateException(helloJson.optString("err", "对端拒绝连接"))
            }
            helloMeta = helloJson
            block(input, output, s)
            val ms = (System.nanoTime() - started) / 1_000_000
            Reply(true, "", "", ms, s.inetAddress?.hostAddress ?: "", helloMeta)
        } catch (t: Throwable) {
            val via = socket?.inetAddress?.hostAddress
                ?: lastAddr?.hostAddress
                ?: target.host
            val hint = when {
                t is java.net.SocketTimeoutException -> "连接/读写超时（${timeoutMs}ms，经 $via）"
                t is java.net.ConnectException ->
                    if (tried > 1) {
                        "连不上 ${target.endpoint()}：试了 $tried 个地址都失败（最后 $via，端口没开或 Wi-Fi 隔离）"
                    } else {
                        "连不上 ${target.endpoint()}（经 $via，端口没开或 Wi-Fi 隔离）"
                    }
                t is java.net.NoRouteToHostException ->
                    "没有到 ${target.endpoint()} 的路由（经 $via；链路本地 IPv6 要带 %网卡）"
                t is java.net.UnknownHostException -> "解析不了地址 ${target.host}"
                else -> "${t.javaClass.simpleName}: ${t.message ?: "连接失败"}"
            }
            Reply(false, "", hint, (System.nanoTime() - started) / 1_000_000, via, helloMeta)
        } finally {
            runCatching { socket?.close() }
        }
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream(256)
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() == 0) null else buf.toString("UTF-8")
            if (b == '\n'.code) return buf.toString("UTF-8")
            if (buf.size() >= LanProto.MAX_LINE) throw IllegalStateException("对端回包超过 256KB")
            buf.write(b)
        }
    }

    private fun writeLine(output: OutputStream, line: String) {
        output.write(line.toByteArray(Charsets.UTF_8))
        output.write('\n'.code)
        output.flush()
    }
}
