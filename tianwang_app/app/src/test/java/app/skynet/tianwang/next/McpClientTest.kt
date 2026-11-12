package app.skynet.tianwang.next

import okhttp3.OkHttpClient
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.BufferedInputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * 用裸 ServerSocket 起一个最小 HTTP 假服务端（不依赖 com.sun.net.httpserver，
 * 那个包在 Android 单测的编译类路径里不可见），把握手、session、SSE、
 * 错误映射四条路径在 JVM 里跑通。
 */
class McpClientTest {

    private var fake: FakeServer? = null

    @After
    fun stop() {
        fake?.close()
    }

    private fun json(id: Any?, key: String, value: Any): String =
        JSONObject().put("jsonrpc", "2.0").put("id", id).put(key, value).toString()

    private data class Reply(
        val code: Int,
        val body: String,
        val session: String? = null,
        val sse: Boolean = false,
    )

    private class State {
        val inits = AtomicInteger()
        @Volatile var sawSessionOnList = false
        @Volatile var auth = ""
        @Volatile var dropNextList = false
    }

    private fun handler(
        state: State,
        sse: Boolean = false,
        failCall: Boolean = false,
        delayMs: Long = 0,
    ): (JSONObject, Map<String, String>) -> Reply = { req, headers ->
        if (delayMs > 0) Thread.sleep(delayMs)
        val id = req.opt("id")
        when (req.optString("method")) {
            "initialize" -> {
                state.inits.incrementAndGet()
                state.auth = headers["authorization"].orEmpty()
                Reply(
                    200,
                    json(id, "result", JSONObject()
                        .put("protocolVersion", "2025-06-18")
                        .put("capabilities", JSONObject())
                        .put("serverInfo", JSONObject().put("name", "fake"))),
                    session = "s1",
                )
            }
            "notifications/initialized" -> Reply(202, "")
            "tools/list" -> {
                state.sawSessionOnList = headers["mcp-session-id"] == "s1"
                if (state.dropNextList) {
                    state.dropNextList = false
                    Reply(400, """{"error":"session expired"}""")
                } else {
                    val tools = JSONArray().put(JSONObject()
                        .put("name", "search")
                        .put("description", "搜索")
                        .put("inputSchema", JSONObject().put("type", "object")))
                    Reply(200, json(id, "result", JSONObject().put("tools", tools)))
                }
            }
            "tools/call" -> {
                if (failCall) {
                    Reply(200, JSONObject().put("jsonrpc", "2.0").put("id", id)
                        .put("error", JSONObject().put("code", -32000).put("message", "工具炸了"))
                        .toString())
                } else {
                    val result = JSONObject().put("content", JSONArray().put(
                        JSONObject().put("type", "text").put("text", "done")
                    ))
                    if (sse) {
                        Reply(
                            200,
                            "event: message\ndata: " + json(id, "result", result) + "\n\n",
                            sse = true,
                        )
                    } else {
                        Reply(200, json(id, "result", result))
                    }
                }
            }
            else -> Reply(200, "{}")
        }
    }

    private fun start(h: (JSONObject, Map<String, String>) -> Reply): String {
        val s = FakeServer(h)
        fake = s
        return s.url
    }

    @Test
    fun handshakeListAndCall() {
        val state = State()
        val client = McpClient(start(handler(state)), token = "t0k")
        val tools = client.listTools()
        assertEquals(1, tools.size)
        assertEquals("search", tools.single().name)
        assertTrue("tools/list 必须带上 initialize 返回的 session", state.sawSessionOnList)
        assertEquals("Bearer t0k", state.auth)

        val result = client.callTool("search", JSONObject().put("q", "x"))
        assertTrue(result.ok)
        assertEquals("done", result.text)
    }

    @Test
    fun sseResponseIsParsed() {
        val state = State()
        val client = McpClient(start(handler(state, sse = true)))
        val result = client.callTool("search", JSONObject())
        assertTrue(result.ok)
        assertEquals("done", result.text)
    }

    @Test
    fun sessionExpiryReinitializesOnce() {
        val state = State()
        val client = McpClient(start(handler(state)))
        state.dropNextList = true
        val tools = client.listTools()
        assertEquals(1, tools.size)
        assertEquals("400 后应重新握手一次", 2, state.inits.get())
    }

    @Test
    fun jsonRpcErrorMessageSurfaces() {
        val state = State()
        val client = McpClient(start(handler(state, failCall = true)))
        try {
            client.callTool("search", JSONObject())
            fail("应该抛 McpCallException")
        } catch (e: McpCallException) {
            assertTrue("错误消息要带对端原文，实际：${e.message}", e.message!!.contains("工具炸了"))
        }
    }

    @Test
    fun httpErrorSurfacesStatusAndBody() {
        val client = McpClient(start { _, _ -> Reply(500, "boom") })
        try {
            client.listTools()
            fail("应该抛 McpCallException")
        } catch (e: McpCallException) {
            assertTrue("错误消息要带 HTTP 状态，实际：${e.message}", e.message!!.contains("HTTP 500"))
        }
    }

    @Test
    fun timeoutIsReported() {
        val state = State()
        val fast = OkHttpClient.Builder()
            .connectTimeout(1, TimeUnit.SECONDS)
            .readTimeout(200, TimeUnit.MILLISECONDS)
            .build()
        val client = McpClient(start(handler(state, delayMs = 1_000)), client = fast)
        try {
            client.listTools()
            fail("应该因超时抛 McpCallException")
        } catch (e: McpCallException) {
            assertTrue("超时要明确写出来，实际：${e.message}", e.message!!.contains("超时"))
        }
    }

    /** 最小 HTTP/1.1 服务端：一次连接一个请求，回 `Connection: close`。 */
    private class FakeServer(private val handler: (JSONObject, Map<String, String>) -> Reply) {
        private val server = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        val url: String = "http://127.0.0.1:${server.localPort}/mcp"

        init {
            Thread({ loop() }, "fake-mcp").apply {
                isDaemon = true
                start()
            }
        }

        fun close() {
            runCatching { server.close() }
        }

        private fun loop() {
            while (!server.isClosed) {
                val socket = try {
                    server.accept()
                } catch (t: Throwable) {
                    return
                }
                Thread({
                    socket.use { s ->
                        try {
                            s.soTimeout = 3_000
                            val input = BufferedInputStream(s.getInputStream())
                            val headers = readHeaders(input)
                            val length = headers["content-length"]?.toIntOrNull() ?: 0
                            val body = ByteArray(length)
                            var off = 0
                            while (off < length) {
                                val n = input.read(body, off, length - off)
                                if (n < 0) break
                                off += n
                            }
                            val request = JSONObject(String(body, Charsets.UTF_8))
                            val reply = handler(request, headers)
                            val payload = reply.body.toByteArray(Charsets.UTF_8)
                            val head = buildString {
                                append("HTTP/1.1 ").append(reply.code).append(" X\r\n")
                                append("Content-Type: ")
                                append(if (reply.sse) "text/event-stream" else "application/json")
                                append("\r\n")
                                reply.session?.let { append("Mcp-Session-Id: ").append(it).append("\r\n") }
                                append("Content-Length: ").append(payload.size).append("\r\n")
                                append("Connection: close\r\n\r\n")
                            }
                            val out = s.getOutputStream()
                            out.write(head.toByteArray(Charsets.UTF_8))
                            out.write(payload)
                            out.flush()
                        } catch (_: Throwable) {
                        }
                    }
                }, "fake-mcp-conn").apply {
                    isDaemon = true
                    start()
                }
            }
        }

        private fun readHeaders(input: BufferedInputStream): Map<String, String> {
            val map = HashMap<String, String>()
            val line = StringBuilder()
            while (true) {
                val b = input.read()
                if (b < 0) break
                if (b == '\r'.code) continue
                if (b == '\n'.code) {
                    if (line.isEmpty()) break
                    val text = line.toString()
                    val idx = text.indexOf(':')
                    if (idx > 0) {
                        map[text.substring(0, idx).trim().lowercase()] = text.substring(idx + 1).trim()
                    }
                    line.clear()
                } else {
                    line.append(b.toChar())
                }
            }
            return map
        }
    }
}
