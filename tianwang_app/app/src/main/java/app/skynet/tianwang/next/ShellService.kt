package app.skynet.tianwang.next

import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * Shizuku 的 UserService：由 Shizuku 在**它自己的进程**里反射实例化，
 * 所以这里的 `Runtime.exec` 拿到的是 shell（uid 2000），不是 App 自己的 uid。
 *
 * 🔴 manifest 里故意不声明这个 Service —— 它不是给系统 bind 的，而是给
 * `Shizuku.bindUserService` 用的。声明成普通 Service 只会多一个空壳入口。
 */
class ShellService : Service() {
    private val binder = object : IUserService.Stub() {
        override fun exec(cmd: String, cwd: String, timeoutMs: Int): String =
            ShellRunner.run(cmd, cwd, timeoutMs)
    }

    override fun onBind(intent: Intent?): IBinder = binder
}
