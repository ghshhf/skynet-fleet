package app.skynet.tianwang.next

import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

/**
 * 长驻 shell 会话。
 *
 * 与 [ShellRunner] 的区别（这也是「工具箱」和「一台能用的机器」的分界线）：
 * - **stdin 可写**：`apk add`、`read`、`ftp login` 这类等输入的程序能继续往下走，不再立刻收到 EOF；
 * - **输出流式**：边跑边取增量，不必等进程退出；
 * - **不截断**：单个会话保留最近 256KB，超出才从头部丢，并且**丢多少如实报**；
 * - **长驻**：一个会话可以连着喂很多条命令，工作目录、环境变量、后台进程都保留；
 * - **可中断**：`Ctrl-C` 走写入 `\u0003`（mksh 解释），kill 才是真杀。
 *
 * 诚实说明：Android 没有开放 `forkpty`，所以这里默认是**管道会话（tty=false）**。
 * 管道会话能写 stdin、能流式读，但 `isatty(0)` 为假的程序会走非交互分支（比如部分
 * 程序会拒绝彩色输出或直接报错）。若系统自带 toybox `script`，可以开 [Mode.PTY]，
 * 由 `script` 去分配真 pty —— 探测不到就如实回落管道模式，不假装自己有 tty。
 */
object TtySession {

    /** 会话后端。PTY 需要系统有 `script`，探测不到就回落 PIPE。 */
    enum class Mode(val label: String) {
        PIPE("管道（tty=false）"),
        PTY("伪终端（tty=true，经 script）"),
    }

    /** 单个会话输出保留上限（字符）。超出从头部丢，并记下丢了多少。 */
    const val MAX_KEEP = 256 * 1024

    /** 同时存活会话上限，防止把手机内存吃满。 */
    const val MAX_SESSIONS = 4

    private class Sess(
        val id: String,
        val mode: Mode,
        val cwd: String,
        val proc: Process,
        val startedAt: Long,
    ) {
        val lock = Any()

        /** 完整输出尾部；[base] 是已经从头丢掉的字符数。 */
        val buf = StringBuilder()
        var base = 0L
        var total = 0L
        var exit: Int? = null
        var closed = false
    }

    private val sessions = ConcurrentHashMap<String, Sess>()
    private val seq = AtomicLong(0)

    /** `script` 在部分 ROM 上是软链到 toybox，能执行才认为可用。 */
    fun ptyAvailable(): Boolean = runCatching {
        val f = File("/system/bin/script")
        f.isFile && f.canExecute()
    }.getOrDefault(false)

    fun supportedModes(): String =
        if (ptyAvailable()) "PIPE / PTY（已探测到 /system/bin/script）"
        else "PIPE（未探测到 script，真 tty 不可用，如实说明）"

    /** 开一个会话。返回 id；失败抛异常，由调用方转成失败回包。 */
    fun open(cwd: String, wantPty: Boolean): String {
        if (sessions.size >= MAX_SESSIONS) {
            throw IllegalStateException("已有 $MAX_SESSIONS 个会话在跑，先关掉一个（self_tty_close）再开")
        }
        val dir = cwd.trim().takeIf { it.isNotEmpty() }
            ?.let { File(it) }?.takeIf { it.isDirectory }
            ?: File(App.ctx.filesDir.absolutePath)

        val usePty = wantPty && ptyAvailable()
        // `-i` 让 mksh 出提示符、不当成一段脚本跑完就退；没有 tty 也照跑，只是不回显输入。
        val cmd = if (usePty) {
            listOf("/system/bin/script", "-q", "-c", "/system/bin/sh -i", "/dev/null")
        } else {
            listOf("/system/bin/sh", "-i")
        }
        val proc = ProcessBuilder(cmd)
            .redirectErrorStream(true)
            .apply { directory(dir) }
            .start()

        val id = "s" + seq.incrementAndGet()
        val s = Sess(id, if (usePty) Mode.PTY else Mode.PIPE, dir.absolutePath, proc, System.currentTimeMillis())
        sessions[id] = s

        Thread {
            runCatching {
                proc.inputStream.bufferedReader().use { r ->
                    val buf = CharArray(4096)
                    while (true) {
                        val n = r.read(buf)
                        if (n < 0) break
                        synchronized(s.lock) {
                            s.buf.appendRange(buf, 0, n)
                            s.total += n
                            // 超上限从头部丢，丢掉的字符数记进 base，读的时候能如实报缺。
                            val over = s.buf.length - MAX_KEEP
                            if (over > 0) {
                                s.buf.delete(0, over)
                                s.base += over
                            }
                        }
                    }
                }
            }
            synchronized(s.lock) {
                s.exit = runCatching { proc.exitValue() }.getOrNull()
            }
        }.apply { isDaemon = true; name = "tty-$id"; start() }

        return id
    }

    fun write(id: String, text: String, newline: Boolean): Boolean {
        val s = sessions[id] ?: return false
        return runCatching {
            val payload = if (newline && !text.endsWith("\n")) "$text\n" else text
            synchronized(s.lock) {
                s.proc.outputStream.write(payload.toByteArray())
                s.proc.outputStream.flush()
                // 自己回显：管道没有 tty，程序不会把输入打回来，不回显人看着像没反应。
                s.buf.append(payload)
                s.total += payload.length
            }
            true
        }.getOrDefault(false)
    }

    /** 读增量。[since] 是上一轮的 cursor；小于已丢弃区间时如实标 dropped。 */
    fun read(id: String, since: Long, maxChars: Int): Read? {
        val s = sessions[id] ?: return null
        val cap = maxChars.coerceIn(200, 32_000)
        synchronized(s.lock) {
            val from = since.coerceAtLeast(s.base)
            val startIdx = (from - s.base).toInt().coerceIn(0, s.buf.length)
            var text = s.buf.substring(startIdx)
            var cursor = s.total
            val truncated = text.length > cap
            if (truncated) {
                // 从尾部留，保证「最新看到」，丢的头部用 cursor 偏差体现。
                text = text.takeLast(cap)
                cursor = s.total
            }
            return Read(
                id = id,
                mode = s.mode,
                text = text,
                cursor = cursor,
                running = !s.closed && s.exit == null && s.proc.isAlive,
                exit = s.exit,
                droppedBefore = if (since < s.base) s.base - since else 0L,
                truncated = truncated,
                cwd = s.cwd,
            )
        }
    }

    fun close(id: String): Boolean {
        val s = sessions[id] ?: return false
        synchronized(s.lock) { s.closed = true }
        // 先按「正常终止」走：SIGTERM 之后等一会儿，仍不退才强杀。不默默强杀。
        runCatching { s.proc.destroy() }
        val gone = runCatching { s.proc.waitFor(1, java.util.concurrent.TimeUnit.SECONDS) }.getOrDefault(false)
        if (!gone) runCatching { s.proc.destroyForcibly() }
        sessions.remove(id)
        return true
    }

    fun list(): List<Info> = sessions.values.sortedBy { it.startedAt }.map { s ->
        synchronized(s.lock) {
            Info(
                id = s.id,
                mode = s.mode,
                cwd = s.cwd,
                running = !s.closed && s.exit == null && s.proc.isAlive,
                exit = s.exit,
                total = s.total,
                kept = s.buf.length.toLong(),
                startedAt = s.startedAt,
                tail = s.buf.takeLast(400).toString(),
            )
        }
    }

    /** 进程退出后会话还留着，方便把最后一段输出取干净。 */
    fun closeAll(): Int {
        val ids = sessions.keys.toList()
        ids.forEach { close(it) }
        return ids.size
    }

    data class Read(
        val id: String,
        val mode: Mode,
        val text: String,
        val cursor: Long,
        val running: Boolean,
        val exit: Int?,
        val droppedBefore: Long,
        val truncated: Boolean,
        val cwd: String,
    )

    data class Info(
        val id: String,
        val mode: Mode,
        val cwd: String,
        val running: Boolean,
        val exit: Int?,
        val total: Long,
        val kept: Long,
        val startedAt: Long,
        val tail: String,
    )
}
