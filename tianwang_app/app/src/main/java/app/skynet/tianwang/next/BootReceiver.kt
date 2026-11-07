package app.skynet.tianwang.next

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 开机把上次开着的入网链接接回来。
 *
 * Android 15 起，target 35 的 dataSync 前台服务不能从 BOOT_COMPLETED 拉起；
 * `PhoneLinkService.start` 内会 catch 并回落成进程内直连。尽力而为，不假装一定成功。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!Prefs.mcpConfigured() || !Prefs.mcpEnabled) {
            // 没接入网也要把 DDNS 接回来：动态 IPv6 换了，域名必须跟着更新。
            if (Prefs.ddnsEnabled) PhoneLinkService.start(context)
            return
        }
        PhoneLinkService.start(context)
    }
}
