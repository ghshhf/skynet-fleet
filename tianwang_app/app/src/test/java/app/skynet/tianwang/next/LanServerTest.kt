package app.skynet.tianwang.next

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 真开 socket 的集成测试：只绑回环，不碰 Wi-Fi。
 * 覆盖的是「口令 / 首帧 / 按行切帧 / 二进制测速段」这些真机上最容易拼错的地方。
 */
class LanServerTest {
    private val token = "test-token-0123456789abcdef"

    private fun withServer(
        handler: (String) -> String? = { line ->
            val id = runCatching { JSONObject(line).opt("id") }.getOrNull()
            JSONObject().put("jsonrpc", "2.0").put("id", id)
                .put("result", JSONObject().put("ok", true)).toString()
        },
        block: (LanServer) -> Unit,
    ) {
        val server = LanServer(token, 0, "测试机", handler)
        assertTrue("server 应能绑定回环端口：${server.lastError}", server.start())
        try {
            block(server)
        } finally {
            server.stop()
        }
    }

    private class Wire(socket: Socket) {
        val input: InputStream = BufferedInputStream(socket.getInputStream())
        val output: OutputStream = BufferedOutputStream(socket.getOutputStream())

        fun line(text: String) {
            output.write(text.toByteArray(Charsets.UTF_8))
            output.write('\n'.code)
            output.flush()
        }

        fun read(): String {
            val buf = java.io.ByteArrayOutputStream(128)
            while (true) {
                val b = input.read()
                if (b < 0) break
                if (b == '\n'.code) break
                buf.write(b)
            }
            return buf.toString("UTF-8")
        }
    }

    private fun connect(server: LanServer): Wire {
        val s = Socket()
        s.tcpNoDelay = true
        s.connect(InetSocketAddress(InetAddress.getLoopbackAddress(), server.boundPort), 3000)
        s.soTimeout = 5000
        return Wire(s)
    }

    @Test
    fun helloWithTokenIsAccepted() {
        withServer { server ->
            val w = connect(server)
            w.line(JSONObject().put("lan", "hello").put("token", token).toString())
            val reply = JSONObject(w.read())
            assertEquals("ok", reply.getString("lan"))
            assertEquals("测试机", reply.getString("name"))
        }
    }

    @Test
    fun wrongTokenIsRejected() {
        withServer { server ->
            val w = connect(server)
            w.line(JSONObject().put("lan", "hello").put("token", "nope").toString())
            val reply = JSONObject(w.read())
            assertEquals("error", reply.getString("lan"))
            assertTrue(reply.getString("err").contains("口令"))
        }
    }

    @Test
    fun mcpLineRoundTrip() {
        withServer { server ->
            val w = connect(server)
            w.line(JSONObject().put("lan", "hello").put("token", token).toString())
            assertEquals("ok", JSONObject(w.read()).getString("lan"))
            w.line("""{"jsonrpc":"2.0","id":7,"method":"ping"}""")
            val reply = JSONObject(w.read())
            assertEquals(7, reply.getInt("id"))
            assertTrue(reply.getJSONObject("result").getBoolean("ok"))
        }
    }

    @Test
    fun phoneMcpInitializeOverLan() {
        withServer(handler = PhoneMcp::handleLine) { server ->
            val w = connect(server)
            w.line(JSONObject().put("lan", "hello").put("token", token).toString())
            assertEquals("ok", JSONObject(w.read()).getString("lan"))
            w.line("""{"jsonrpc":"2.0","id":1,"method":"initialize"}""")
            val reply = JSONObject(w.read())
            assertEquals("phone-self", reply.getJSONObject("result")
                .getJSONObject("serverInfo").getString("name"))
        }
    }

    @Test
    fun speedUploadMeasuresPayload() {
        withServer { server ->
            val w = connect(server)
            val bytes = 256 * 1024
            w.line(JSONObject().put("lan", "speed").put("token", token)
                .put("bytes", bytes).put("dir", "up").toString())
            val buf = ByteArray(64 * 1024)
            var left = bytes
            while (left > 0) {
                val n = minOf(left, buf.size)
                w.output.write(buf, 0, n)
                left -= n
            }
            w.output.flush()
            val reply = JSONObject(w.read())
            assertEquals("speed", reply.getString("lan"))
            assertEquals(bytes, reply.getInt("bytes"))
            assertTrue(reply.getString("human").contains("MB/s"))
        }
    }

    @Test
    fun speedDownloadDeliversPayload() {
        withServer { server ->
            val w = connect(server)
            val bytes = 256 * 1024
            w.line(JSONObject().put("lan", "speed").put("token", token)
                .put("bytes", bytes).put("dir", "down").toString())
            val head = JSONObject(w.read())
            assertEquals("speed", head.getString("lan"))
            var got = 0
            val buf = ByteArray(64 * 1024)
            while (got < bytes) {
                val n = w.input.read(buf, 0, minOf(bytes - got, buf.size))
                if (n < 0) break
                got += n
            }
            assertEquals(bytes, got)
            val tail = JSONObject(w.read())
            assertEquals(bytes, tail.getInt("bytes"))
        }
    }

    @Test
    fun clientAcceptsHostnameAndResolvesIt() {
        withServer { server ->
            val r = LanClient.speed(
                LanProto.Target("localhost", server.boundPort), token, 1, false, 15_000,
            )
            assertTrue(r.err, r.ok)
            assertTrue("via=${r.via}", r.via.isNotBlank())
        }
    }

    @Test
    fun speedWithoutTokenIsRejected() {
        withServer { server ->
            val w = connect(server)
            w.line(JSONObject().put("lan", "speed").put("token", "bad")
                .put("bytes", 1024).put("dir", "up").toString())
            val reply = JSONObject(w.read())
            assertEquals("error", reply.getString("lan"))
        }
    }
}
