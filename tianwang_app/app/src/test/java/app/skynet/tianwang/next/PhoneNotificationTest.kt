package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 通知压成一行给 MCP/本地模型看，字段缺失时不能出现空冒号。 */
class PhoneNotificationTest {
    @Test
    fun formatsAllFields() {
        val line = PhoneNotification(
            pkg = "com.example", app = "示例", title = "验证码",
            text = "123456", timeMs = 0, ongoing = false, key = "k",
        ).line()
        assertEquals("示例 · 验证码：123456", line)
    }

    @Test
    fun fallsBackToPackageWhenAppNameMissing() {
        val line = PhoneNotification(
            pkg = "com.example", app = "", title = "", text = "到账 100 元",
            timeMs = 0, ongoing = false, key = "k",
        ).line()
        assertEquals("com.example：到账 100 元", line)
    }

    @Test
    fun marksOngoing() {
        val line = PhoneNotification(
            pkg = "com.example", app = "示例", title = "正在同步", text = "",
            timeMs = 0, ongoing = true, key = "k",
        ).line()
        assertTrue(line.endsWith("（常驻）"))
    }
}
