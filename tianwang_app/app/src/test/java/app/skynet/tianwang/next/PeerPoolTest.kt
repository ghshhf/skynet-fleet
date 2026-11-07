package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 并行算力的选路与合并是纯逻辑：不需要两台真机，也不加载模型。
 *
 * 真机上唯一剩下的风险是网络，这里先把「叫谁、什么时候加机器、怎么合并」钉死。
 */
class PeerPoolTest {

    private fun node(name: String, battery: Int, charging: Boolean = false, seen: Long = 0) =
        PeerPool.Node(name, battery, charging, seen)

    @Test
    fun firstWavePrefersChargingThenBatteryAndSkipsLowBattery() {
        val nodes = listOf(
            node("low", 10),
            node("mid", 50, seen = 9),
            node("high", 90),
            node("charging", 5, charging = true),
        )
        assertEquals(
            listOf("charging", "high", "mid"),
            PeerPool.firstWave(nodes, parallel = 3).map { it.name },
        )
        assertEquals(listOf("charging", "high"), PeerPool.firstWave(nodes, parallel = 2).map { it.name })
        assertFalse(PeerPool.firstWave(nodes, parallel = 4).any { it.name == "low" })
    }

    @Test
    fun firstWaveIgnoresPcAndApiLanes() {
        val nodes = listOf(
            node("a-phone", 90),
            PeerPool.Node("电脑", lane = PeerPool.Lane.PC),
            PeerPool.Node("API·deepseek", lane = PeerPool.Lane.API),
        )
        assertEquals(listOf("a-phone"), PeerPool.firstWave(nodes, parallel = 4).map { it.name })
    }

    @Test
    fun remoteWavePutsPcBeforeApi() {
        val nodes = listOf(
            PeerPool.Node("API·zhipu", lane = PeerPool.Lane.API),
            PeerPool.Node("a-phone", 90),
            PeerPool.Node("电脑", lane = PeerPool.Lane.PC),
            PeerPool.Node("API·deepseek", lane = PeerPool.Lane.API),
        )
        assertEquals(
            listOf("电脑", "API·deepseek", "API·zhipu"),
            PeerPool.remoteWave(nodes).map { it.name },
        )
    }

    @Test
    fun escalateOnlyAddsUntriedAndStopsAtCap() {
        val nodes = listOf(node("a", 90), node("b", 80), node("c", 70), node("d", 60), node("e", 50))
        val next = PeerPool.escalateWave(nodes, tried = setOf("a", "b"))
        assertEquals(listOf("c", "d"), next.map { it.name })
        assertTrue(PeerPool.escalateWave(nodes, tried = nodes.map { it.name }.toSet()).isEmpty())
    }

    @Test
    fun escalateWhenFirstWaveAllFailedOrLocalOverflowed() {
        val allBad = listOf(PeerPool.Attempt("本机", false, 10, err = "模型正忙"))
        assertTrue(PeerPool.shouldEscalate(allBad, enabled = true, alreadyEscalated = false))

        // 对端答了，但本机明确带不动 ⇒ 拉更多机器进来
        val localOverflow = listOf(
            PeerPool.Attempt("本机", false, 10, err = "上下文超了"),
            PeerPool.Attempt("b-phone", true, 900, text = "答案"),
        )
        assertTrue(PeerPool.shouldEscalate(localOverflow, enabled = true, alreadyEscalated = false))

        // 对端答了、本机只是忙 ⇒ 不再扩散
        val localBusy = listOf(
            PeerPool.Attempt("本机", false, 10, err = "本机模型正忙"),
            PeerPool.Attempt("b-phone", true, 900, text = "答案"),
        )
        assertFalse(PeerPool.shouldEscalate(localBusy, enabled = true, alreadyEscalated = false))
    }

    @Test
    fun escalationObeysSwitchAndHappensOnlyOnce() {
        val bad = listOf(PeerPool.Attempt("本机", false, 10, err = "失败"))
        assertFalse(PeerPool.shouldEscalate(bad, enabled = false, alreadyEscalated = false))
        assertFalse(PeerPool.shouldEscalate(bad, enabled = true, alreadyEscalated = true))
    }

    @Test
    fun overflowRecognizesContextAndMemoryFailures() {
        assertTrue(PeerPool.overflow("上下文长度不够"))
        assertTrue(PeerPool.overflow("CUDA out of memory"))
        assertTrue(PeerPool.overflow("模型加载失败：显存不足"))
        assertFalse(PeerPool.overflow("本机模型正忙"))
        assertFalse(PeerPool.overflow("网络超时"))
    }

    @Test
    fun aggregateRaceUsesFastestAndKeepsOthersAsFootnote() {
        val text = PeerPool.aggregate(
            listOf(
                PeerPool.Attempt("b-phone", true, 900, text = "B 的答案"),
                PeerPool.Attempt("本机", true, 120, text = "本机的答案"),
                PeerPool.Attempt("c-phone", false, 30, err = "连接被关"),
            ),
            PeerPool.Mode.RACE,
        )
        assertTrue(text.contains("由 本机 先回（120ms）"))
        assertTrue(text.contains("本机的答案"))
        assertTrue(text.contains("另外 1 台也回了"))
        assertTrue(text.contains("b-phone"))
        assertTrue(text.contains("c-phone"))
    }

    @Test
    fun aggregateAllListsEveryAnswerAndClipsLongText() {
        val long = "长".repeat(30)
        val text = PeerPool.aggregate(
            listOf(
                PeerPool.Attempt("本机", true, 100, text = long),
                PeerPool.Attempt("b-phone", true, 300, text = long),
            ),
            PeerPool.Mode.ALL,
            clip = 10,
        )
        assertTrue(text.contains("2 台成功"))
        assertTrue(text.contains("—— 本机 · 100ms ——"))
        assertTrue(text.contains("—— b-phone · 300ms ——"))
        assertTrue(text.contains("已截断"))
    }

    @Test
    fun aggregateAllFailedKeepsEveryError() {
        val text = PeerPool.aggregate(
            listOf(
                PeerPool.Attempt("本机", false, 1, err = "没有模型"),
                PeerPool.Attempt("b-phone", false, 2, err = "连接被关"),
            ),
            PeerPool.Mode.RACE,
        )
        assertTrue(text.contains("全部失败（2 台）"))
        assertTrue(text.contains("没有模型"))
        assertTrue(text.contains("连接被关"))
    }
}
