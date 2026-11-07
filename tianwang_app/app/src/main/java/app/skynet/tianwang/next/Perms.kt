package app.skynet.tianwang.next

import android.content.Context
import android.os.Build
import java.io.File

/**
 * 能力上限由**通道**决定，不由模型决定。
 *
 * 这张表就是「在有限权限内使用」的兑现方式：每条能力标清楚走哪条通道、当前能不能用、
 * 不能用时下一步做什么。缺权时就显式降级，不重试、不假装成功 —— 与 `PhoneSelf` 同一纪律。
 *
 * [rows] / [format] 是纯函数，环境状态用 [PermEnv] 传进来，方便 JVM 单测各种组合。
 */

enum class PermState(val label: String) {
    READY("可用"),
    NEEDS_GRANT("待授权"),
    NEEDS_SHIZUKU("需 Shizuku"),
    NEEDS_ROOT("需 Root"),
    UNSUPPORTED("不适用"),
}

data class PermEnv(
    val accessibilityEnabled: Boolean = false,
    val accessibilityConnected: Boolean = false,
    val notificationGranted: Boolean = false,
    val shizukuRunning: Boolean = false,
    val shizukuAuthorized: Boolean = false,
    val shellBound: Boolean = false,
    val screenshotSupported: Boolean = true,
    val rootAvailable: Boolean = false,
    val deviceOwner: Boolean = false,
    val lanRunning: Boolean = false,
    val ddnsConfigured: Boolean = false,
    val ddnsPublished: Boolean = false,
)

data class PermRow(
    val id: String,
    val capability: String,
    val channel: String,
    val state: PermState,
    val next: String,
)

object Perms {
    private const val UI_NEXT_ON = "已可用：self.ui.*"
    private const val UI_NEXT_OFF = "系统设置 → 无障碍 → 天网 Next"

    private fun shizukuState(env: PermEnv): PermState = when {
        env.shizukuAuthorized -> PermState.READY
        env.shizukuRunning -> PermState.NEEDS_GRANT
        else -> PermState.NEEDS_SHIZUKU
    }

    private fun shizukuNext(env: PermEnv): String = when {
        env.shizukuAuthorized && env.shellBound -> "已就绪：self.shell.exec"
        env.shizukuAuthorized -> "已授权；首次执行 self.shell.exec 会自动绑定 shell"
        env.shizukuRunning -> "执行 self.shizuku.request，在 Shizuku 弹窗点「允许」"
        else -> "装 Shizuku 并启动服务后，执行 self.shizuku.request"
    }

    private fun ownerNext(env: PermEnv): String = if (env.deviceOwner) {
        "已可用：self.owner.grant_perm / self.owner.restriction / self.owner.reboot"
    } else {
        "跑 self.owner.provision 看一条 adb 命令；已登录账号要先移除（全新手机最佳）"
    }

    fun rows(env: PermEnv): List<PermRow> {
        val uiState = if (env.accessibilityConnected) PermState.READY else PermState.NEEDS_GRANT
        val uiNext = when {
            env.accessibilityConnected -> UI_NEXT_ON
            env.accessibilityEnabled -> "已授权但服务未连接：退出设置页等 1–2 秒或重开 App"
            else -> UI_NEXT_OFF
        }
        val shotState = when {
            !env.screenshotSupported -> PermState.UNSUPPORTED
            else -> uiState
        }
        val shotNext = when {
            !env.screenshotSupported -> "Android 11+ 才有无障碍截图；低版本改用 self.ui.dump 读控件"
            env.accessibilityConnected -> "已可用：self.ui.screenshot"
            else -> uiNext
        }
        return listOf(
            PermRow("ui.observe", "读界面 / 找控件", "无障碍", uiState, uiNext),
            PermRow("ui.operate", "点击 / 输入 / 滑动 / 导航键", "无障碍", uiState, uiNext),
            PermRow("ui.screenshot", "截屏（可回传 JPEG）", "无障碍", shotState, shotNext),
            PermRow(
                "notif.read", "读通知（验证码 / 到账 / 消息）", "通知使用权",
                if (env.notificationGranted) PermState.READY else PermState.NEEDS_GRANT,
                if (env.notificationGranted) "已可用：self.notif.list"
                else "系统设置 → 通知使用权 → 勾选「天网 Next」",
            ),
            PermRow(
                "clipboard", "读写剪贴板", "普通权限", PermState.READY,
                "已可用；Android 10+ 后台读剪贴板可能被系统拒绝",
            ),
            PermRow("shell.exec", "跨 App shell（uid 2000）", "Shizuku", shizukuState(env), shizukuNext(env)),
            PermRow("settings.put", "改系统设置（secure / global）", "Shizuku", shizukuState(env), shizukuNext(env)),
            PermRow("app.install", "安装 / 卸载应用", "Shizuku", shizukuState(env), shizukuNext(env)),
            PermRow("app.force_stop", "强制停止后台应用", "Shizuku", shizukuState(env), shizukuNext(env)),
            PermRow(
                "lan.peer", "手机直连（局域网 / IPv6）", "TCP/IPv6",
                if (env.lanRunning) PermState.READY else PermState.NEEDS_GRANT,
                if (env.lanRunning) "已可用：self.lan.call / self.lan.speed，直连不经过中继"
                else "跑 self.lan.start；同一 Wi-Fi 用 fe80 链路本地，跨网用全局 IPv6",
            ),
            PermRow(
                "net.ddns", "动态 IPv6 → 稳定域名", "DNS/DDNS",
                if (env.ddnsPublished) PermState.READY else PermState.NEEDS_GRANT,
                when {
                    env.ddnsPublished -> "已发布：对端用 self.lan.call host=<名字>，地址变了也不用改"
                    env.ddnsConfigured -> "已配置未发布：跑 self.ddns.publish，或 self.ddns.watch enabled=true"
                    else -> "跑 self.ddns.config provider=duckdns domain=... token=...（或 custom URL）"
                },
            ),
            PermRow(
                "device.owner", "设备所有者 / 企业策略", "设备所有者",
                if (env.deviceOwner) PermState.READY else PermState.NEEDS_GRANT,
                if (env.deviceOwner) "已就绪：self.owner.status" else
                    "全新手机无账号时跑 self.owner.provision（一条 adb，无需 root / 解锁 Bootloader）",
            ),
            PermRow(
                "perm.grant", "静默授予 / 撤销运行时权限", "设备所有者",
                if (env.deviceOwner) PermState.READY else PermState.NEEDS_GRANT, ownerNext(env),
            ),
            PermRow(
                "device.restrict", "系统限制（相机 / USB / 截屏…）", "设备所有者",
                if (env.deviceOwner) PermState.READY else PermState.NEEDS_GRANT, ownerNext(env),
            ),
            PermRow(
                "device.reboot", "远程重启设备", "设备所有者",
                if (env.deviceOwner) PermState.READY else PermState.NEEDS_GRANT, ownerNext(env),
            ),
            PermRow(
                "root", "root shell", "Root",
                if (env.rootAvailable) PermState.READY else PermState.NEEDS_ROOT,
                if (env.rootAvailable) "检测到 su：可自建 root 通道（未接）"
                else "未检测到 root；Shizuku 已覆盖绝大多数需要",
            ),
        )
    }

    /** 多行文本表：先报四条通道的总状态，再逐条列能力与下一步。 */
    fun format(env: PermEnv): String {
        val list = rows(env)
        val count = list.groupingBy { it.state }.eachCount()
        val txt = buildString {
            append("权限矩阵（能力上限由通道决定）\n")
            append("无障碍：").append(
                when {
                    env.accessibilityConnected -> "已连接"
                    env.accessibilityEnabled -> "已授权未连接"
                    else -> "未开启"
                }
            ).append('\n')
            append("通知使用权：").append(if (env.notificationGranted) "已授权" else "未授权").append('\n')
            append("Shizuku：").append(
                when {
                    env.shizukuAuthorized -> "已授权"
                    env.shizukuRunning -> "运行中，未授权"
                    else -> "未运行"
                }
            ).append('\n')
            append("Root：").append(if (env.rootAvailable) "检测到 su" else "未检测到").append('\n')
            append("设备所有者：").append(if (env.deviceOwner) "已就绪" else "未就绪").append('\n')
            append("局域网直连：").append(if (env.lanRunning) "运行中" else "未启动").append('\n')
            append("动态 IPv6 名字：").append(
                when {
                    env.ddnsPublished -> "已发布"
                    env.ddnsConfigured -> "已配置未发布"
                    else -> "未配置"
                }
            ).append('\n')
            append("—— 可用 ").append(count[PermState.READY] ?: 0)
            append(" / 待授权 ").append(count[PermState.NEEDS_GRANT] ?: 0)
            append(" / 需 Shizuku ").append(count[PermState.NEEDS_SHIZUKU] ?: 0)
            append(" / 不适用 ").append((count[PermState.UNSUPPORTED] ?: 0) + (count[PermState.NEEDS_ROOT] ?: 0))
            append(" ——\n")
            for (r in list) {
                append("· ").append(r.id.padEnd(15))
                    .append(r.channel.padEnd(10))
                    .append(r.state.label).append("  ")
                    .append(r.capability).append('\n')
                append("    → ").append(r.next).append('\n')
            }
            append("缺授权时工具会直接报「没开」并给出下一步，不重试、不退回假数据。")
        }
        return txt
    }

    /** 从真机读一次当前状态。只读：不绑定服务、不弹任何授权框。 */
    fun snapshot(ctx: Context): PermEnv {
        val sz = ShizukuBridge.status()
        return PermEnv(
            accessibilityEnabled = PhoneSelf.enabled(ctx),
            accessibilityConnected = PhoneSelf.connected(),
            notificationGranted = NotificationWatch.enabled(ctx),
            shizukuRunning = sz.running,
            shizukuAuthorized = sz.authorized,
            shellBound = ShizukuBridge.bound(),
            screenshotSupported = Build.VERSION.SDK_INT >= 30,
            rootAvailable = hasRootHint(),
            deviceOwner = OwnerBridge.status(ctx).deviceOwner,
            lanRunning = LanHub.running,
            ddnsConfigured = Prefs.ddnsProvider != Ddns.PROVIDER_OFF,
            ddnsPublished = Prefs.ddnsEnabled && Prefs.ddnsLastIp.isNotBlank(),
        )
    }

    /** 只看 su 文件在不在，不执行任何命令；只作为「有没有 root」的弱提示。 */
    fun hasRootHint(): Boolean = ROOT_PATHS.any {
        runCatching { File(it).exists() }.getOrDefault(false)
    }

    private val ROOT_PATHS = listOf(
        "/system/bin/su", "/system/xbin/su", "/sbin/su", "/debug_ramdisk/su",
    )
}
