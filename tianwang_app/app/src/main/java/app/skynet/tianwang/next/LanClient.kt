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
    data class Reply(val ok: Boolean, val text: String, val err: String, val ms: Long)
    data class Speed(val ok: Boolean, val text: String, val err: String)

    private const val CONNECT_TIMEOUT_MS = 8_000

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
        if (!r.ok) return Reply(false, "", r.err, ms)
        if (outText.length > 8 * 1024 * 1024) {
            return Reply(true, outText.take(8 * 1024 * 1024) + "\n…（回包超过 8MB，已截断）", "", ms)
        }
        return Reply(true, outText, "", ms)
    }

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
        return if (r.ok) Speed(true, reported, "") else Speed(false, "", r.err)
    }

    private fun withSocket(
        target: LanProto.Target,
        token: String,
        timeoutMs: Int,
        block: (InputStream, OutputStream, Socket) -> Unit,
    ): Reply {
        val started = System.nanoTime()
        var socket: Socket? = null
        return try {
            val addr = InetAddress.getByName(target.host)
            val s = Socket()
            s.tcpNoDelay = true
            s.connect(InetSocketAddress(addr, target.port), CONNECT_TIMEOUT_MS)
            socket = s
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
            block(input, output, s)
            val ms = (System.nanoTime() - started) / 1_000_000
            Reply(true, "", "", ms)
        } catch (t: Throwable) {
            val hint = when {
                t is java.net.SocketTimeoutException -> "连接/读写超时（${timeoutMs}ms）"
                t is java.net.ConnectException -> "连不上 ${target.endpoint()}（端口没开或 Wi-Fi 隔离）"
                t is java.net.NoRouteToHostException -> "没有到 ${target.endpoint()} 的路由（链路本地 IPv6 要带 %网卡）"
                t is java.net.UnknownHostException -> "解析不了地址 ${target.host}"
                else -> "${t.javaClass.simpleName}: ${t.message ?: "连接失败"}"
            }
            Reply(false, "", hint, (System.nanoTime() - started) / 1_000_000)
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
