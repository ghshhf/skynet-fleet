package app.skynet.tianwang.next

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/** 一条读到的通知。只保留本机内存，进程被杀就清空 —— 不做悄悄落盘。 */
data class PhoneNotification(
    val pkg: String,
    val app: String,
    val title: String,
    val text: String,
    val timeMs: Long,
    val ongoing: Boolean,
    val key: String,
) {
    fun line(): String = buildString {
        append(app.ifBlank { pkg })
        if (title.isNotBlank()) append(" · ").append(title)
        if (text.isNotBlank()) append("：").append(text)
        if (ongoing) append("（常驻）")
    }
}

/**
 * 通知监听（NotificationListenerService）。
 *
 * 为什么需要它：很多「读完就走」的场景（验证码、到账提醒、聊天消息）不必截图，
 * 也不必点进 App —— 直接读通知最快。它**不替代无障碍**：通知里没有按钮，
 * 要回复/跳转仍得走界面树。
 *
 * 权限要用户手动给：系统设置 → 通知使用权。别自动尝试，降级要显式。
 */
class NotificationWatch : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        val n = sbn ?: return
        if (n.packageName == packageName) return    // 自己的前台服务通知不进列表，避免噪声
        val extras = n.notification.extras ?: return
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
            ?.ifBlank { null }
            ?: extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return
        val label = runCatching {
            val pm = packageManager
            pm.getApplicationLabel(pm.getApplicationInfo(n.packageName, 0)).toString()
        }.getOrDefault(n.packageName)
        NotificationCache.put(
            PhoneNotification(
                pkg = n.packageName,
                app = label,
                title = title,
                text = text,
                timeMs = n.postTime,
                ongoing = n.isOngoing,
                key = n.key,
            )
        )
    }

    companion object {
        fun enabled(ctx: Context): Boolean = runCatching {
            val flat = Settings.Secure.getString(
                ctx.contentResolver, "enabled_notification_listeners"
            ).orEmpty()
            flat.split(':').any {
                ComponentName.unflattenFromString(it)?.packageName == ctx.packageName
            }
        }.getOrDefault(false)

        fun openSettings(ctx: Context): Boolean = runCatching {
            ctx.startActivity(
                Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            true
        }.getOrDefault(false)

        fun list(limit: Int): List<PhoneNotification> = NotificationCache.snapshot(limit)
        fun clear() = NotificationCache.clear()
    }
}

/** 最近通知缓存。最多 40 条，按 key 去重（同一条更新时替换而不是刷屏）。 */
private object NotificationCache {
    private const val MAX = 40
    private val lock = Any()
    private val items = ArrayList<PhoneNotification>(MAX)

    fun put(n: PhoneNotification) = synchronized(lock) {
        items.removeAll { it.key == n.key }
        items.add(0, n)
        while (items.size > MAX) items.removeAt(items.lastIndex)
    }

    fun snapshot(limit: Int): List<PhoneNotification> = synchronized(lock) {
        items.take(limit.coerceIn(1, MAX))
    }

    fun clear() = synchronized(lock) { items.clear() }
}
