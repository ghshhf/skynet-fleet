package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** MCP 工具表：名字唯一、新能力在册、只读闸门对写操作生效。 */
class PhoneMcpToolsTest {
    @Test
    fun toolNamesAreUnique() {
        val names = PhoneMcp.toolList().map { it.name }
        assertEquals(names.size, names.toSet().size)
    }

    @Test
    fun notificationClipboardBatteryToolsAreExposed() {
        val names = PhoneMcp.toolList().map { it.name }
        for (n in listOf(
            "self_notif_list", "self_notif_clear",
            "self_clipboard_get", "self_clipboard_set", "self_battery_status",
        )) {
            assertTrue("缺少工具 $n", n in names)
        }
    }

    @Test
    fun automationAndDeviceToolsAreExposed() {
        val names = PhoneMcp.toolList().map { it.name }
        for (n in listOf(
            "self_ui_wait", "self_ui_tap_at", "self_ui_swipe",
            "self_app_open_url", "self_app_list",
            "self_audio_volume", "self_audio_set_volume",
            "self_net_status", "self_device_info", "self_screen_state",
            "self_diag", "self_audit",
        )) {
            assertTrue("缺少工具 $n", n in names)
        }
    }

    @Test
    fun writeToolsAreMarkedSideEffect() {
        val byName = PhoneMcp.toolList().associateBy { it.name }
        assertTrue(byName.getValue("self_clipboard_set").sideEffect)
        assertTrue(byName.getValue("self_notif_clear").sideEffect)
        assertTrue(byName.getValue("self_audio_set_volume").sideEffect)
        assertTrue(byName.getValue("self_ui_tap_at").sideEffect)
        assertTrue(byName.getValue("self_ui_swipe").sideEffect)
        assertTrue(byName.getValue("self_app_open_url").sideEffect)
        assertFalse(byName.getValue("self_screen_state").sideEffect)
        assertFalse(byName.getValue("self_clipboard_get").sideEffect)
        assertFalse(byName.getValue("self_notif_list").sideEffect)
        assertFalse(byName.getValue("self_ui_wait").sideEffect)
        assertFalse(byName.getValue("self_app_list").sideEffect)
        assertFalse(byName.getValue("self_audio_volume").sideEffect)
        assertFalse(byName.getValue("self_net_status").sideEffect)
        assertFalse(byName.getValue("self_device_info").sideEffect)
        assertFalse(byName.getValue("self_diag").sideEffect)
        assertFalse(byName.getValue("self_audit").sideEffect)
    }

    @Test
    fun readOnlyGateBlocksClipboardWrite() {
        var called = false
        val core = McpCore(
            tools = PhoneMcp.toolList(),
            actionsAllowed = { false },
            call = { _, _ -> called = true; ToolResult(true, text = "should-not-run") },
        )
        val resp = core.handleLine(
            """{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"self_clipboard_set","arguments":{"text":"x"}}}"""
        )
        val o = JSONObject(resp!!)
        assertTrue(o.getJSONObject("result").getBoolean("isError"))
        assertFalse(called)
    }

    @Test
    fun readOnlyGateDoesNotBlockReads() {
        var called = false
        val core = McpCore(
            tools = PhoneMcp.toolList(),
            actionsAllowed = { false },
            call = { _, _ -> called = true; ToolResult(true, text = "ok") },
        )
        val resp = core.handleLine(
            """{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"self_notif_list","arguments":{}}}"""
        )
        val o = JSONObject(resp!!)
        assertFalse(o.getJSONObject("result").getBoolean("isError"))
        assertTrue(called)
    }
}
