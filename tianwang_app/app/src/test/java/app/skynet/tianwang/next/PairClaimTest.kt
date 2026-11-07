package app.skynet.tianwang.next

import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import kotlin.concurrent.thread

/**
 * 配对换长期凭据：请求体字段、成功解析、以及三类失败必须给出可读原因。
 * 用裸 ServerSocket 起一个一次性 HTTP 服务 —— Android 单测的 classpath 里
 * 没有 jdk.httpserver 模块，不能图省事用 com.sun.net.httpserver。
 */
class PairClaimTest {
    private lateinit var server: ServerSocket
    private lateinit var worker: Thread
    private var lastBody = ""
    private var replyCode = 200
    private var replyBody = ""

    @Before
    fun setUp() {
        server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        worker = thread(isDaemon = true, name = "pair-claim-stub") {
            try {
                server.accept().use { s ->
                    val input = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8))
                    input.readLine()                       // 请求行
                    var length = 0
                    while (true) {
                        val line = input.readLine() ?: break
                        if (line.isEmpty()) break
                        if (line.startsWith("Content-Length:", ignoreCase = true)) {
                            length = line.substringAfter(':').trim().toIntOrNull() ?: 0
                        }
                    }
                    val sb = StringBuilder()
                    var remaining = length
                    while (remaining > 0) {
                        val buf = CharArray(minOf(remaining, 4096))
                        val n = input.read(buf)
                        if (n <= 0) break
                        sb.append(String(buf, 0, n))
                        remaining -= n
                    }
                    lastBody = sb.toString()
                    val out = s.getOutputStream()
                    val bytes = replyBody.toByteArray(Charsets.UTF_8)
                    out.write(
                        ("HTTP/1.1 $replyCode STUB\r\n" +
                            "Content-Type: application/json\r\n" +
                            "Content-Length: ${bytes.size}\r\n" +
                            "Connection: close\r\n\r\n").toByteArray(Charsets.UTF_8)
                    )
                    out.write(bytes)
                    out.flush()
                }
            } catch (_: IOException) {
                // tearDown 会关掉 server，accept 抛异常属正常
            }
        }
    }

    @After
    fun tearDown() {
        runCatching { server.close() }
        worker.join(2_000)
    }

    private fun base(): String = "http://127.0.0.1:${server.localPort}"

    @Test
    fun successReturnsDeviceIdAndSecret() {
        replyBody = """{"ok":true,"device_id":"dev-1","secret":"s3cr3t"}"""
        val r = runBlocking { Net.claim(base(), "123456", "phone-test") }
        assertEquals("dev-1", r.deviceId)
        assertEquals("s3cr3t", r.secret)

        val sent = JSONObject(lastBody)
        assertEquals("123456", sent.getString("code"))
        assertEquals("phone-test", sent.getString("name"))
        assertEquals("android", sent.getString("platform"))
    }

    @Test
    fun httpErrorCarriesStatusAndBody() {
        replyCode = 403
        replyBody = """{"ok":false,"err":"pair-code-rejected"}"""
        val e = assertThrows(IOException::class.java) {
            runBlocking { Net.claim(base(), "123456", "phone-test") }
        }
        assertTrue(e.message!!.contains("403"))
        assertTrue(e.message!!.contains("pair-code-rejected"))
    }

    @Test
    fun okFalseUsesServerErr() {
        replyBody = """{"ok":false,"err":"pair-code-expired"}"""
        val e = assertThrows(IOException::class.java) {
            runBlocking { Net.claim(base(), "123456", "phone-test") }
        }
        assertTrue(e.message!!.contains("pair-code-expired"))
    }

    @Test
    fun missingCredentialsIsRejected() {
        replyBody = """{"ok":true,"device_id":"dev-1"}"""
        val e = assertThrows(IOException::class.java) {
            runBlocking { Net.claim(base(), "123456", "phone-test") }
        }
        assertTrue(e.message!!.contains("device_id/secret"))
    }
}
