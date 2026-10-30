package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 云侧地址归一化 + 端点拼接。
 *
 * 这些用例不是凑数：`no protocol: /api/providers` 这个现场报错，就是
 * 「空地址直接拼路径」造出来的。下面几条断言把它钉死。
 */
class ApiTest {

    @Test
    fun `空地址不兜底 —— 让 UI 分得清没配和配错`() {
        assertEquals("", Api.normalizeBase(""))
        assertEquals("", Api.normalizeBase("   "))
        assertEquals("", Api.normalizeBase("/"))
        assertEquals("", Api.normalizeBase("///"))
    }

    @Test
    fun `尾部斜杠全部去掉`() {
        assertEquals("http://127.0.0.1:9124", Api.normalizeBase("http://127.0.0.1:9124"))
        assertEquals("http://127.0.0.1:9124", Api.normalizeBase("http://127.0.0.1:9124/"))
        assertEquals("http://127.0.0.1:9124", Api.normalizeBase("http://127.0.0.1:9124///"))
        assertEquals("http://127.0.0.1:9124", Api.normalizeBase("  http://127.0.0.1:9124/  "))
    }

    @Test
    fun `没写协议时补 http —— 用户只填 127点0点0点1 冒号 9124 不该报错`() {
        assertEquals("http://127.0.0.1:9124", Api.normalizeBase("127.0.0.1:9124"))
        assertEquals("http://10.0.2.2:9124", Api.normalizeBase("10.0.2.2:9124"))
    }

    @Test
    fun `https 原样保留，大小写不敏感`() {
        assertEquals("https://fleet.example.com", Api.normalizeBase("https://fleet.example.com"))
        assertEquals("HTTPS://fleet.example.com", Api.normalizeBase("HTTPS://fleet.example.com"))
    }

    @Test
    fun `地址为空时端点仍拼成绝对 URL —— 这是 no protocol 的回归`() {
        val url = Api.endpoint("", "/api/providers")
        assertEquals("http://127.0.0.1:9124/api/providers", url)
        assertTrue(url.startsWith("http://"))
        assertFalse(url.startsWith("/"))
    }

    @Test
    fun `端点拼接不会出现双斜杠`() {
        assertEquals(
            "http://127.0.0.1:9124/api/caps",
            Api.endpoint("http://127.0.0.1:9124/", "/api/caps"),
        )
        assertEquals(
            "http://127.0.0.1:9124/api/agent",
            Api.endpoint("127.0.0.1:9124///", "api/agent"),
        )
    }

    @Test
    fun `AgentConfig 有效性的判据`() {
        assertTrue(AgentConfig(url = "http://127.0.0.1:9124", token = "t").valid())
        // 只有 IP:端口 也算有效 —— normalizeBase 会补协议
        assertTrue(AgentConfig(url = "127.0.0.1:9124", token = "t").valid())
        // 空地址不算
        assertFalse(AgentConfig(url = "", token = "t").valid())
        assertFalse(AgentConfig(url = "http://127.0.0.1:9124", token = "").valid())
    }
}
