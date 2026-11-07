package app.skynet.tianwang.next

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Executors
import org.json.JSONObject

/**
 * 局域网 / IPv6 直连服务端。
 *
 * 协议故意很薄：一行一个 JSON-RPC，和云中继完全同一套 [McpCore]，只是换了传输层。
 * 大流量（测速、将来的文件）走同一条连接上的二进制段，不套 Base64、不套 JSON。
 *
 * 安全：首帧必须带口令；口令错就直接断，不回「差一点」之类的细节。
 * 只监听不鉴权的端口会被同一 Wi-Fi 下任何设备扫到，所以默认端口不固定写在公网。
 */
class LanServer(
    private val token: String,
    private val requestedPort: Int = LanProto.DEFAULT_PORT,
    private val deviceName: String = "phone",
    private val meta: () -> JSONObject = { JSONObject() },
    private val handler: (String) -> String?,
) {
    @Volatile
    var lastError: String = ""
        private set

    @Volatile
    var boundPort: Int = 0
        private set

    @Volatile
    var boundHost: String = ""
        private set

    @Volatile
    var connections: Int = 0
        private set

    private var server: ServerSocket? = null
    private var acceptThread: Thread? = null
    private val pool = Executors.newCachedThreadPool { r ->
        Thread(r, "skynet-lan-conn").apply { isDaemon = true }
    }

    @Synchronized
    fun start(): Boolean {
        if (server != null) return true
        val ss = bind(requestedPort) ?: return false
        server = ss
        boundPort = ss.localPort
        boundHost = ss.inetAddress?.hostAddress ?: ""
        acceptThread = Thread({
            while (!ss.isClosed) {
                val s = try {
                    ss.accept()
                } catch (t: Throwable) {
                    break
                }
                connections++
                pool.execute {
                    try {
                        serve(s)
                    } catch (t: Throwable) {
                        lastError = "连接处理失败：${t.javaClass.simpleName}: ${t.message ?: ""}"
                    } finally {
                        runCatching { s.close() }
                    }
                }
            }
        }, "skynet-lan-accept").apply {
            isDaemon = true
            start()
        }
        return true
    }

    /**
     * 优先绑 IPv6 通配地址（Android 上通常是 dual-stack，v4 也能进）；
     * 内核关了 IPv6 时退回 v4 通配，并在 [boundHost] 里如实报。
     */
    private fun bind(port: Int): ServerSocket? {
        val v6 = runCatching {
            ServerSocket().apply {
                reuseAddress = true
                bind(InetSocketAddress(InetAddress.getByName("::"), port), 32)
            }
        }.getOrNull()
        if (v6 != null) return v6
        val v4 = runCatching {
            ServerSocket().apply {
                reuseAddress = true
                bind(InetSocketAddress(port), 32)
            }
        }.getOrNull()
        if (v4 == null) lastError = "端口 $port 绑定失败（被占用或系统拒绝）"
        return v4
    }

    fun stop() {
        runCatching { server?.close() }
        server = null
        acceptThread?.interrupt()
        acceptThread = null
        runCatching { pool.shutdownNow() }
        boundPort = 0
    }

    private fun serve(socket: Socket) {
        socket.tcpNoDelay = true
        runCatching {
            socket.sendBufferSize = 1024 * 1024
            socket.receiveBufferSize = 1024 * 1024
        }
        socket.soTimeout = 30_000
        val input = BufferedInputStream(socket.getInputStream(), 64 * 1024)
        val output = BufferedOutputStream(socket.getOutputStream(), 64 * 1024)

        val first = readLine(input) ?: return
        val head = runCatching { JSONObject(first) }.getOrNull()
        if (head == null) {
            errorLine(output, "首帧不是 JSON")
            return
        }
        if (!LanProto.tokenEquals(head.optString("token"), token)) {
            errorLine(output, "口令不对")
            return
        }
        when (head.optString("lan")) {
            "speed" -> {
                speed(head, input, output)
                return
            }
            "hello" -> {
                val reply = JSONObject()
                    .put("lan", "ok")
                    .put("name", deviceName)
                    .put("proto", 1)
                    .put("ipv6", LanProto.localAddresses().any { it.host.contains(':') })
                // 自述字段可以被 meta 补，但不能覆盖 lan/name/proto 这些协议字段。
                runCatching { meta() }.getOrNull()?.let { extra ->
                    val keys = extra.keys()
                    while (keys.hasNext()) {
                        val k = keys.next()
                        if (!reply.has(k)) reply.put(k, extra.get(k))
                    }
                }
                writeLine(output, reply.toString())
            }
            else -> {
                errorLine(output, "首帧必须是 hello 或 speed")
                return
            }
        }

        while (true) {
            val line = readLine(input) ?: break
            if (line.isBlank()) continue
            // 测速可以复用已鉴权的连接：hello 之后直接发一条 speed 头，后面跟二进制。
            val probe = runCatching { JSONObject(line) }.getOrNull()
            if (probe?.optString("lan") == "speed") {
                speed(probe, input, output)
                return
            }
            val reply = try {
                handler(line)
            } catch (t: Throwable) {
                JSONObject()
                    .put("jsonrpc", "2.0")
                    .put("id", JSONObject.NULL)
                    .put("error", JSONObject()
                        .put("code", -32603)
                        .put("message", "手机内部错误：${t.javaClass.simpleName}"))
                    .toString()
            } ?: continue
            writeLine(output, reply)
        }
    }

    /**
     * 一通到底的测速段：`up` 是对端往本机灌，`down` 是本机往对端灌。
     * 只测 TCP 有效载荷，不把 JSON/Base64 的开销算进去。
     */
    private fun speed(head: JSONObject, input: InputStream, output: OutputStream) {
        val bytes = head.optInt("bytes", 0).coerceIn(1, LanProto.MAX_SPEED_BYTES)
        val down = head.optString("dir") == "down"
        val buf = ByteArray(64 * 1024)
        val started = System.nanoTime()

        if (down) {
            writeLine(output, JSONObject().put("lan", "speed").put("ok", true).put("bytes", bytes).toString())
            var left = bytes
            while (left > 0) {
                val n = minOf(left, buf.size)
                output.write(buf, 0, n)
                left -= n
            }
            output.flush()
        } else {
            var left = bytes
            while (left > 0) {
                val n = input.read(buf, 0, minOf(left, buf.size))
                if (n < 0) break
                left -= n
            }
        }

        val ms = (System.nanoTime() - started) / 1_000_000.0
        writeLine(output, JSONObject()
            .put("lan", "speed")
            .put("ok", true)
            .put("bytes", bytes)
            .put("ms", ms)
            .put("human", LanProto.speedLine(bytes, ms))
            .toString())
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream(256)
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() == 0) null else buf.toString("UTF-8")
            if (b == '\n'.code) return buf.toString("UTF-8")
            if (buf.size() >= LanProto.MAX_LINE) return null
            buf.write(b)
        }
    }

    private fun writeLine(output: OutputStream, line: String) {
        output.write(line.toByteArray(Charsets.UTF_8))
        output.write('\n'.code)
        output.flush()
    }

    private fun errorLine(output: OutputStream, message: String) {
        writeLine(output, JSONObject().put("lan", "error").put("err", message).toString())
    }
}
