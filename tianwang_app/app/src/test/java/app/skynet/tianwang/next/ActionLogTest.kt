package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class ActionLogTest {

    @Before
    fun clean() = ActionLog.clear()

    @Test
    fun keepsNewestFifty() {
        for (i in 0 until 60) ActionLog.add("tool-$i", JSONObject(), true, "", 1)
        val rows = ActionLog.list(50)
        assertEquals(50, rows.size)
        assertEquals("tool-59", rows.first().name)
        assertEquals("tool-10", rows.last().name)
    }

    @Test
    fun summaryHidesSecretsAndKeepsTextAsLength() {
        val args = JSONObject()
            .put("secret", "abcdef")
            .put("token", "t-123")
            .put("text", "x".repeat(100))
            .put("query", "设置")
        val s = ActionLog.summarize(args)
        assertTrue(s.contains("secret=<已隐藏>"))
        assertTrue(s.contains("token=<已隐藏>"))
        assertTrue(s.contains("text=100 字"))
        assertTrue(s.contains("query=设置"))
        assertTrue(s.length <= 160)
    }

    @Test
    fun formatMarksFailureAndReason() {
        ActionLog.add("self_ui_tap", JSONObject().put("query", "发送"), false, "没找到控件", 12)
        val row = ActionLog.list(1).single()
        val line = ActionLog.format(row)
        assertTrue(line.contains("✗"))
        assertTrue(line.contains("self_ui_tap"))
        assertTrue(line.contains("没找到控件"))
        assertTrue(line.contains("12ms"))
    }
}
