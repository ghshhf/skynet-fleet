package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PairTokenTest {
    @Test
    fun matchesPythonWsrelayVectors() {
        // 向量由 tools/cloudnode/wsrelay.py:pair_token 生成，两边必须逐字节一致。
        assertEquals("3663a19cd20b8f79c5451a12152b989c", PairToken.of("phone-1", "s3cr3t"))
        assertEquals("504c2190c54334be45699d8305f25fc2", PairToken.of("room-abc", "fleet-secret"))
    }

    @Test
    fun pairUrlAddsPairOnlyWhenPathEmpty() {
        val bare = McpLinkConfig("wss://relay.example.com", "r", "s")
        assertEquals("wss://relay.example.com/pair", bare.pairUrl())

        val slash = McpLinkConfig("wss://relay.example.com/", "r", "s")
        assertEquals("wss://relay.example.com/pair", slash.pairUrl())

        val custom = McpLinkConfig("wss://relay.example.com/custom", "r", "s")
        assertEquals("wss://relay.example.com/custom", custom.pairUrl())
    }

    @Test
    fun rejectsNonWebSocketScheme() {
        val cfg = McpLinkConfig("https://relay.example.com", "r", "s")
        assertTrue(cfg.validError().contains("ws://"))
    }

    @Test
    fun rejectsMissingCredentials() {
        assertTrue(McpLinkConfig("wss://relay.example.com", "", "s").validError().contains("房间"))
        assertTrue(McpLinkConfig("wss://relay.example.com", "r", "").validError().contains("密钥"))
    }
}
