package app.skynet.tianwang.next

import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedReader
import java.io.File
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.ServerSocket
import java.nio.file.Files
import kotlin.concurrent.thread

/**
 * 链接下载的硬规矩：文件名归一、.part 不冒充成品、体积/魔数不对必须失败。
 * 用裸 ServerSocket 起一次性 HTTP 服务，不依赖真网。
 */
class ModelDownloadTest {
    private lateinit var server: ServerSocket
    private lateinit var dir: File
    private var replyCode = 200
    private var replyBody = ByteArray(0)

    @Before
    fun setUp() {
        server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        dir = Files.createTempDirectory("tw-models").toFile()
        thread(isDaemon = true, name = "model-download-stub") {
            try {
                server.accept().use { s ->
                    val input = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8))
                    while (true) {
                        val line = input.readLine() ?: break
                        if (line.isEmpty()) break
                    }
                    val out = s.getOutputStream()
                    out.write(
                        ("HTTP/1.1 $replyCode STUB\r\n" +
                            "Content-Type: application/octet-stream\r\n" +
                            "Content-Length: ${replyBody.size}\r\n" +
                            "Connection: close\r\n\r\n").toByteArray(Charsets.UTF_8)
                    )
                    out.write(replyBody)
                    out.flush()
                }
            } catch (_: IOException) {
            }
        }
    }

    @After
    fun tearDown() {
        runCatching { server.close() }
        dir.deleteRecursively()
    }

    private fun base(): String = "http://127.0.0.1:${server.localPort}"

    @Test
    fun filenameFromUrlHandlesQueryAndEncoding() {
        assertEquals(
            "Qwen2.5-0.5B-Q4_K_M.gguf",
            ModelDownload.filenameFromUrl(
                "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/Qwen2.5-0.5B-Q4_K_M.gguf?download=true"
            ),
        )
        assertEquals(
            "my model.gguf",
            ModelDownload.filenameFromUrl("https://example.com/a/my%20model.gguf"),
        )
        assertEquals(
            "weights.gguf",
            ModelDownload.filenameFromUrl("https://example.com/weights"),
        )
    }

    @Test
    fun downloadsAndKeepsGgufFile() {
        replyBody = "GGUFhello!!!".toByteArray(Charsets.UTF_8)
        val f = runBlocking {
            ModelDownload.fetch(base() + "/tiny.gguf", dir, "tiny.gguf").getOrThrow()
        }
        assertEquals("tiny.gguf", f.name)
        assertTrue(f.exists())
        assertTrue(String(f.readBytes(), 0, 4, Charsets.UTF_8) == "GGUF")
        assertFalse(File(dir, "tiny.gguf.part").exists())
    }

    @Test
    fun rejectsNonGgufAndLeavesNoFile() {
        replyBody = "NOPEnot-a-model".toByteArray(Charsets.UTF_8)
        val e = assertThrows(IOException::class.java) {
            runBlocking {
                ModelDownload.fetch(base() + "/fake.gguf", dir, "fake.gguf").getOrThrow()
            }
        }
        assertTrue(e.message!!.contains("GGUF"))
        assertFalse(File(dir, "fake.gguf").exists())
        assertFalse(File(dir, "fake.gguf.part").exists())
    }

    @Test
    fun rejectsSizeMismatchBeforeWriting() {
        replyBody = "GGUFsmall".toByteArray(Charsets.UTF_8)
        val e = assertThrows(IOException::class.java) {
            runBlocking {
                ModelDownload.fetch(
                    base() + "/small.gguf", dir, "small.gguf", expectedBytes = 999_999L,
                ).getOrThrow()
            }
        }
        assertTrue(e.message!!.contains("体积对不上"))
        assertFalse(File(dir, "small.gguf").exists())
    }

    @Test
    fun rejectsNonHttpScheme() {
        val e = assertThrows(IOException::class.java) {
            runBlocking {
                ModelDownload.fetch("ftp://example.com/x.gguf", dir).getOrThrow()
            }
        }
        assertTrue(e.message!!.contains("http/https"))
    }
}
