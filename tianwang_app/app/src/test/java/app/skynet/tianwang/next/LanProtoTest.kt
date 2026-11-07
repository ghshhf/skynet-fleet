package app.skynet.tianwang.next

import java.net.InetAddress
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LanProtoTest {
    @Test
    fun ipv6EndpointNeedsBrackets() {
        assertEquals("[fe80::1%wlan0]:18765", LanProto.Target("fe80::1%wlan0").endpoint())
        assertEquals("[2408:8000::1]:9000", LanProto.Target("2408:8000::1", 9000).endpoint())
        assertEquals("192.168.1.7:18765", LanProto.Target("192.168.1.7").endpoint())
    }

    @Test
    fun parseBracketedV6WithPort() {
        val t = LanProto.parse("[fe80::1%wlan0]:9001")
        assertNotNull(t)
        assertEquals("fe80::1%wlan0", t!!.host)
        assertEquals(9001, t.port)
    }

    @Test
    fun parseBareV6UsesDefaultPort() {
        val t = LanProto.parse("2408:8000::1")
        assertEquals("2408:8000::1", t!!.host)
        assertEquals(LanProto.DEFAULT_PORT, t.port)
    }

    @Test
    fun parseV4AndPort() {
        assertEquals(18765, LanProto.parse("192.168.1.7")!!.port)
        assertEquals(9000, LanProto.parse("192.168.1.7:9000")!!.port)
        assertEquals(null, LanProto.parse("192.168.1.7:70000"))
        assertEquals(null, LanProto.parse("[::1"))
    }

    @Test
    fun familyClassifiesV6AndV4() {
        assertEquals(LanProto.Family.LOOPBACK, LanProto.familyOf(InetAddress.getByName("::1")))
        assertEquals(LanProto.Family.LINK_LOCAL_V6, LanProto.familyOf(InetAddress.getByName("fe80::1")))
        assertEquals(LanProto.Family.ULA_V6, LanProto.familyOf(InetAddress.getByName("fd00::1")))
        assertEquals(
            LanProto.Family.GLOBAL_V6,
            LanProto.familyOf(InetAddress.getByName("2408:8000::1")),
        )
        assertEquals(LanProto.Family.PRIVATE_V4, LanProto.familyOf(InetAddress.getByName("10.0.0.1")))
        assertEquals(LanProto.Family.PUBLIC_V4, LanProto.familyOf(InetAddress.getByName("8.8.8.8")))
    }

    @Test
    fun localAddressesExcludeLoopback() {
        val list = LanProto.localAddresses()
        assertTrue(list.none { it.family == LanProto.Family.LOOPBACK })
        assertTrue(list.none { it.host.startsWith("127.") })
    }

    @Test
    fun tokenComparisonIsExact() {
        assertTrue(LanProto.tokenEquals("abcd", "abcd"))
        assertFalse(LanProto.tokenEquals("abcd", "abce"))
        assertFalse(LanProto.tokenEquals("", ""))
        assertFalse(LanProto.tokenEquals("abcd", "abcde"))
    }

    @Test
    fun tokenIsLongAndRandom() {
        val a = LanProto.newToken()
        val b = LanProto.newToken()
        assertEquals(32, a.length)
        assertTrue(a != b)
    }

    @Test
    fun toolRequestShape() {
        val req = JSONObject(LanProto.toolRequest("self_ui_dump", JSONObject().put("limit", 3)))
        assertEquals("tools/call", req.getString("method"))
        assertEquals("self_ui_dump", req.getJSONObject("params").getString("name"))
        assertEquals(3, req.getJSONObject("params").getJSONObject("arguments").getInt("limit"))
    }

    @Test
    fun summarizeHidesImageBody() {
        val reply = JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", 1)
            .put("result", JSONObject().put("content", org.json.JSONArray()
                .put(JSONObject().put("type", "text").put("text", "已截屏"))
                .put(JSONObject().put("type", "image")
                    .put("mimeType", "image/jpeg").put("data", "A".repeat(1000)))))
            .toString()
        val text = LanProto.summarizeMcpReply(reply)
        assertTrue(text.contains("已截屏"))
        assertTrue(text.contains("[图片 image/jpeg，1000 字符 base64]"))
        assertFalse(text.contains("AAAA"))
    }

    @Test
    fun summarizeSurfacesError() {
        val reply = JSONObject()
            .put("jsonrpc", "2.0").put("id", 1)
            .put("error", JSONObject().put("code", -32601).put("message", "未知方法：x"))
            .toString()
        assertTrue(LanProto.summarizeMcpReply(reply).contains("未知方法：x"))
    }

    @Test
    fun speedLineReportsMbps() {
        // 1MB 在 8ms 内传完 = 1000 Mbps 左右。
        val line = LanProto.speedLine(1_000_000, 8.0)
        assertTrue(line.contains("MB/s"))
        assertTrue(line.contains("Mbps"))
    }
}
