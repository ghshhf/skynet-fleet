package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 失败诊断与止损是纯逻辑：不需要真机、不加载模型，几毫秒跑完。
 *
 * 这里只测「回包文字 → 事实」的映射和三条硬止损，不测模型会不会说人话。
 */
class ReplanTest {

    private fun fail(name: String, arg: String, err: String, side: Boolean = false) =
        Replan.Step(name, arg, false, err, sideEffect = side)

    private fun ok(name: String, arg: String, text: String = "ok", side: Boolean = false) =
        Replan.Step(name, arg, true, text = text, sideEffect = side)

    @Test
    fun classifyUsesTheMostSpecificFact() {
        assertEquals(Replan.Kind.PERMISSION,
            Replan.classify("无障碍通道没开。去「手机本机」页点「去开启」"))
        assertEquals(Replan.Kind.PERMISSION, Replan.classify("通知监听没授权"))
        assertEquals(Replan.Kind.NO_WINDOW,
            Replan.classify("读不到当前窗口（锁屏、系统界面或无障碍服务刚断开？）"))
        assertEquals(Replan.Kind.REJECTED,
            Replan.classify("手势没被系统接受（锁屏或系统界面？）"))
        assertEquals(Replan.Kind.LOCKED, Replan.classify("屏幕已锁定，点不到 App"))
        assertEquals(Replan.Kind.TIMEOUT, Replan.classify("等了 3000ms 没等到「登录」"))
        assertEquals(Replan.Kind.NOT_FOUND, Replan.classify("当前界面没找到「设置」"))
        assertEquals(Replan.Kind.ARGUMENT, Replan.classify("缺 goal：写清要做什么"))
        assertEquals(Replan.Kind.ARGUMENT,
            Replan.classify("坐标 (2000,300) 不在屏幕内（屏幕 1080×2400）"))
        assertEquals(Replan.Kind.EXEC, Replan.classify("NullPointerException: null"))
        assertEquals(Replan.Kind.UNKNOWN, Replan.classify(""))
        assertEquals(Replan.Kind.UNKNOWN, Replan.classify("说不清为什么"))
    }

    @Test
    fun everyKindGivesAConcreteAlternative() {
        for (k in Replan.Kind.values()) {
            val h = Replan.hint(k, "self_ui_tap")
            assertTrue("$k 的建议不能为空", h.isNotBlank())
            assertTrue("$k 的建议必须指向换路，而不是重试：$h",
                h.contains("不要") || h.contains("停手") || h.contains("换") ||
                    h.contains("先 ") || h.contains("改"))
        }
    }

    @Test
    fun webNotFoundHintForbidsInventingUrls() {
        // 真机样本：1.7B 照着页面里的标题自己拼了个文章链接 → 404 → 又照着拼。
        // 联网工具的建议不能沿用「重新 self_ui_dump」那套界面口径。
        val h = Replan.hint(Replan.Kind.NOT_FOUND, "web_read")
        assertTrue("必须明确禁止拼链接：$h", h.contains("不要自己拼"))
        assertTrue("不该给界面操作建议：$h", !h.contains("self_ui_dump"))
    }

    @Test
    fun duplicateWriteIsBlockedButReadIsNot() {
        val done = listOf(ok("self_ui_tap", "text=设置", side = true))
        assertNotNull("同一个写操作成功过，必须拒绝再执行",
            Replan.blockBefore(done, "self_ui_tap", "text=设置", true))
        assertNull("状态观察类读操作重复不拦（点完再看界面是有意义的）",
            Replan.blockBefore(done, "self_ui_dump", "text=设置", false))
        assertNull("写操作上一次失败，允许换参数再试",
            Replan.blockBefore(listOf(fail("self_ui_tap", "text=设置", "没找到")),
                "self_ui_tap", "text=设置", true))
    }

    @Test
    fun duplicateContentReadIsBlocked() {
        // 真机实测：1.7B 连续 4 次读同一个 URL，把步数烧光、一句答案都没给。
        val done = listOf(ok("web_read", "url=https://example.com", side = false))
        val why = Replan.blockBefore(done, "web_read", "url=https://example.com", false)
        assertNotNull("同一个网页读第二遍必须拦", why)
        assertTrue("拦下时要明确要求用已有结果回答：$why", why!!.contains("回答"))
        // 换一个 URL 是正常的新读取，不能拦
        assertNull("换了 URL 就该放行",
            Replan.blockBefore(done, "web_read", "url=https://other.com", false))
    }

    @Test
    fun permissionAndLockScreenStopImmediately() {
        assertNotNull("权限没开，一步就该停",
            Replan.stall(listOf(fail("self_ui_tap", "x=1", "无障碍通道没开"))))
        assertNotNull("锁屏，一步就该停",
            Replan.stall(listOf(fail("self_ui_tap", "x=1", "屏幕已锁定"))))
    }

    @Test
    fun sameFailureTwiceOrThreeFailuresStop() {
        assertNull("只失败一步，给模型一次换路机会",
            Replan.stall(listOf(fail("self_ui_find", "q=登录", "当前界面没找到「登录」"))))

        assertNotNull("同一动作失败两次，停",
            Replan.stall(listOf(
                fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
                fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
            )))

        assertNotNull("同类失败连续两次，停",
            Replan.stall(listOf(
                fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
                fail("self_ui_find", "q=注册", "当前界面没找到「注册」"),
            )))

        assertNull("两类不同的失败各一次，还可以再试一步",
            Replan.stall(listOf(
                fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
                fail("self_ui_tap", "text=登录", "手势没被系统接受（锁屏？）"),
            )))

        assertNotNull("累计失败三步，停",
            Replan.stall(listOf(
                fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
                fail("self_ui_tap", "text=登录", "手势没被系统接受（锁屏？）"),
                fail("self_ui_dump", "q=", "超时"),
            )))
    }

    @Test
    fun successResetsConsecutiveCountButNotTheLedger() {
        val steps = listOf(
            fail("self_ui_find", "q=登录", "当前界面没找到「登录」"),
            ok("self_ui_dump", ""),
            fail("self_ui_tap", "text=登录", "手势没被系统接受"),
        )
        assertNull("中间成功过一次，连续失败从 1 重新算", Replan.stall(steps))
        val text = Replan.summary(steps)
        assertTrue("账本要写清成功几步", text.contains("成功 1 步"))
        assertTrue("账本要保留失败原因", text.contains("手势没被系统接受"))
    }
}
