package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PeerBookTest {
    private fun peer(
        name: String,
        battery: Int = 80,
        charging: Boolean = false,
        tools: Set<String> = setOf("self_status"),
        lastSeen: Long = 0L,
    ) = PeerBook.Peer(
        name = name,
        host = "fe80::1%wlan0",
        token = "0123456789abcdef0123456789abcdef",
        battery = battery,
        charging = charging,
        tools = tools,
        lastSeen = lastSeen,
    )

    @Test
    fun encodeDecodeKeepsRoster() {
        val one = peer("b-phone", charging = true)
        val text = PeerBook.encode(listOf(one))
        val back = PeerBook.decode(text)
        assertEquals(1, back.size)
        assertEquals("b-phone", back[0].name)
        assertEquals("fe80::1%wlan0", back[0].host)
        assertEquals(80, back[0].battery)
        assertTrue(back[0].charging)
        assertEquals(setOf("self_status"), back[0].tools)
        assertEquals(one.token, back[0].token)
    }

    @Test
    fun decodeSkipsBrokenRows() {
        val text = """[{"name":"","host":"x"},{"name":"ok","host":"1.2.3.4"},{"name":"noHost"}]"""
        val back = PeerBook.decode(text)
        assertEquals(1, back.size)
        assertEquals("ok", back[0].name)
        assertEquals(LanProto.DEFAULT_PORT, back[0].port)
        assertEquals(emptyList<PeerBook.Peer>(), PeerBook.decode("not json"))
    }

    @Test
    fun upsertReplacesByNameIgnoringCase() {
        val list = listOf(peer("B-Phone", battery = 10), peer("c"))
        val out = PeerBook.upsert(list, peer("b-phone", battery = 90))
        assertEquals(2, out.size)
        assertEquals(90, PeerBook.find(out, "b-phone")?.battery)
        assertEquals(90, PeerBook.find(out, "B-PHONE")?.battery)
    }

    @Test
    fun removeIsCaseInsensitive() {
        val out = PeerBook.remove(listOf(peer("b-phone"), peer("c")), "B-PHONE")
        assertEquals(1, out.size)
        assertEquals("c", out[0].name)
        assertNull(PeerBook.find(out, "b-phone"))
    }

    @Test
    fun chargingBeatsHigherBattery() {
        val ranked = PeerBook.rank(
            listOf(peer("full", battery = 100), peer("plugged", battery = 40, charging = true)),
            "self_status",
        )
        assertEquals("plugged", ranked.first().name)
    }

    @Test
    fun lowBatteryIsExcludedUnlessChargingOrUnknown() {
        val ranked = PeerBook.rank(
            listOf(
                peer("low", battery = 5),
                peer("plugged", battery = 5, charging = true),
                peer("unknown", battery = -1),
            ),
            "self_status",
        )
        assertEquals(listOf("plugged", "unknown"), ranked.map { it.name })
    }

    @Test
    fun toolMustBeAdvertised() {
        val peers = listOf(
            peer("no-camera", tools = setOf("self_status")),
            peer("camera", tools = setOf("self_status", "self_ui_screenshot")),
        )
        val ranked = PeerBook.rank(peers, "self_ui_screenshot")
        assertEquals(listOf("camera"), ranked.map { it.name })
        // 没探测过工具表（空集）按「可能有」处理。
        val unknown = PeerBook.rank(listOf(peer("fresh", tools = emptySet())), "self_ui_tap")
        assertEquals(1, unknown.size)
    }

    @Test
    fun writeToolsAreNotFilteredByPeerFlag() {
        // 权限只有一档：选路只看能力，不看每台的「只读/可写」标记。
        val peers = listOf(
            peer("a", tools = setOf("self_ui_tap")),
            peer("b", tools = setOf("self_ui_tap")),
        )
        assertEquals(2, PeerBook.rank(peers, "self_ui_tap").size)
    }

    @Test
    fun fresherPeerWinsAtSameBattery() {
        val ranked = PeerBook.rank(
            listOf(peer("old", lastSeen = 1_000), peer("new", lastSeen = 9_000)),
            "self_status",
        )
        assertEquals("new", ranked.first().name)
    }

    @Test
    fun maskNeverShowsWholeToken() {
        val token = "0123456789abcdef0123456789abcdef"
        val masked = PeerBook.mask(token)
        assertFalse(masked.contains(token))
        assertTrue(masked.startsWith("0123"))
        assertEquals("未设置", PeerBook.mask("short"))
    }

    @Test
    fun lineShowsBatteryAndCapability() {
        val text = PeerBook.line(peer("b", battery = 66, charging = true))
        assertTrue(text.contains("b · "))
        assertTrue(text.contains("66%"))
        assertTrue(text.contains("充电中"))
        assertTrue(text.contains("能力"))
    }
}
