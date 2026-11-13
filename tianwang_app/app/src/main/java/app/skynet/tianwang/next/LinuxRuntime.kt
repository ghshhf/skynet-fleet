package app.skynet.tianwang.next

import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * 手机内置 Linux 运行时（自包含，不依赖 Termux / root / 电脑）。
 *
 * 对照 Horus / Forge / JCode 的形态：App 自己带一个 Linux 用户态。
 * 这里落成两层，状态永远说真话，不拿降级冒充完整发行版：
 *
 *  - 默认引擎 android-sh：Android 本身就是 Linux 内核，/system/bin/sh + toybox
 *    开箱可用，离线、零下载、零额外权限。跑 shell 脚本、文本处理、文件整理够了。
 *  - 增强引擎 alpine-proot：可选下载 Alpine arm64 minirootfs，用官方 .sha256
 *    校验后交给系统 tar 解到 App 私有目录。若 APK 带了 libproot.so（或用户导入了
 *    proot 二进制），就用 PRoot 把命令放进完整 Linux 用户态跑（python3 / node / apk）。
 *
 * 资源全部来自 Alpine 官方 CDN，校验值实取，不编造镜像。
 * 目录都在 App 私有 filesDir，卸载即清；不碰系统分区、不申请 root。
 */
object LinuxRuntime {
    const val ROOTFS_VERSION = "alpine-minirootfs-3.20.3-aarch64"
    const val ROOTFS_URL =
        "https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/alpine-minirootfs-3.20.3-aarch64.tar.gz"
    const val ROOTFS_SHA256_URL =
        "https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/aarch64/alpine-minirootfs-3.20.3-aarch64.tar.gz.sha256"
    const val ROOTFS_SHA256 =
        "041fa34a81788242df9e78fa69b97ab45b8ec47ddbf88864755610414a7bf3de"

    private const val K_READY = "linux.rootfs.ready"
    private const val K_SHA = "linux.rootfs.sha"
    private const val K_AT = "linux.rootfs.at"

    private fun runtimeDir(): File = File(App.ctx.filesDir, "linux").apply { mkdirs() }
    fun rootfsDir(): File = File(runtimeDir(), "rootfs")
    fun workspace(): File = File(runtimeDir(), "workspace").apply { mkdirs() }
    fun archive(): File = File(runtimeDir(), "$ROOTFS_VERSION.tar.gz")

    /** proot 只认两处：APK 的 nativeLibraryDir（只读可执行）或用户导入的私有 bin。 */
    fun prootBinary(): File? {
        val candidates = listOf(
            File(App.ctx.applicationInfo.nativeLibraryDir, "libproot.so"),
            File(runtimeDir(), "bin/proot"),
        )
        return candidates.firstOrNull { it.isFile && it.canExecute() }
    }

    fun rootfsReady(): Boolean =
        KB.str(K_READY, "0") == "1" &&
            (File(rootfsDir(), "bin").isDirectory || File(rootfsDir(), "etc").isDirectory)

    data class Status(
        val engine: String,
        val engineLabel: String,
        val rootfsReady: Boolean,
        val prootReady: Boolean,
        val archiveBytes: Long,
        val rootfsBytes: Long,
        val workspaceBytes: Long,
        val sha: String,
        val installedAt: Long,
        val note: String,
    )

    fun status(): Status {
        val rootfs = rootfsReady()
        val proot = prootBinary()
        val linux = rootfs && proot != null
        return Status(
            engine = if (linux) "alpine-proot" else "android-sh",
            engineLabel = if (linux) "Alpine Linux（PRoot）" else "Android Linux shell",
            rootfsReady = rootfs,
            prootReady = proot != null,
            archiveBytes = archive().takeIf { it.isFile }?.length() ?: 0L,
            rootfsBytes = dirSize(rootfsDir()),
            workspaceBytes = dirSize(workspace()),
            sha = KB.str(K_SHA, ""),
            installedAt = runCatching { KB.str(K_AT, "0").toLong() }.getOrDefault(0L),
            note = if (linux) {
                "完整 Linux 用户态已就绪：python3 / node / apk 走 PRoot。"
            } else if (rootfs) {
                "Linux 用户态已解压，但缺 proot 二进制；当前跑 Android shell。" +
                    "完整隔离需要把 libproot.so 打进 APK 或导入 proot。"
            } else {
                "当前跑 Android 自带 Linux shell，离线可用；装 Alpine 用户态可解锁更多工具。"
            },
        )
    }

    private fun dirSize(dir: File): Long {
        if (!dir.isDirectory) return 0L
        var sum = 0L
        dir.walkTopDown().forEach { if (it.isFile) sum += it.length() }
        return sum
    }

    /** 下载官方 minirootfs；进度回调给人看，失败返回 false 不抛。 */
    private fun download(report: (String) -> Unit): Boolean {
        val target = archive()
        val part = File(target.absolutePath + ".part")
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(ROOTFS_URL).openConnection() as HttpURLConnection).apply {
                connectTimeout = 20_000
                readTimeout = 30_000
                instanceFollowRedirects = true
                requestMethod = "GET"
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                report("下载失败：HTTP $code")
                return false
            }
            val total = conn.contentLengthLong
            conn.inputStream.use { input ->
                part.outputStream().use { out ->
                    val buf = ByteArray(16 * 1024)
                    var read: Int
                    var done = 0L
                    var lastPct = -1
                    while (input.read(buf).also { read = it } >= 0) {
                        out.write(buf, 0, read)
                        done += read
                        if (total > 0) {
                            val pct = (done * 100 / total).toInt()
                            if (pct != lastPct && pct % 10 == 0) {
                                lastPct = pct
                                report("下载中 $pct%（${done / 1024} KB）")
                            }
                        }
                    }
                }
            }
            if (part.length() < 100_000L) {
                report("下载内容过小（${part.length()} 字节），丢弃")
                part.delete()
                return false
            }
            if (target.exists()) target.delete()
            if (!part.renameTo(target)) {
                report("重命名失败")
                return false
            }
            report("下载完成：${target.length()} 字节")
            true
        } catch (t: Throwable) {
            report("下载异常：${t.javaClass.simpleName}: ${t.message}")
            false
        } finally {
            runCatching { conn?.disconnect() }
        }
    }

    private fun sha256(f: File): String = try {
        val md = MessageDigest.getInstance("SHA-256")
        FileInputStream(f).use { input ->
            val buf = ByteArray(16 * 1024)
            var n: Int
            while (input.read(buf).also { n = it } >= 0) md.update(buf, 0, n)
        }
        md.digest().joinToString("") { "%02x".format(it) }
    } catch (t: Throwable) {
        ""
    }

    /** 取官方 .sha256 首段做交叉核对（取不到就用内置实取值，不阻塞安装）。 */
    private fun officialSha(): String = try {
        val conn = (URL(ROOTFS_SHA256_URL).openConnection() as HttpURLConnection).apply {
            connectTimeout = 8_000
            readTimeout = 8_000
        }
        val text = conn.inputStream.bufferedReader().use { it.readText() }
        conn.disconnect()
        text.trim().split(Regex("\\s+")).firstOrNull()
            ?.takeIf { it.length == 64 } ?: ROOTFS_SHA256
    } catch (t: Throwable) {
        ROOTFS_SHA256
    }

    /** 一键安装：下载 → SHA-256 校验 → 系统 tar 解压 → 标记就绪。 */
    fun install(report: (String) -> Unit): Boolean {
        report("下载 Alpine Linux arm64 minirootfs…")
        if (!download(report)) return false

        report("校验 SHA-256…")
        val actual = sha256(archive())
        val expect = officialSha()
        if (actual.isBlank() || !actual.equals(expect, ignoreCase = true)) {
            report("校验不通过：实际 $actual / 期望 $expect，已停止，不解压")
            return false
        }

        report("校验通过，解压到 App 私有目录…")
        val dir = rootfsDir()
        dir.mkdirs()
        val out = ShellRunner.run(
            "tar -xzf '${archive().absolutePath}' -C '${dir.absolutePath}'",
            dir.absolutePath, 60_000,
        )
        if (!File(dir, "bin").isDirectory && !File(dir, "etc").isDirectory) {
            report("解压后没找到 bin/etc，失败：$out")
            return false
        }
        KB.setStr(K_READY, "1")
        KB.setStr(K_SHA, actual)
        KB.setStr(K_AT, System.currentTimeMillis().toString())
        report("Linux 用户态已就绪（${dirSize(dir) / 1024} KB）。")
        if (prootBinary() == null) {
            report("注意：尚未提供 proot，完整隔离暂不可用；默认仍跑 Android shell。")
        }
        return true
    }

    data class ExecResult(
        val engine: String,
        val output: String,
        val ok: Boolean,
        val ms: Long,
    )

    /**
     * 跑一条命令。先把命令落成脚本文件再执行，避免 shell 引号转义把用户代码弄坏。
     * 有 proot + rootfs 就进完整 Linux 用户态；否则退回 Android 自带 shell。
     */
    fun exec(command: String, cwd: File? = null, timeoutMs: Int = 15_000): ExecResult {
        val started = System.currentTimeMillis()
        val ws = workspace()
        val dir = (cwd?.takeIf { it.isDirectory } ?: ws).absolutePath
        val script = File(ws, ".run-${System.currentTimeMillis()}.sh")
        runCatching {
            script.writeText("#!/system/bin/sh\n$command\n", Charsets.UTF_8)
        }
        val proot = prootBinary()
        val linux = proot != null && rootfsReady()
        val raw = if (linux) {
            val cmd = "${proot!!.absolutePath} --kill-on-exit --link2symlink -0 " +
                "-r '${rootfsDir().absolutePath}' " +
                "-b '${ws.absolutePath}':/workspace -b /dev -b /proc -b /sys " +
                "-w /workspace /bin/sh '/workspace/${script.name}'"
            ShellRunner.run(cmd, dir, timeoutMs)
        } else {
            ShellRunner.run("sh '${script.absolutePath}'", dir, timeoutMs)
        }
        runCatching { script.delete() }
        return ExecResult(
            engine = if (linux) "alpine-proot" else "android-sh",
            output = raw,
            ok = raw.startsWith("exit=0"),
            ms = System.currentTimeMillis() - started,
        )
    }

    /** 只删下载包，不碰工作区里人写的文件。 */
    fun clearArchive(): Boolean {
        val ok = !archive().exists() || archive().delete()
        return ok
    }

    /** 彻底重置运行时（含工作区），UI 必须二次确认后才调。 */
    fun resetAll(): Boolean {
        KB.setStr(K_READY, "0")
        KB.setStr(K_SHA, "")
        KB.setStr(K_AT, "0")
        return runCatching { runtimeDir().deleteRecursively() }.isSuccess
    }
}
