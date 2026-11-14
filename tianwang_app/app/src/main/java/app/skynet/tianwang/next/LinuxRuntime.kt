package app.skynet.tianwang.next

import android.os.Build
import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * 手机内置 Linux 运行时（自包含，不依赖 Termux / root / 电脑）。
 *
 * 对照 Horus / Forge / JCode 的形态：App 自己带一个 Linux 用户态。
 * 关键原则（主人定）：**环境不封装死**，运行时自己探测、自己选，状态永远说真话。
 *
 *  - 架构自适应：从 [Build.SUPPORTED_ABIS] 选 Alpine 支持的架构（aarch64 / x86_64 / armv7），
 *    不再写死 aarch64 —— 在 x86_64 模拟器或 32 位机上也能装对应镜像。
 *  - 版本可配置：默认稳定系列 3.20.3，可用 KB 覆盖，不把版本烤进代码。
 *  - 镜像多源：官方 CDN + 清华 + 阿里云三处 fallback，某源不可达自动切下一个，不绑死一个地址。
 *  - 空间探测：装之前先看剩余空间，不够直接拒，不让用户下载到一半失败。
 *  - proot 来源如实报：APK 内置 / 用户导入 / 无，三者分明，不假设一定在。
 *  - 命令可用性探测：android 层与完整发行版内的命令分开看，不假装「装了 proot 就有 python」。
 *
 * 两层，状态永远说真话，不拿降级冒充完整发行版：
 *
 *  - 默认引擎 android-sh：Android 本身就是 Linux 内核，/system/bin/sh + toybox
 *    开箱可用，离线、零下载、零额外权限。跑 shell 脚本、文本处理、文件整理够了。
 *  - 增强引擎 alpine-proot：下载 Alpine minirootfs，用官方 .sha256 校验后交给系统 tar 解到
 *    App 私有目录。若提供了 proot 二进制，就用 PRoot 把命令放进完整 Linux 用户态跑。
 *    Alpine 默认不含 python3 / node，需要联网 `apk add`，runtime 会如实提示，不冒充。
 *
 * 资源全部来自 Alpine 官方或国内镜像，校验值实取，不编造镜像；未知架构的内置校验值留空，
 * 联网取不到官方值就拒装（宁可让用户联网，也不拿假校验放行）。
 */
object LinuxRuntime {
    /** 稳定发布系列；镜像路径用 v$RELEASE。 */
    private const val RELEASE = "3.20"

    /** 默认 rootfs 版本；可用 [version] 经 KB 覆盖，不烤进代码。 */
    private const val DEFAULT_VERSION = "3.20.3"

    /** 镜像源：官方 CDN 优先，清华 / 阿里云兜底。任一不可达自动切下一个。 */
    private val MIRRORS = listOf(
        "dl-cdn.alpinelinux.org",
        "mirrors.tuna.tsinghua.edu.cn/alpine",
        "mirrors.aliyun.com/alpine",
    )

    /** 已知实取校验值（仅本机核实过的架构）；其余架构联网取官方值，取不到就拒装，不编造。 */
    private val KNOWN_SHA = mapOf(
        "aarch64" to "041fa34a81788242df9e78fa69b97ab45b8ec47ddbf88864755610414a7bf3de",
    )

    /** ABI → Alpine 架构，按 Android 常见优先级排列（x86 32 位 Alpine 不再提供，排除）。 */
    private val ABI_TO_ARCH = listOf(
        "arm64-v8a" to "aarch64",
        "x86_64" to "x86_64",
        "armeabi-v7a" to "armv7",
    )

    /** 安装预估占用（rootfs 解压后约 5-8MB，但留足缓冲 + 工作区）。 */
    private const val NEED_BYTES = 48L * 1024 * 1024 * 1024

    private const val K_READY = "linux.rootfs.ready"
    private const val K_SHA = "linux.rootfs.sha"
    private const val K_AT = "linux.rootfs.at"
    private const val K_VER = "linux.rootfs.version"
    private const val K_CMDS = "linux.cmds"
    private const val K_CMDS_AT = "linux.cmds.at"
    private const val K_CMDS_CC = "linux.cmds.cc"     // 探测计数
    private const val K_CMDS_LAST = "linux.cmds.last"  // 最后探测时间戳
    private const val K_PROBE_SUCC = "linux.probe.succ" // 探测成功计数
    private const val K_PROBE_FAIL = "linux.probe.fail" // 探测失败计数
    private const val K_PROBE_FRESH = "linux.probe.fresh" // 是否显式刷新

    /** 当前选用的 rootfs 版本（可被用户/远端配置覆盖）。 */
    fun version(): String = KB.str(K_VER, DEFAULT_VERSION).ifBlank { DEFAULT_VERSION }

    /** 探测本机首个受支持的 Alpine 架构；都不支持返回 null。 */
    fun detectArch(): String? {
        val abis = Build.SUPPORTED_ABIS.toList()
        for ((abi, arch) in ABI_TO_ARCH) {
            if (abis.contains(abi)) return arch
        }
        return null
    }

    private fun runtimeDir(): File = File(App.ctx.filesDir, "linux").apply { mkdirs() }
    fun rootfsDir(): File = File(runtimeDir(), "rootfs")
    fun workspace(): File = File(runtimeDir(), "workspace").apply { mkdirs() }
    fun archive(): File = File(runtimeDir(), "minirootfs-${version()}.tar.gz")

    /** 当前拟用的镜像地址（展示用，自适应结果）。 */
    fun currentRootfsUrl(): String {
        val arch = detectArch() ?: "aarch64"
        return rootfsUrl(arch, version(), MIRRORS.first())
    }

    private fun rootfsUrl(arch: String, ver: String, mirror: String): String =
        "https://$mirror/alpine/v$RELEASE/releases/$arch/alpine-minirootfs-$ver-$arch.tar.gz"

    private fun rootfsShaUrl(arch: String, ver: String, mirror: String): String =
        rootfsUrl(arch, ver, mirror) + ".sha256"

    enum class ProotSource { APK, IMPORTED, NONE, DISCOVERED }

    /** 探测已下载的 rootfs 目录中是否存在 proot binary（子目录 bin/ 与 lib/）。 */
    private fun discoverProotInDir(dir: File): Boolean {
        if (!dir.isDirectory) return false
        // 检查 bin/ 目录
        val binDir = File(dir, "bin")
        if (binDir.isDirectory) {
            val prootInBin = binDir.listFiles()?.firstOrNull { it.name.startsWith("proot") && it.canExecute() }
            if (prootInBin != null) return true
        }
        // 检查 lib/ 目录（部分打包方式会放 lib/）
        val libDir = File(dir, "lib")
        if (libDir.isDirectory) {
            val prootInLib = libDir.listFiles()?.firstOrNull { it.name.startsWith("proot") && it.canExecute() }
            if (prootInLib != null) return true
        }
        // 检查最顶层（有的压包直接把二进制放根目录）
        if (dir.canExecute() && !dir.name.startsWith(".")) {
            if (dir.listFiles()?.any { it.name.startsWith("proot") && it.canExecute() } == true) return true
        }
        return false
    }

    /** proot 来源：APK 内置 / 用户导入 / 已下载 rootfs 中自动发现 / 无。 */
    fun prootSource(): ProotSource {
        val apk = File(App.ctx.applicationInfo.nativeLibraryDir, "libproot.so")
        if (apk.isFile && apk.canExecute()) return ProotSource.APK
        val imported = File(runtimeDir(), "bin/proot")
        if (imported.isFile && imported.canExecute()) return ProotSource.IMPORTED
        // 尝试从已下载的 minirootfs 中发现
        val rootfsDir = rootfsDir()
        if (rootfsDir.isDirectory && discoverProotInDir(rootfsDir)) return ProotSource.DISCOVERED
        return ProotSource.NONE
    }

    /** proot 二进制；无则返回 null（引擎退回 android-sh）。 */
    fun prootBinary(): File? = when (prootSource()) {
        ProotSource.APK -> File(App.ctx.applicationInfo.nativeLibraryDir, "libproot.so")
        ProotSource.IMPORTED -> File(runtimeDir(), "bin/proot")
        ProotSource.NONE -> null
    }

    fun rootfsReady(): Boolean =
        KB.str(K_READY, "0") == "1" &&
            (File(rootfsDir(), "bin").isDirectory || File(rootfsDir(), "etc").isDirectory)

    data class Status(
        val engine: String,
        val engineLabel: String,
        val arch: String,
        val archSupported: Boolean,
        val spaceFreeBytes: Long,
        val rootfsReady: Boolean,
        val prootReady: Boolean,
        val prootSource: String,
        val mirror: String,
        val rootfsUrl: String,
        val available: Map<String, Boolean>,
        val archiveBytes: Long,
        val rootfsBytes: Long,
        val workspaceBytes: Long,
        val sha: String,
        val installedAt: Long,
        val note: String,
        /** 最后一次探测的时间戳（Unix 毫秒） */
        val lastProbeAt: Long,
        /** 探测成功计数（命令至少有一条可用） */
        val probeSuccessCount: Int,
        /** 探测失败计数（上一次探测中没有任何命令可用） */
        val probeFailCount: Int,
    }

    /** 探测 android 层可用命令（计数 + 显式刷新），不执行完整发行版内的探测以免拖慢。 */
    private fun probeAndroidCommands(): Map<String, Boolean> {
        val cached = KB.str(K_CMDS, "")
        val cnt = runCatching { KB.str(K_CMDS_CC, "0").toInt() }.getOrDefault(0)
        val last = runCatching { KB.str(K_CMDS_LAST, "0").toLong() }.getOrDefault(0L)
        // 若本轮已显式刷新或距离上次探测超过了阈值（可配），则重新探测
        val shouldRefresh = KB.str(K_PROBE_FRESH, "0").toBoolean()
        if (!shouldRefresh && cached.isNotBlank() && cnt > 0 && System.currentTimeMillis() - last < 300_000) {
            // 缓存有效：返回已有的，并累加计数（防止同一轮多次调用导致计数异常）
            return cached.split(',').mapNotNull { it.split('=').takeIf { it.size == 2 } }
                .associate { it[0] to (it[1] == "1") }
        }
        val cmds = listOf("sh", "ls", "cat", "grep", "awk", "sed", "tar", "curl", "python3", "node")
        val script = cmds.joinToString("; ") { "command -v $it >/dev/null 2>&1 && echo '$it=1' || echo '$it=0'" }
        val out = ShellRunner.run(script, runtimeDir().absolutePath, 8_000)
        val map = cmds.associateWith { false }.toMutableMap()
        out.lineSequence().forEach { line ->
            val p = line.trim().split('=')
            if (p.size == 2) map[p[0]] = p[1] == "1"
        }
        // 写入缓存：键值对、本轮计数 +1、最后探测时间、探测成功/失败计数
        val newEntries = map.map { "${it.key}=${if (it.value) 1 else 0}" }.joinToString(",")
        KB.setStr(K_CMDS, newEntries)
        KB.setStr(K_CMDS_CC, (cnt + 1).toString())
        KB.setStr(K_CMDS_LAST, System.currentTimeMillis().toString())
        // 若本次探测有至少有一条命令可用，记为成功；否则记为失败
        val hasAny = map.values.any { it }
        KB.setStr(K_PROBE_SUCC, if (hasAny) (runCatching { KB.str(K_PROBE_SUCC, "0").toInt() + 1 }.getOrDefault(0) else runCatching { KB.str(K_PROBE_SUCC, "0").toInt() })
        KB.setStr(K_PROBE_FAIL, if (!hasAny) (runCatching { KB.str(K_PROBE_FAIL, "0").toInt() + 1 }.getOrDefault(0) else runCatching { KB.str(K_PROBE_FAIL, "0").toInt() })
        return map
    }

    /** 显式刷新命令探测缓存（由 AI 或手动调用，始终重新探测）。 */
    fun refreshProbe(): Map<String, Boolean> = probeAndroidCommands(forceRefresh = true).also {
        KB.setStr(K_PROBE_FRESH, "1")
    }

    fun status(): Status {
        val arch = detectArch()
        val archSupported = arch != null
        val rootfs = rootfsReady()
        val proot = prootBinary()
        val linux = rootfs && proot != null
        val cmds = probeAndroidCommands()
        val note = buildNote(archSupported, rootfs, proot, linux, cmds)
        val mirror = MIRRORS.first()
        val url = rootfsUrl(arch ?: "aarch64", version(), mirror)
        // 探测相关状态
        val lastProbe = runCatching { KB.str(K_CMDS_LAST, "0").toLong() }.getOrDefault(0L)
        val succCnt = runCatching { KB.str(K_PROBE_SUCC, "0").toInt() }.getOrDefault(0)
        val failCnt = runCatching { KB.str(K_PROBE_FAIL, "0").toInt() }.getOrDefault(0)
        return Status(
            engine = if (linux) "alpine-proot" else "android-sh",
            engineLabel = if (linux) "Alpine Linux（PRoot）" else "Android Linux shell",
            arch = arch ?: "unknown",
            archSupported = archSupported,
            spaceFreeBytes = runtimeDir().getFreeSpace(),
            rootfsReady = rootfs,
            prootReady = proot != null,
            prootSource = when (prootSource()) {
                ProotSource.APK -> "APK 内置 libproot.so"
                ProotSource.IMPORTED -> "已导入（runtime/bin/proot）"
                ProotSource.NONE -> "无"
            },
            mirror = mirror,
            rootfsUrl = url,
            available = cmds,
            archiveBytes = archive().takeIf { it.isFile }?.length() ?: 0L,
            rootfsBytes = dirSize(rootfsDir()),
            workspaceBytes = dirSize(workspace()),
            sha = KB.str(K_SHA, ""),
            installedAt = runCatching { KB.str(K_AT, "0").toLong() }.getOrDefault(0L),
            note = note,
            lastProbeAt = lastProbe,
            probeSuccessCount = succCnt,
            probeFailCount = failCnt,
        )
    }

    private fun buildNote(
        archSupported: Boolean,
        rootfs: Boolean,
        proot: File?,
        linux: Boolean,
        cmds: Map<String, Boolean>,
    ): String {
        if (!archSupported) {
            return "本机架构不在 Alpine 支持范围（需 arm64-v8a / x86_64 / armeabi-v7a）。" +
                "当前只能跑 Android 自带 shell，无法装入完整 Linux 用户态。"
        }
        return when {
            linux -> {
                val py = if (cmds["python3"] == true) "有" else "需 apk add python3"
                val nd = if (cmds["node"] == true) "有" else "需 apk add nodejs"
                "完整 Linux 用户态已就绪（PRoot：${proot!!.name}）。" +
                    "python3 $py；node $nd（Alpine 默认不含，首次需联网装）。"
            }
            rootfs -> {
                "Linux 用户态已解压，但缺 proot 二进制（当前 ${prootSource().name}）。" +
                    "完整隔离需把 libproot.so 打进 APK，或在「代码工作台→运行时」导入 proot；" +
                    "当前退回 Android shell。"
            }
            else -> {
                "当前跑 Android 自带 Linux shell（${if (cmds["sh"] == true) "sh 可用" else "sh 缺失"}），离线可用；" +
                    "装 Alpine 用户态可解锁完整发行版与 python3 / node（需联网 apk add）。"
            }
        }
    }

    private fun dirSize(dir: File): Long {
        if (!dir.isDirectory) return 0L
        var sum = 0L
        dir.walkTopDown().forEach { if (it.isFile) sum += it.length() }
        return sum
    }

    /** 下载：依次尝试所有镜像源，第一个成功即返回；全部失败报最后错误。 */
    private fun download(report: (String) -> Unit): Boolean {
        val arch = detectArch() ?: return false
        val ver = version()
        val target = archive()
        val part = File(target.absolutePath + ".part")
        var lastErr = "无可用镜像"
        for (mirror in MIRRORS) {
            val url = rootfsUrl(arch, ver, mirror)
            report("尝试镜像源：$mirror …")
            try {
                if (tryDownload(url, part, report)) {
                    if (target.exists()) target.delete()
                    if (!part.renameTo(target)) {
                        report("重命名失败，改用直接落地")
                        part.copyTo(target, overwrite = true)
                        part.delete()
                    }
                    report("下载完成（来自 $mirror）：${target.length()} 字节")
                    return true
                }
            } catch (t: Throwable) {
                lastErr = "${t.javaClass.simpleName}: ${t.message}"
            }
            part.delete()
        }
        report("所有镜像源下载失败：$lastErr")
        return false
    }

    private fun tryDownload(url: String, part: File, report: (String) -> Unit): Boolean {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = 20_000
                readTimeout = 30_000
                instanceFollowRedirects = true
                requestMethod = "GET"
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                report("  $url → HTTP $code")
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
                return false
            }
            true
        } catch (t: Throwable) {
            report("  $url → ${t.javaClass.simpleName}: ${t.message}")
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

    /** 取官方 .sha256 首段做交叉核对；依次尝试所有镜像，取不到返回 null（不编造）。 */
    private fun officialSha(): String? {
        val arch = detectArch() ?: return null
        val ver = version()
        for (mirror in MIRRORS) {
            try {
                val conn = (URL(rootfsShaUrl(arch, ver, mirror)).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 8_000
                    readTimeout = 8_000
                }
                val text = conn.inputStream.bufferedReader().use { it.readText() }
                conn.disconnect()
                val sha = text.trim().split(Regex("\\s+")).firstOrNull()?.takeIf { it.length == 64 }
                if (sha != null) return sha
            } catch (t: Throwable) {
                // 试下一个镜像
            }
        }
        return null
    }

    /** 一键安装：探测架构/空间 → 多源下载 → SHA-256 校验 → 系统 tar 解压 → 标记就绪。 */
    fun install(report: (String) -> Unit): Boolean {
        val arch = detectArch()
        if (arch == null) {
            report("本机架构不受 Alpine 支持（需 arm64-v8a / x86_64 / armeabi-v7a），无法装入完整 Linux 用户态。")
            return false
        }
        val ver = version()
        report("探测到架构 $arch，选用 Alpine $ver。")
        val free = runtimeDir().getFreeSpace()
        if (free < NEED_BYTES) {
            report("可用空间不足（${free / 1024 / 1024} MB），需至少 ${NEED_BYTES / 1024 / 1024} MB，已停止安装。")
            return false
        }
        report("下载 Alpine Linux $arch minirootfs…")
        if (!download(report)) return false

        report("校验 SHA-256…")
        val actual = sha256(archive())
        val expect = officialSha() ?: KNOWN_SHA[arch]
        if (expect == null) {
            report("无法获取 $arch 的官方校验值（可能离线），为安全起见不安装，请联网后重试。")
            return false
        }
        if (actual.isBlank() || !actual.equals(expect, ignoreCase = true)) {
            report("校验不通过：实际 $actual / 期望 $expect，已停止，不解压。")
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
        when (prootSource()) {
            ProotSource.NONE -> report("注意：尚未提供 proot，完整隔离暂不可用；默认仍跑 Android shell。可在「运行时」导入 proot 解锁。")
            ProotSource.IMPORTED -> report("已用导入的 proot，可直接进完整 Linux 用户态。")
            ProotSource.APK -> report("已用 APK 内置 proot，可直接进完整 Linux 用户态。")
        }
        return true
    }

    data class ExecResult(
        val engine: String,
        val output: String,
        val ok: Boolean,
        val ms: Long,
    )

    private fun androidShell(): String {
        val sys = "/system/bin/sh"
        return if (File(sys).canExecute()) sys else "sh"
    }

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
            val rd = rootfsDir().absolutePath
            val cmd = "${proot!!.absolutePath} --kill-on-exit --link2symlink -0 " +
                "-r '$rd' " +
                "-b '${ws.absolutePath}':/workspace -b /dev -b /proc -b /sys " +
                "-w /workspace /bin/sh '/workspace/${script.name}'"
            ShellRunner.run(cmd, dir, timeoutMs)
        } else {
            ShellRunner.run("${androidShell()} '${script.absolutePath}'", dir, timeoutMs)
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
