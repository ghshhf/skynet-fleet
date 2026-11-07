package app.skynet.tianwang.next

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder

/**
 * 入网链接的常驻壳。
 *
 * App 退到后台后，普通进程随时可能被回收；`PhoneMcpLink` 的断线重连再聪明，
 * 进程没了也没用。前台服务只做一件事：拿一个 ongoing 通知把进程钉住，
 * 然后让 `PhoneMcpLink` 继续工作。
 */
class PhoneLinkService : Service() {
    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(
                CHANNEL, "手机入网", NotificationManager.IMPORTANCE_LOW,
            ).apply { description = "控制面通过 /pair 调用本机工具时保持在线" })
        }
        startForeground(NOTIFICATION_ID, buildNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 这个服务现在管两条链路：云中继（MCP）和手机直连（LAN/IPv6）。
        // 只要有一条在跑就得钉住进程；两条都没有才停。
        if (!Prefs.mcpConfigured() && !LanHub.running) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (Prefs.mcpConfigured()) PhoneMcpLink.start()
        return START_STICKY
    }

    override fun onDestroy() {
        // 只停这次连接，不把用户的「开着」改成「关着」：
        // 系统回收后 START_STICKY 会重新拉起，读到的仍是 enabled=true。
        PhoneMcpLink.pause()
        LanHub.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentTitle("天网手机链路")
            .setContentText("正在保持入网 / 局域网直连；点开可查看或断开")
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    companion object {
        private const val CHANNEL = "phone_link"
        private const val NOTIFICATION_ID = 1001

        fun start(ctx: Context) {
            try {
                ctx.startForegroundService(Intent(ctx, PhoneLinkService::class.java))
            } catch (t: Throwable) {
                // Android 15 对后台启动 dataSync 有限制；退回进程内直连，至少不断线。
                PhoneMcpLink.start()
            }
        }

        fun stop(ctx: Context) {
            PhoneMcpLink.stop()
            ctx.stopService(Intent(ctx, PhoneLinkService::class.java))
        }
    }
}
