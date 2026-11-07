package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 设备所有者通道：四态文案 + 抄给用户的 adb 命令必须和 manifest 里的 receiver 对得上。 */
class OwnerBridgeTest {
    @Test
    fun statusLineCoversEachStage() {
        assertTrue(OwnerBridge.Status(true, true, false).line().contains("已就绪"))
        assertTrue(OwnerBridge.Status(false, true, false).line().contains("self.owner.provision"))
        assertTrue(OwnerBridge.Status(false, false, true).line().contains("Dhizuku"))
        assertTrue(OwnerBridge.Status(false, false, false).line().contains("provision"))
        assertTrue(OwnerBridge.Status(true, true, false).usable)
    }

    @Test
    fun provisionCommandMatchesReceiverName() {
        val pkg = "app.skynet.tianwang.next"
        assertEquals(
            "adb shell dpm set-device-owner $pkg/.OwnerReceiver",
            OwnerBridge.provisionCommand(pkg),
        )
    }

    @Test
    fun provisionCommandUsesGivenPackage() {
        assertTrue(OwnerBridge.provisionCommand("com.example.fleet").contains("com.example.fleet/.OwnerReceiver"))
    }
}
