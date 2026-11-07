package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Test

class WavWriterTest {
    private fun intLE(b: ByteArray, at: Int): Int =
        (b[at].toInt() and 0xff) or
            ((b[at + 1].toInt() and 0xff) shl 8) or
            ((b[at + 2].toInt() and 0xff) shl 16) or
            ((b[at + 3].toInt() and 0xff) shl 24)

    private fun shortLE(b: ByteArray, at: Int): Int =
        (b[at].toInt() and 0xff) or ((b[at + 1].toInt() and 0xff) shl 8)

    @Test
    fun writesCanonical16kMonoPcmHeader() {
        val h = WavWriter.header(3200)          // 0.1 秒 @16k/mono/16bit
        assertEquals(44, h.size)
        assertEquals("RIFF", String(h, 0, 4))
        assertEquals("WAVE", String(h, 8, 4))
        assertEquals("fmt ", String(h, 12, 4))
        assertEquals("data", String(h, 36, 4))

        assertEquals(36 + 3200, intLE(h, 4))
        assertEquals(1, shortLE(h, 20))          // PCM
        assertEquals(1, shortLE(h, 22))          // mono
        assertEquals(16000, intLE(h, 24))
        assertEquals(16000 * 2, intLE(h, 28))    // byte rate
        assertEquals(2, shortLE(h, 32))          // block align
        assertEquals(16, shortLE(h, 34))
        assertEquals(3200, intLE(h, 40))
    }
}
