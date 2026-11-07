package app.skynet.tianwang.next

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 整单目标的安全边界：不能递归、不能切断自己的回包通道。
 * 这是纯集合测试 —— 不需要真机，也不会加载模型。
 */
class LocalGoalTest {
    @Test
    fun goalCannotRecurseOrCutItsOwnLink() {
        for (n in listOf(
            "self_agent_goal", "self_peer_goal", "self_peer_call",
            "self_lan_start", "self_lan_stop", "self_owner_reboot",
        )) {
            assertTrue("应屏蔽 $n", n in LocalGoal.blockedNames)
        }
    }

    @Test
    fun normalToolsStayAvailable() {
        for (n in listOf(
            "self_ui_tap", "self_ui_dump", "self_shell_exec",
            "self_peer_list", "self_peer_probe", "self_ddns_publish",
        )) {
            assertFalse("不应屏蔽 $n", n in LocalGoal.blockedNames)
        }
    }
}
