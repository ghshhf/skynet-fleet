package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** 本机出图的纯逻辑：patch 命名、请求体、SSE 解析，不碰真机不联网。 */
class ImageEngineLogicTest {

    @Test
    fun patch文件名解析() {
        assertEquals(768 to 768, ImageEngineLogic.parsePatchName("768"))
        assertEquals(512 to 768, ImageEngineLogic.parsePatchName("512x768"))
        assertEquals(1024 to 768, ImageEngineLogic.parsePatchName("1024X768"))
        assertNull(ImageEngineLogic.parsePatchName("upscaler"))
        assertNull(ImageEngineLogic.parsePatchName("512x"))
    }

    @Test
    fun 非512必须带对应patch_没有就不硬跑() {
        val d = Files.createTempDirectory("tw-img").toFile()
        try {
            File(d, "512x768.patch").writeText("x")
            File(d, "768.patch").writeText("x")
            File(d, "1024x768.patch").writeText("x")
            assertNull(ImageEngineLogic.patchFor(d, 512, 512))
            assertEquals(File(d, "512x768.patch"), ImageEngineLogic.patchFor(d, 512, 768))
            assertEquals(File(d, "768.patch"), ImageEngineLogic.patchFor(d, 768, 768))
            assertEquals(File(d, "1024x768.patch"), ImageEngineLogic.patchFor(d, 1024, 768))
            // 拿不到 patch 就返回 null，宁可不出图也不出彩色噪声
            assertNull(ImageEngineLogic.patchFor(d, 640, 640))
            assertNull(ImageEngineLogic.patchFor(d, 768, 512))
        } finally {
            d.deleteRecursively()
        }
    }

    @Test
    fun 请求体字段与边界() {
        val o = JSONObject(ImageEngineLogic.payload("a cat", "blurry", 512, 768, 20, 7f, -1))
        assertEquals("a cat", o.getString("prompt"))
        assertEquals("blurry", o.getString("negative_prompt"))
        assertEquals("png", o.getString("output_format"))
        assertTrue(o.getInt("width") == 512 && o.getInt("height") == 768)
        assertTrue(o.getInt("steps") == 20)
        assertFalse(o.has("seed"))                 // -1 = 随机，不传 seed
        assertFalse(o.getBoolean("show_diffusion_process"))

        val o2 = JSONObject(ImageEngineLogic.payload("x", "", 512, 512, 999, 7f, 42))
        assertTrue(o2.getInt("steps") == 50)      // 上限夹紧
        assertTrue(o2.getLong("seed") == 42L)
    }

    @Test
    fun SSE_complete与error能分辨() {
        var ev = ""
        val r1 = ImageEngineLogic.sseLine("event: complete", ev)
        ev = r1.first
        assertEquals("complete", ev)
        assertNull(r1.second)

        val r2 = ImageEngineLogic.sseLine(
            """data: {"type":"complete","image":"AAA","format":"png"}""", ev)
        assertEquals("AAA", r2.second?.optString("image"))
        assertEquals("png", r2.second?.optString("format"))

        assertNull(ImageEngineLogic.sseLine("data: [DONE]", ev).second)
        assertNull(ImageEngineLogic.sseLine(": ping", ev).second)
        assertNull(ImageEngineLogic.sseLine("", ev).second)

        val e1 = ImageEngineLogic.sseLine("event: error", "")
        assertEquals("error", e1.first)
        val e2 = ImageEngineLogic.sseLine("""data: {"type":"error","message":"PD denied"}""", e1.first)
        assertEquals("PD denied", e2.second?.optString("message"))
    }
}
