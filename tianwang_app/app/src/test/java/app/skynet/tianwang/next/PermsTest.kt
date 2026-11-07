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
    fun deviceOwnerRowsFollowProvisionState() {
        val off = PermEnv()
        assertEquals(PermState.NEEDS_GRANT, row(off, "device.owner").state)
        assertTrue(row(off, "device.owner").next.contains("self.owner.provision"))
        assertEquals(PermState.NEEDS_GRANT, row(off, "perm.grant").state)
        assertEquals(PermState.NEEDS_GRANT, row(off, "device.restrict").state)
        assertEquals(PermState.NEEDS_GRANT, row(off, "device.reboot").state)

        val owner = PermEnv(deviceOwner = true)
        assertEquals(PermState.READY, row(owner, "device.owner").state)
        assertEquals(PermState.READY, row(owner, "perm.grant").state)
        assertEquals(PermState.READY, row(owner, "device.restrict").state)
        assertEquals(PermState.READY, row(owner, "device.reboot").state)
        assertTrue(row(owner, "device.reboot").next.contains("self.owner.reboot"))
    }

    @Test
    fun rootFollowsHint() {
        assertEquals(PermState.NEEDS_ROOT, row(PermEnv(), "root").state)
        assertEquals(PermState.READY, row(PermEnv(rootAvailable = true), "root").state)
    }

    @Test
    fun lanRowFollowsServerState() {
        assertEquals(PermState.NEEDS_GRANT, row(PermEnv(), "lan.peer").state)
        assertTrue(row(PermEnv(), "lan.peer").next.contains("self.lan.start"))
        assertEquals(PermState.READY, row(PermEnv(lanRunning = true), "lan.peer").state)
        assertTrue(row(PermEnv(lanRunning = true), "lan.peer").next.contains("self.lan.call"))
    }

    @Test
    fun fleetRoutingFollowsLanChannel() {
        assertEquals(PermState.NEEDS_GRANT, row(PermEnv(), "fleet.peer").state)
        assertTrue(row(PermEnv(), "fleet.peer").next.contains("self.lan.start"))
        val on = row(PermEnv(lanRunning = true), "fleet.peer")
        assertEquals(PermState.READY, on.state)
        assertTrue(on.next.contains("self.peer.call"))
        assertTrue(on.next.contains("权限只有一档"))
    }

    @Test
    fun ddnsRowFollowsConfigAndPublishState() {
        assertEquals(PermState.NEEDS_GRANT, row(PermEnv(), "net.ddns").state)
        assertTrue(row(PermEnv(), "net.ddns").next.contains("self.ddns.config"))

        val configured = PermEnv(ddnsConfigured = true)
        assertEquals(PermState.NEEDS_GRANT, row(configured, "net.ddns").state)
        assertTrue(row(configured, "net.ddns").next.contains("self.ddns.publish"))

        val published = PermEnv(ddnsConfigured = true, ddnsPublished = true)
        assertEquals(PermState.READY, row(published, "net.ddns").state)
        assertTrue(row(published, "net.ddns").next.contains("host=<名字>"))
    }

    @Test
    fun formatReportsChannelStatusCountsAndNextSteps() {
        val txt = Perms.format(PermEnv())
        assertTrue(txt.contains("无障碍：未开启"))
        assertTrue(txt.contains("通知使用权：未授权"))
        assertTrue(txt.contains("Shizuku：未运行"))
        assertTrue(txt.contains("设备所有者：未就绪"))
        assertTrue(txt.contains("局域网直连：未启动"))
        assertTrue(txt.contains("动态 IPv6 名字：未配置"))
        assertTrue(txt.contains("需 Shizuku"))
        assertTrue(txt.contains("→"))
        assertTrue(txt.contains("不重试"))
        assertFalse(txt.contains("null"))
    }
}
