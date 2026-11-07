package app.skynet.tianwang.next

import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 在 shell 身份下跑一条命令。
 *
 * 单独抽出来是为了能在 JVM 单测里真跑 `sh`，把「超时杀进程」「输出截断」这些
 * 只能靠真机才发现的行为先钉住。调用方只负责把结果转成人能看懂的回包。
 */
object ShellRunner {
    /** 回给控制面的输出上限。一条 `dumpsys` 能有几 MB，不截断会把 MCP 一帧撑爆。 */
    const val MAX_OUTPUT = 8 * 1024

    fun run(cmd: String, cwd: String, timeoutMs: Int): String {
        val timeout = timeoutMs.coerceIn(200, 60_000)
        val dir = cwd.trim().takeIf { it.isNotEmpty() }
            ?.let { File(it) }?.takeIf { it.isDirectory }
        val process = try {
            ProcessBuilder("sh", "-c", cmd)
                .redirectErrorStream(true)
                .apply { if (dir != null) directory(dir) }
                .start()
        } catch (t: Throwable) {
            return "启动失败：${t.javaClass.simpleName}: ${t.message}"
        }
        // 不写 stdin；有些命令会一直等输入，关掉立刻收到 EOF。
        runCatching { process.outputStream.close() }

        val out = StringBuilder()
        val reader = Thread {
            runCatching {
                process.inputStream.bufferedReader().use { r ->
                    val buf = CharArray(4096)
                    while (true) {
                        val n = r.read(buf)
                        if (n < 0) break
                        synchronized(out) {
                            // 留一倍余量给后面的截断提示，避免无限膨胀。
                            if (out.length < MAX_OUTPUT * 2) out.appendRange(buf, 0, n)
                        }
                    }
                }
            }
        }.apply { isDaemon = true; start() }

        val finished = runCatching {
            process.waitFor(timeout.toLong(), TimeUnit.MILLISECONDS)
        }.getOrDefault(false)
        if (!finished) {
            process.destroy()
            if (!runCatching { process.waitFor(2, TimeUnit.SECONDS) }.getOrDefault(false)) {
                process.destroyForcibly()
            }
        }
        runCatching { reader.join(1_000) }

        val text = synchronized(out) { out.toString() }
        val head = if (finished) {
            "exit=${runCatching { process.exitValue() }.getOrDefault(-1)}"
        } else {
            "超时 ${timeout}ms，已终止"
        }
        if (text.isBlank()) return head
        val body = if (text.length > MAX_OUTPUT) {
            text.take(MAX_OUTPUT) + "\n…（输出超过 $MAX_OUTPUT 字符，已截断）"
        } else {
            text
        }
        return "$head\n${body.trimEnd()}"
    }
}
