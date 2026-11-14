package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** 遥控屏的坐标换算：点图必须打到对端对应的那个像素。 */
class RemoteScreenTest {

    @Test
    fun parseSizeFromStatus() {
        val text = "通道：在线\n屏幕：1080×2400 @420dpi\n截图：可用"
        assertEquals(1080 to 2400, RemoteScreen.parseScreenSize(text))
    }

    @Test
    fun parseSizeAcceptsLowercaseXAndSpaces() {
        assertEquals(720 to 1600, RemoteScreen.parseScreenSize("屏幕: 720 x 1600"))
    }

    @Test
    fun parseSizeRejectsGarbage() {
        assertNull(RemoteScreen.parseScreenSize(""))
        assertNull(RemoteScreen.parseScreenSize("屏幕：未知"))
        assertNull(RemoteScreen.parseScreenSize("屏幕：0×2400"))
    }

    @Test
    fun centerMapsToCenter() {
        val p = RemoteScreen.mapToScreen(
            px = 500f, py = 500f,
            viewW = 1000f, viewH = 1000f,
            imgW = 1080, imgH = 2400,
            screenW = 1080, screenH = 2400,
        )
        assertEquals(540 to 1200, p)
    }

    /** 图片 1080×2400 塞进 1000×1000，实际画幅只有 450 宽，左右各留白 275。 */
    @Test
    fun letterboxPaddingIsIgnored() {
        assertNull(
            RemoteScreen.mapToScreen(
                100f, 500f, 1000f, 1000f, 1080, 2400, 1080, 2400,
            )
        )
        assertEquals(
            0 to 0,
            RemoteScreen.mapToScreen(275f, 0f, 1000f, 1000f, 1080, 2400, 1080, 2400),
        )
        assertEquals(
            1079 to 2399,
            RemoteScreen.mapToScreen(725f, 1000f, 1000f, 1000f, 1080, 2400, 1080, 2400),
        )
    }

    @Test
    fun sameAspectFillsWholeView() {
        assertEquals(
            270 to 540,
            RemoteScreen.mapToScreen(100f, 200f, 400f, 800f, 1080, 2160, 1080, 2160),
        )
    }

    @Test
    fun degenerateInputsReturnNull() {
        assertNull(RemoteScreen.mapToScreen(0f, 0f, 0f, 100f, 1080, 2400, 1080, 2400))
        assertNull(RemoteScreen.mapToScreen(0f, 0f, 100f, 100f, 0, 2400, 1080, 2400))
        assertNull(RemoteScreen.mapToScreen(0f, 0f, 100f, 100f, 1080, 2400, 0, 2400))
    }
}
