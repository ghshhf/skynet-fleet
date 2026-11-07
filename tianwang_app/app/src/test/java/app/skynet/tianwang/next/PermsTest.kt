package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 权限矩阵：缺权必须显式降级，状态跟着通道走，文案里要有下一步。 */
class PermsTest {
    private fun row(env: PermEnv, id: String) = Perms.rows(env).first { it.id == id }

    @Test
    fun everyRowHasUniqueIdAndNonBlankChannel() {
        val rows = Perms.rows(PermEnv())
        assertEquals(rows.size, rows.map { it.id }.toSet().size)
        rows.forEach {
            assertTrue("${it.id} 缺通道", it.channel.isNotBlank())
            assertTrue("${it.id} 缺能力描述", it.capability.isNotBlank())
            assertTrue("${it.id} 缺下一步", it.next.isNotBlank())
        }
    }

    @Test
    fun accessibilityOffDegradesUiCapabilities() {
        val env = PermEnv()
        assertEquals(PermState.NEEDS_GRANT, row(env, "ui.observe").state)
        assertEquals(PermState.NEEDS_GRANT, row(env, "ui.operate").state)
        assertEquals(PermState.NEEDS_GRANT, row(env, "ui.screenshot").state)
        assertTrue(row(env, "ui.observe").next.contains("无障碍"))
    }

    @Test
    fun accessibilityConnectedMakesUiReady() {
        val env = PermEnv(accessibilityEnabled = true, accessibilityConnected = true)
        assertEquals(PermState.READY, row(env, "ui.observe").state)
        assertEquals(PermState.READY, row(env, "ui.operate").state)
        assertEquals(PermState.READY, row(env, "ui.screenshot").state)
    }

    @Test
    fun enabledButNotConnectedSaysWait() {
        val env = PermEnv(accessibilityEnabled = true, accessibilityConnected = false)
        assertEquals(PermState.NEEDS_GRANT, row(env, "ui.observe").state)
        assertTrue(row(env, "ui.observe").next.contains("未连接"))
    }

    @Test
    fun shizukuStatesFollowServiceAndAuth() {
        val off = PermEnv()
        assertEquals(PermState.NEEDS_SHIZUKU, row(off, "shell.exec").state)
        assertEquals(PermState.NEEDS_SHIZUKU, row(off, "app.install").state)

        val running = PermEnv(shizukuRunning = true)
        assertEquals(PermState.NEEDS_GRANT, row(running, "shell.exec").state)
        assertTrue(row(running, "shell.exec").next.contains("self.shizuku.request"))

        val authed = PermEnv(shizukuRunning = true, shizukuAuthorized = true)
        assertEquals(PermState.READY, row(authed, "shell.exec").state)
        assertTrue(row(authed, "shell.exec").next.contains("自动绑定"))

        val bound = PermEnv(shizukuRunning = true, shizukuAuthorized = true, shellBound = true)
        assertTrue(row(bound, "shell.exec").next.contains("self.shell.exec"))
    }

    @Test
    fun screenshotIsUnsupportedBeforeAndroid11() {
        val env = PermEnv(
            accessibilityEnabled = true,
            accessibilityConnected = true,
            screenshotSupported = false,
        )
        assertEquals(PermState.UNSUPPORTED, row(env, "ui.screenshot").state)
        assertTrue(row(env, "ui.screenshot").next.contains("ui.dump"))
    }

    @Test
    fun notificationAndClipboardAreIndependentOfAccessibility() {
        val env = PermEnv(notificationGranted = true)
        assertEquals(PermState.READY, row(env, "notif.read").state)
        assertEquals(PermState.READY, row(env, "clipboard").state)

        val off = PermEnv()
        assertEquals(PermState.NEEDS_GRANT, row(off, "notif.read").state)
    }

    @Test
    fun deviceOwnerIsAlwaysUnsupportedAndRootFollowsHint() {
        assertEquals(PermState.UNSUPPORTED, row(PermEnv(), "device.owner").state)
        assertEquals(PermState.NEEDS_ROOT, row(PermEnv(), "root").state)
        assertEquals(PermState.READY, row(PermEnv(rootAvailable = true), "root").state)
    }

    @Test
    fun formatReportsChannelStatusCountsAndNextSteps() {
        val txt = Perms.format(PermEnv())
        assertTrue(txt.contains("无障碍：未开启"))
        assertTrue(txt.contains("通知使用权：未授权"))
        assertTrue(txt.contains("Shizuku：未运行"))
        assertTrue(txt.contains("需 Shizuku"))
        assertTrue(txt.contains("→"))
        assertTrue(txt.contains("不重试"))
        assertFalse(txt.contains("null"))
    }
}
