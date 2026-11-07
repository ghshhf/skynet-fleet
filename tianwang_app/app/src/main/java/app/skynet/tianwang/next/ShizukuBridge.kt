package app.skynet.tianwang.next

import android.content.ComponentName
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.IBinder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import rikka.shizuku.Shizuku
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume

/**
 * Shizuku 通道：借一次无线调试授权拿到 shell（uid 2000）身份，
 * 补上无障碍根本够不着的「跨 App shell / 装应用 / 改系统设置」。
 *
 * 纪律跟无障碍一致：**没装、没运行、没授权都显式报出来**，不重试、不假装成功。
 * 没装 Shizuku 不影响 App 其它能力，这条通道只是「可选的更高一层」。
 */
object ShizukuBridge {
    const val REQUEST_CODE = 0x5A1C
    private const val MANAGER_PKG = "moe.shizuku.manager"
    private const val SERVICE_TAG = "skynet-shell"

    data class Status(
        val installed: Boolean,
        val running: Boolean,
        val authorized: Boolean,
        val version: Int,
        val uid: Int,
        val bound: Boolean,
    ) {
        val usable: Boolean get() = running && authorized

        /** 一行说清当前卡在哪；纯函数，方便单测。 */
        fun line(): String = when {
            !running && !installed -> "不可用：没装 Shizuku（不影响无障碍通道）"
            !running -> "已装但服务没在运行：打开 Shizuku，完成配对并启动服务"
            !authorized -> "服务在运行，但本 App 未授权：执行 self.shizuku.request 后点「允许」"
            bound -> "已就绪：shell uid=$uid，Shizuku v$version"
            else -> "已授权，shell 会话未建立：首次执行 self.shell.exec 时自动绑定"
        }
    }

    @Volatile
    private var remote: IUserService? = null

    @Volatile
    private var waiter: Continuation<Boolean>? = null

    @Volatile
    var lastError: String = ""
        private set

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val svc = runCatching { IUserService.Stub.asInterface(binder) }.getOrNull()
            remote = svc
            lastError = if (svc == null) "Shizuku 返回了空 binder" else ""
            waiter?.let { w ->
                waiter = null
                w.resume(svc != null)
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            remote = null
            lastError = "shell 会话断开（Shizuku 重启或被撤销授权）"
            waiter?.let { w ->
                waiter = null
                w.resume(false)
            }
        }
    }

    private fun args(): Shizuku.UserServiceArgs =
        Shizuku.UserServiceArgs(
            ComponentName(App.ctx.packageName, ShellService::class.java.name)
        )
            .daemon(true)
            .tag(SERVICE_TAG)
            .version(1)

    /** 只读探测：不绑定服务、不弹授权框。 */
    fun status(): Status {
        val running = runCatching { Shizuku.pingBinder() }.getOrDefault(false)
        val installed = runCatching {
            App.ctx.packageManager.getPackageInfo(MANAGER_PKG, 0)
            true
        }.getOrDefault(false)
        if (!running) {
            return Status(installed, running = false, authorized = false, version = -1, uid = -1, bound = false)
        }
        val authorized = runCatching {
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        }.getOrDefault(false)
        return Status(
            installed = true,
            running = true,
            authorized = authorized,
            version = runCatching { Shizuku.getVersion() }.getOrDefault(-1),
            uid = runCatching { Shizuku.getUid() }.getOrDefault(-1),
            bound = remote != null,
        )
    }

    fun bound(): Boolean = remote != null

    /** 发起授权。授权框由 Shizuku 自己弹，本 App 不伪造系统弹窗。 */
    fun requestPermission(): String {
        val st = status()
        if (!st.running) return st.line()
        if (st.authorized) return "本 App 已经拿到 Shizuku 授权，直接执行 self.shell.exec 即可。"
        return runCatching {
            Shizuku.requestPermission(REQUEST_CODE)
            "已向 Shizuku 发起授权请求：请在弹出的窗口点「允许」（必要时先切到 Shizuku App），然后重跑 self.shizuku.status。"
        }.getOrElse {
            "发起授权失败：${it.javaClass.simpleName}: ${it.message}"
        }
    }

    /**
     * 确保 shell 会话在。绑定回调走主线程，所以这里用挂起等，而不是忙等。
     * 超时/失败都写进 [lastError]，由上层原样报给调用方。
     */
    suspend fun ensureBound(timeoutMs: Long = 12_000): Boolean {
        if (remote != null) return true
        val st = status()
        if (!st.usable) {
            lastError = st.line()
            return false
        }
        val ok = withTimeoutOrNull(timeoutMs) {
            suspendCancellableCoroutine<Boolean> { cont ->
                waiter = cont
                cont.invokeOnCancellation { if (waiter === cont) waiter = null }
                runCatching { Shizuku.bindUserService(args(), connection) }
                    .onFailure {
                        waiter = null
                        lastError = "绑定 shell 失败：${it.javaClass.simpleName}: ${it.message}"
                        cont.resume(false)
                    }
            }
        }
        if (ok == true) return true
        if (lastError.isBlank()) {
            lastError = "绑定 shell 超时（${timeoutMs}ms）：Shizuku 可能在休眠，重开 Shizuku 再试"
        }
        waiter = null
        return false
    }

    /** 在 shell 身份下执行一条命令。binder 调用是阻塞的，必须离开主线程。 */
    suspend fun exec(cmd: String, cwd: String, timeoutMs: Int): ToolResult {
        if (cmd.isBlank()) return ToolResult(false, err = "cmd 不能为空")
        if (!ensureBound()) {
            return ToolResult(false, err = lastError.ifBlank { "Shizuku shell 通道不可用" })
        }
        val svc = remote ?: return ToolResult(false, err = "shell 会话已断开，请重试")
        val ms = timeoutMs.coerceIn(500, 60_000)
        return withContext(Dispatchers.IO) {
            runCatching { svc.exec(cmd, cwd, ms) }
                .fold(
                    onSuccess = { out ->
                        val uid = status().uid
                        ToolResult(
                            true,
                            text = "$out\n\n（在 Shizuku shell 身份下执行，uid=$uid，超时 ${ms}ms）",
                        )
                    },
                    onFailure = { t ->
                        remote = null
                        ToolResult(
                            false,
                            err = "shell 执行失败：${t.javaClass.simpleName}: ${t.message}",
                        )
                    },
                )
        }
    }
}
