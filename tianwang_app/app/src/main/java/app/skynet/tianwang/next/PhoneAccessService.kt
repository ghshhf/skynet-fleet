package app.skynet.tianwang.next

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.accessibilityservice.AccessibilityService.ScreenshotResult
import android.accessibilityservice.AccessibilityService.TakeScreenshotCallback
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

/**
 * 无障碍自持通道（文档《手机端Agent能力拆解》第一步）。
 *
 * 为什么先做无障碍而不是 Shizuku：
 *   · 不用装第二个 App、不用联网调试授权，用户在本机设置里开一次即可；
 *   · 不烧 token：拿到的是结构化视图树，不是每步一张截图；
 *   · 权限面比 Shizuku 窄 —— 只能做界面能做的事，装应用/改系统设置这类高风险动作天然做不到。
 * Shizuku 那条通道以后可以并列加，[PhoneSelf] 的调用面不用改。
 *
 * 服务本身很薄：只提供「当前窗口 + 手势 + 截图 + 全局动作」，
 * 判断逻辑全在 [PhoneSelf] / [UiTree]，这样单测不必碰 Android 桩。
 */
class PhoneAccessService : AccessibilityService() {
    override fun onServiceConnected() {
        super.onServiceConnected()
        App.bind(this)
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            lastPackage = event.packageName?.toString().orEmpty()
        }
    }

    override fun onInterrupt() = Unit

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    /** 当前活动窗口的根节点。读不到（锁屏/系统界面）返回 null，由调用方如实报错。 */
    fun currentRoot(): AccessibilityNodeInfo? = rootInActiveWindow

    fun currentPackage(): String =
        runCatching { rootInActiveWindow?.packageName?.toString().orEmpty() }.getOrDefault(lastPackage)

    /** 把系统节点树映射成纯数据，交给 [UiTree] 处理。 */
    fun snapshot(maxDepth: Int = 40): UiNode? = mapNode(currentRoot(), 0, maxDepth)

    private fun mapNode(n: AccessibilityNodeInfo?, depth: Int, maxDepth: Int): UiNode? {
        n ?: return null
        if (depth > maxDepth) return null
        val r = Rect()
        runCatching { n.getBoundsInScreen(r) }
        val children = ArrayList<UiNode>()
        for (i in 0 until n.childCount) {
            if (depth + 1 > maxDepth) break
            mapNode(n.getChild(i), depth + 1, maxDepth)?.let { children.add(it) }
        }
        return UiNode(
            text = n.text?.toString().orEmpty(),
            desc = n.contentDescription?.toString().orEmpty(),
            viewId = n.viewIdResourceName.orEmpty(),
            cls = n.className?.toString()?.substringAfterLast('.').orEmpty(),
            clickable = n.isClickable,
            longClickable = n.isLongClickable,
            scrollable = n.isScrollable,
            editable = n.isEditable,
            checkable = n.isCheckable,
            checked = n.isChecked,
            enabled = n.isEnabled,
            focused = n.isFocused,
            selected = n.isSelected,
            bounds = UiBounds(r.left, r.top, r.right, r.bottom),
            depth = depth,
            children = children,
        )
    }

    /** 点一个坐标。无障碍手势比 `input tap` 更安全：不需要 INJECT_EVENTS，也不需要 adb。 */
    fun tap(x: Int, y: Int, long: Boolean = false): Boolean {
        if (x <= 0 || y <= 0) return false
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val duration = if (long) 650L else 120L
        val stroke = GestureDescription.StrokeDescription(path, 0, duration)
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long = 400): Boolean {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, ms.coerceIn(60, 1200))
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun global(action: Int): Boolean = performGlobalAction(action)

    /**
     * 截屏并返回 Bitmap（Android 11+）。失败把系统错误码原样带出来，
     * 不拿一张黑图冒充成功 —— 「截到了但全黑」和「根本没截到」必须分得开。
     */
    suspend fun screenshot(): Result<Bitmap> = suspendCancellableCoroutine { cont ->
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            cont.resume(Result.failure(UnsupportedOperationException("系统截图需要 Android 11+")))
            return@suspendCancellableCoroutine
        }
        takeScreenshot(Display.DEFAULT_DISPLAY, mainExecutor, object : TakeScreenshotCallback {
            override fun onSuccess(result: ScreenshotResult) {
                val bmp = try {
                    Bitmap.wrapHardwareBuffer(result.hardwareBuffer, result.colorSpace)
                        ?.copy(Bitmap.Config.ARGB_8888, false)
                } catch (e: Throwable) {
                    null
                } finally {
                    runCatching { result.hardwareBuffer.close() }
                }
                cont.resume(
                    if (bmp != null) Result.success(bmp)
                    else Result.failure(IllegalStateException("截图返回了空位图"))
                )
            }

            override fun onFailure(errorCode: Int) {
                cont.resume(Result.failure(IllegalStateException("截图失败 code=$errorCode")))
            }
        })
    }

    companion object {
        @Volatile
        var instance: PhoneAccessService? = null
            private set

        @Volatile
        private var lastPackage: String = ""
    }
}
