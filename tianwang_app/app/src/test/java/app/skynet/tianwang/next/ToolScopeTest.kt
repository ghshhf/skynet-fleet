package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 工具面裁剪纯逻辑测试：不碰 Android、不加载模型。
 *
 * 守的是两条硬线：
 *  1. **每个工具都必须有归属域** —— 漏一个，那一域的目标就永远拿不到它；
 *  2. **给出的一律 ≤ limit** —— 总纲实测 58 个工具 56.2 秒，超时线 25 秒。
 */
class ToolScopeTest {

    /** 真实工具名（与 PhoneMcp 的 63 个一一对应）。 */
    private val names = listOf(
        "self_status", "self_ui_dump", "self_ui_find", "self_ui_tap", "self_ui_tap_at",
        "self_ui_swipe", "self_ui_text", "self_ui_scroll", "self_ui_key", "self_ui_wait",
        "self_app_launch", "self_app_open_url", "self_app_list", "self_ui_screenshot",
        "self_notif_list", "self_notif_clear", "self_clipboard_get", "self_clipboard_set",
        "self_battery_status", "self_audio_volume", "self_audio_set_volume", "self_net_status",
        "self_device_info", "self_screen_state", "self_diag", "self_audit", "self_perm_matrix",
        "self_shizuku_status", "self_shizuku_request", "self_shell_exec", "self_owner_status",
        "self_owner_provision", "self_owner_grant_perm", "self_owner_restriction",
        "self_owner_reboot", "self_lan_status", "self_lan_start", "self_lan_stop",
        "self_lan_call", "self_lan_speed", "self_peer_add", "self_peer_remove", "self_peer_list",
        "self_peer_probe", "self_peer_pick", "self_peer_call", "self_agent_goal",
        "self_peer_goal", "self_model_ask", "self_api_ask", "self_peer_pool", "self_dns_resolve",
        "self_ddns_status", "self_ddns_config", "self_ddns_publish", "self_ddns_watch",
        "cloud_nodes", "cloud_list", "cloud_search", "cloud_file_url",
        "web_search", "web_read", "web_digest",
    )

    private fun table(list: List<String> = names): List<McpTool> =
        list.map { McpTool(it, "", JSONObject()) }

    private fun pickedNames(goal: String, limit: Int = ToolScope.DEFAULT_LIMIT): List<String> =
        ToolScope.pick(goal, table(), limit).tools.map { it.name }

    @Test
    fun everyToolHasDomain() {
        assertEquals(63, names.size)
        for (n in names) {
            assertTrue("工具 $n 没有归属域，那一域的目标永远拿不到它", ToolScope.domainLabel(n).isNotBlank())
        }
    }

    @Test
    fun uiGoalPicksUiTools() {
        val got = pickedNames("帮我点一下设置里的关于手机")
        assertTrue("应给到 self_ui_tap，实际：$got", "self_ui_tap" in got)
        assertTrue("不该出现网盘工具，实际：$got", got.none { it.startsWith("cloud_") })
        assertTrue(got.size <= ToolScope.DEFAULT_LIMIT)
    }

    @Test
    fun cloudGoalPicksCloudTools() {
        val got = pickedNames("网盘里搜一下上个月的报告")
        // 网盘域只有 4 个工具，必须一个不漏地给全
        for (n in listOf("cloud_nodes", "cloud_list", "cloud_search", "cloud_file_url")) {
            assertTrue("网盘目标漏了 $n，实际：$got", n in got)
        }
        // 余下名额只能是通用底座（status/dump/find/tap），不能塞别的领域来占 token
        assertTrue("不该出现 ddns，实际：$got", got.none { it.startsWith("self_ddns_") })
        assertTrue(
            "不该出现高权限工具，实际：$got",
            got.none { it.startsWith("self_shell_") || it.startsWith("self_owner_") },
        )
        assertTrue(got.size <= ToolScope.DEFAULT_LIMIT)
    }

    @Test
    fun longWordHitBeatsCoreOrder() {
        // 「截图」这种长词命中描述时，不在 core 里的工具也必须挤进预算内
        val withDesc = names.map {
            McpTool(
                it,
                when (it) {
                    "self_ui_screenshot" -> "系统无障碍截图，回传压缩后的 JPEG"
                    "self_ui_key" -> "发 back / home 全局动作"
                    else -> ""
                },
                JSONObject(),
            )
        }
        val got = ToolScope.pick("帮我截图一下", withDesc).tools.map { it.name }
        assertTrue("说截图却没给截图工具，实际：$got", "self_ui_screenshot" in got)
    }

    @Test
    fun batteryGoalPicksBattery() {
        val got = pickedNames("看看电量还有多少")
        assertTrue("应给到 self_battery_status，实际：$got", "self_battery_status" in got)
    }

    @Test
    fun peerGoalPicksPeer() {
        val got = pickedNames("派给另一台手机去做")
        assertTrue("应给到集群工具，实际：$got", got.any { it.startsWith("self_peer_") })
    }

    @Test
    fun ddnsGoalPicksDdns() {
        val got = pickedNames("把公网 ipv6 发布到我的域名")
        assertTrue("应给到 ddns 工具，实际：$got", got.any { it.startsWith("self_ddns_") })
    }

    @Test
    fun notifyGoalPicksNotify() {
        val got = pickedNames("把剪贴板里的内容复制下来")
        assertTrue("应给到剪贴板工具，实际：$got", got.any { it.startsWith("self_clipboard_") })
    }

    /** 动作类界面工具：补位时混进任何一个都可能在真机上产生一次真实点击。 */
    private val ACTION_UI = setOf(
        "self_ui_tap", "self_ui_tap_at", "self_ui_text", "self_ui_swipe",
        "self_ui_key", "self_ui_scroll", "self_ui_wait",
    )

    @Test
    fun smallDomainsNeverFillWithActionTools() {
        // 真机实测（0.1.42）：联网目标补位到 8 个时把 self_ui_tap 塞进了工具面，
        // 1.7B 读完正文就去点页面标题 —— 用户要的是结论，它去开浏览器，
        // 还因为无障碍没开把整轮任务拖进失败。补位只能是观察，不能是操作。
        // 联网 / 网络接入 / 通知剪贴板三个域各只有 3 个工具，都会被补位覆盖。
        val cases = mapOf(
            "读一下这个网页讲了什么 https://example.com" to "web_digest",
            "把公网 ipv6 发布到我的域名" to "self_ddns_publish",
            "把剪贴板里的内容复制下来" to "self_clipboard_get",
        )
        for ((goal, expected) in cases) {
            val got = pickedNames(goal)
            assertTrue("「$goal」应给到 $expected，实际：$got", expected in got)
            val bad = got.filter { it in ACTION_UI }
            assertTrue("「$goal」不该拿到动作工具 $bad（全表 $got）", bad.isEmpty())
            // 也不该为了凑满预算拉一堆无关工具进来
            assertTrue("「$goal」不该硬凑满 8 个，实际 ${got.size} 个：$got", got.size <= 6)
        }
    }

    @Test
    fun unknownGoalFallsBackToCommon() {
        // 一句既不是操作、也不是查询、更没有具体对象的话 —— 任何域都不该抢
        val r = ToolScope.pick("嗯，你看着办吧", table())
        assertEquals("通用", r.domain)
        assertTrue("兜底必须带 self_status，实际：${r.tools.map { it.name }}",
            r.tools.any { it.name == "self_status" })
        assertEquals(ToolScope.DEFAULT_LIMIT, r.tools.size)
    }

    @Test
    fun webGoalPicksWebTools() {
        val got = pickedNames("上网搜一下今天有什么新闻")
        assertTrue("应给到 web_search，实际：$got", "web_search" in got)
        assertTrue("不该出现网盘工具，实际：$got", got.none { it.startsWith("cloud_") })
        assertTrue(got.size <= ToolScope.DEFAULT_LIMIT)
    }

    @Test
    fun weatherGoalGoesOnline() {
        // 查天气必须联网。落到通用域的话，模型手里没有联网工具，只会答「我做不到」。
        assertTrue("天气应命中联网域", "web_search" in pickedNames("今天天气怎么样"))
    }

    @Test
    fun cloudVetoesWebDomain() {
        // 「网盘里搜一下」是网盘动作。联网域不否决的话会靠「搜一下」平分，
        // 两边各给一半工具，小模型反而更容易选错。
        val r = ToolScope.pick("网盘里搜一下上个月的报告", table())
        assertEquals("网盘文件", r.domain)
        assertTrue(
            "联网工具不该混进网盘目标：${r.tools.map { it.name }}",
            r.tools.none { it.name.startsWith("web_") },
        )
    }

    @Test
    fun englishWebGoalPicksWebTools() {
        assertTrue(
            "search the web 应命中联网域",
            "web_search" in pickedNames("search the web for python tutorial"),
        )
    }

    @Test
    fun bareUrlGoesOnline() {
        // 最自然的用法：直接甩一条链接，句子里没有「网页/搜索」任何词。
        // 不把 http(s) 当硬信号的话，这一条会落到通用域，模型只能说「做不到」。
        val got = pickedNames("https://example.com/a?x=1")
        assertTrue("裸链接必须进联网域，实际：$got", "web_digest" in got)
        assertTrue("多篇汇总应排在最前，实际：$got", got.indexOf("web_digest") < got.indexOf("web_read"))
    }

    @Test
    fun shortListIsNotNarrowed() {
        val small = table(listOf("self_status", "self_ui_dump", "self_ui_find"))
        val r = ToolScope.pick("点一下设置", small)
        assertFalse("本来就没超预算，不该标记为裁剪", r.narrowed)
        assertEquals(3, r.tools.size)
    }

    @Test
    fun limitIsRespected() {
        for (goal in listOf("点一下设置", "网盘搜文件", "派给另一台", "看看电量", "天气怎么样")) {
            val r = ToolScope.pick(goal, table(), limit = 5)
            assertTrue("「$goal」超出 limit：${r.tools.map { it.name }}", r.tools.size <= 5)
        }
    }

    @Test
    fun peerGoalAndPeerPoolBelongToModel() {
        // 名字带 peer，但语义是「让模型算」，归错域就会把算力目标导到集群工具上。
        assertEquals("模型与算力", ToolScope.domainLabel("self_peer_goal"))
        assertEquals("模型与算力", ToolScope.domainLabel("self_peer_pool"))
        assertEquals("集群协作", ToolScope.domainLabel("self_peer_call"))
        assertEquals("集群协作", ToolScope.domainLabel("self_lan_status"))
    }

    @Test
    fun pickedToolsAlwaysComeFromInputTable() {
        val all = table()
        for (goal in listOf("点一下", "网盘", "派单", "电量", "随便说点什么")) {
            val r = ToolScope.pick(goal, all)
            assertNotNull(r.domain)
            for (t in r.tools) {
                assertTrue("裁出了不存在的工具 ${t.name}", all.any { it.name == t.name })
            }
        }
    }

    @Test
    fun englishGoalPicksSameDomain() {
        // 对等节点派单常用英文，域判断不能只认中文
        assertTrue("tap 应命中界面域", "self_ui_tap" in pickedNames("tap the Settings button"))
        assertTrue(
            "cloud 应命中网盘域",
            pickedNames("search my cloud folder").any { it.startsWith("cloud_") },
        )
        assertTrue(
            "battery 应命中系统域",
            "self_battery_status" in pickedNames("what is the battery level"),
        )
    }

    @Test
    fun englishWordBoundaryDoesNotLeak() {
        // 「profile」里有 file、「report」里有 port：用 contains 会被误判到网盘/网络域
        val r = ToolScope.pick("show my profile page", table())
        assertTrue("被 file/profile 误判到网盘域：${r.domain}", r.domain != "网盘文件")
        val r2 = ToolScope.pick("write a short report", table())
        assertTrue("被 port/report 误判到网络域：${r2.domain}", r2.domain != "网络接入")
    }
}
