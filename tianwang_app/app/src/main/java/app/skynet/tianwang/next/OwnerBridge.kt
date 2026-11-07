package app.skynet.tianwang.next

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context

/**
 * 设备所有者通道：不比 root 弱多少的「假 root」，且**不需要解锁 Bootloader、不需要 root**。
 *
 * 与 Shizuku 的分工：
 * - Shizuku = shell（uid 2000），擅长跨 App 文件 / pm / am / dumpsys。
 * - 设备所有者 = 系统策略身份，擅长静默授权、系统限制、静默卸载、重启，但**不是 shell**。
 *
 * 本 App 自己成为设备所有者即可直接调 DPM，不需要 Dhizuku；Dhizuku 只用于把设备所有者
 * 权限共享给多个第三方 App（本 App 若已是所有者，就不能再装 Dhizuku 当所有者）。
 */
object OwnerBridge {
    private const val DHIZUKU_PKG = "com.rosan.dhizuku"

    data class Status(
        val deviceOwner: Boolean,
        val adminActive: Boolean,
        val dhizukuInstalled: Boolean,
    ) {
        val usable: Boolean get() = deviceOwner

        /** 一行说清卡在哪；纯函数，方便单测。 */
        fun line(): String = when {
            deviceOwner -> "已就绪：本 App 是设备所有者（静默授权 / 系统限制 / 重启）"
            adminActive -> "只激活了设备管理，不是设备所有者：按 self.owner.provision 升级"
            dhizukuInstalled ->
                "未就绪：设备上有 Dhizuku；可把本 App 设为所有者，或改用 Dhizuku 共享（本 App 未接其 UserService）"
            else -> "未就绪：全新手机可跑 self.owner.provision，用一条 adb 命令成为设备所有者"
        }
    }

    private fun dpm(ctx: Context): DevicePolicyManager? =
        runCatching { ctx.getSystemService(DevicePolicyManager::class.java) }.getOrNull()

    private fun admin(ctx: Context): ComponentName = ComponentName(ctx, OwnerReceiver::class.java)

    fun status(ctx: Context): Status {
        val d = dpm(ctx)
        val owner = runCatching { d?.isDeviceOwnerApp(ctx.packageName) == true }.getOrDefault(false)
        val active = runCatching { d?.isAdminActive(admin(ctx)) == true }.getOrDefault(false)
        val dhizuku = runCatching {
            ctx.packageManager.getPackageInfo(DHIZUKU_PKG, 0)
            true
        }.getOrDefault(false)
        return Status(owner, active, dhizuku)
    }

    /** 纯函数：给用户抄的 adb 命令，路径必须和 manifest 里的 receiver 完全一致。 */
    fun provisionCommand(pkg: String): String =
        "adb shell dpm set-device-owner $pkg/.OwnerReceiver"

    fun provisionHint(ctx: Context): String = buildString {
        append("把本 App 设为设备所有者（不需要 root，也不需要解锁 Bootloader）\n")
        append("1) 手机开「开发者选项 → USB 调试」，连电脑\n")
        append("2) ").append(provisionCommand(ctx.packageName)).append('\n')
        append("3) 看到 Success 后回来跑 self.owner.status\n")
        append("前提：系统里不能有任何账号。全新手机最合适；已登录账号请先移除，否则会报 already some accounts。\n")
        append("无电脑替代：首次开机用 Android 企业 QR provisioning 也能指定 DPC，但要恢复出厂。\n")
        append("注意：设备所有者不能像普通 App 一样卸载；撤销需要恢复出厂或后续专门的清除入口。\n")
        append("和 Shizuku 的关系：两者可共存但互相替代不了——Shizuku 给 shell，所有者给 DPM 策略。")
    }

    fun grantPermission(ctx: Context, pkg: String, permission: String, mode: String): ToolResult {
        if (pkg.isBlank() || permission.isBlank()) {
            return ToolResult(false, err = "pkg 和 permission 都不能为空")
        }
        if (!status(ctx).deviceOwner) return notOwner()
        val state = when (mode.trim().lowercase()) {
            "grant", "granted", "allow" -> DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED
            "deny", "denied" -> DevicePolicyManager.PERMISSION_GRANT_STATE_DENIED
            "default", "reset" -> DevicePolicyManager.PERMISSION_GRANT_STATE_DEFAULT
            else -> return ToolResult(false, err = "mode 只支持 granted / denied / default")
        }
        val d = dpm(ctx) ?: return ToolResult(false, err = "本机没有 DevicePolicyManager")
        return runCatching {
            val ok = d.setPermissionGrantState(admin(ctx), pkg, permission, state)
            if (ok) {
                ToolResult(true, text = "已把 $pkg 的 $permission 设为 $mode。")
            } else {
                ToolResult(
                    false,
                    err = "系统拒绝：目标没装、不是运行时权限，或用户已手动锁定该权限。",
                )
            }
        }.getOrElse { ToolResult(false, err = "设置权限失败：${it.javaClass.simpleName}: ${it.message}") }
    }

    fun setRestriction(ctx: Context, key: String, enabled: Boolean): ToolResult {
        if (key.isBlank()) return ToolResult(false, err = "key 不能为空，例如 no_camera / no_usb_file_transfer")
        if (!status(ctx).deviceOwner) return notOwner()
        val d = dpm(ctx) ?: return ToolResult(false, err = "本机没有 DevicePolicyManager")
        return runCatching {
            if (enabled) d.addUserRestriction(admin(ctx), key)
            else d.clearUserRestriction(admin(ctx), key)
            ToolResult(
                true,
                text = if (enabled) "已开启限制 $key。"
                else "已解除限制 $key。",
            )
        }.getOrElse { ToolResult(false, err = "设置限制失败：${it.javaClass.simpleName}: ${it.message}") }
    }

    fun reboot(ctx: Context): ToolResult {
        if (!status(ctx).deviceOwner) return notOwner()
        val d = dpm(ctx) ?: return ToolResult(false, err = "本机没有 DevicePolicyManager")
        return runCatching {
            d.reboot(admin(ctx))
            ToolResult(true, text = "已请求重启。手机会立刻重启，远程连接会断开，稍后自动重连。")
        }.getOrElse { ToolResult(false, err = "重启失败：${it.javaClass.simpleName}: ${it.message}") }
    }

    private fun notOwner(): ToolResult = ToolResult(
        false,
        err = "本 App 不是设备所有者。先跑 self.owner.provision 按命令设置（全新手机、无账号）。",
    )
}
