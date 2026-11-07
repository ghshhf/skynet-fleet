package app.skynet.tianwang.next

import java.net.InetAddress
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class Dns6Test {
    @Test
    fun literalDetectionDoesNotResolveNames() {
        assertTrue(Dns6.isLiteral("2408:8000::1"))
        assertTrue(Dns6.isLiteral("[fe80::1%wlan0]"))
        assertTrue(Dns6.isLiteral("192.168.1.7"))
        assertFalse(Dns6.isLiteral("myphone.duckdns.org"))
        assertFalse(Dns6.isLiteral("1.2.3"))
        assertFalse(Dns6.isLiteral(""))
    }

    @Test
    fun resolveLiteralKeepsAddress() {
        val list = Dns6.resolve("::1")
        assertEquals(1, list.size)
        assertTrue(list[0].isLoopbackAddress)
    }

    @Test
    fun resolveNameReturnsLoopbackForLocalhost() {
        val list = Dns6.resolve("localhost")
        assertTrue(list.isNotEmpty())
        assertTrue(list.all { it.isLoopbackAddress })
    }

    @Test
    fun v6OutranksV4() {
        val v6 = InetAddress.getByName("2408:8000::1")
        val ula = InetAddress.getByName("fd00::1")
        val v4 = InetAddress.getByName("192.168.1.7")
        val pub = InetAddress.getByName("8.8.8.8")
        assertTrue(Dns6.score(v6) > Dns6.score(ula))
        assertTrue(Dns6.score(ula) > Dns6.score(v4))
        assertTrue(Dns6.score(v4) > Dns6.score(pub))
    }

    @Test
    fun describeMarksFamily() {
        val text = Dns6.describe("::1")
        assertTrue(text.contains("0:0:0:0:0:0:0:1"))
        assertTrue(text.contains("本机回环"))
    }

    @Test
    fun describeReportsUnknownName() {
        val text = Dns6.describe("no-such-name.invalid")
        assertTrue(text.contains("解析不了") || text.contains("解析失败"))
    }

    @Test
    fun globalV6OnlyReturnsPublicV6() {
        // 测试机可能完全没有全局 IPv6；只断言不会混进 v4 / 链路本地。
        Dns6.globalV6().forEach { ip ->
            assertTrue(ip.contains(":"))
            assertFalse(ip.startsWith("fe80"))
        }
    }
}
