package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject
import kotlin.random.Random

/**
 * 交互工具层：对照 DSH Go 里「点一下就出结果」的小工具 —— tavern_dice_sheet、
 * tavern_storybook / tavern_reading、forum_composer / forum_search、
 * settings_search_page、quick_panel_items_sheet。
 *
 * 每个都有真交互：骰子真掷、故事书真翻页、搜索真过滤、快捷面板真启停，
 * 数据落 [KB]。不是列表占位，不是空按钮。
 */

/** 骰子：真随机数，可配面数与个数，记历史。对照 tavern_dice_sheet。 */
@Composable
fun DiceScreen() {
    var sides by remember { mutableStateOf("20") }
    var count by remember { mutableStateOf("1") }
    var result by remember { mutableStateOf("点「掷」看结果") }
    var tick by remember { mutableStateOf(0) }
    val hist = remember(tick) { kbList("dice_rolls").asReversed() }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("骰子", "真随机数：可配面数与个数，掷完记一条历史。对照酒馆的 dice_sheet。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(sides, { sides = it }, "面数（如 6 / 20 / 100）")
                Spacer(Modifier.height(6.dp))
                FeatureField(count, { count = it }, "一次掷几个")
                Spacer(Modifier.height(4.dp))
                FeatureActions("掷" to {
                    val s = sides.toIntOrNull()?.coerceIn(2, 1000) ?: 6
                    val c = count.toIntOrNull()?.coerceIn(1, 20) ?: 1
                    val rolls = (1..c).map { Random.nextInt(1, s + 1) }
                    result = "${c}d$s → ${rolls.joinToString(" + ")} = ${rolls.sum()}"
                    KB.add("dice_rolls", JSONObject()
                        .put("expr", "${c}d$s").put("rolls", rolls.joinToString(","))
                        .put("sum", rolls.sum().toString()))
                    tick++
                })
                Spacer(Modifier.height(6.dp))
                Text(result, fontSize = 13.sp, color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(10.dp))
        }
        item { FeatureSection("历史（${hist.size}）") }
        hist.take(20).forEach { r ->
            item {
                FeatureCard {
                    Text("${r["expr"].orEmpty()} = ${r["sum"].orEmpty()}", fontSize = 12.sp)
                    Text(r["rolls"].orEmpty(), fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 故事书：把段落登记成一本，逐页翻看。对照 tavern_storybook / reading。 */
@Composable
fun StorybookScreen() {
    var tick by remember { mutableStateOf(0) }
    val pages = remember(tick) { kbList("story_pages") }
    var idx by remember { mutableStateOf(0) }
    var text by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("故事书", "把一段段文字登记成页，逐页翻看。对照 Tavern 的 storybook/reading。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                val cur = pages.getOrNull(idx)
                Text("第 ${pages.size.let { if (it == 0) 0 else idx + 1 }} / ${pages.size} 页",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(4.dp))
                Text(cur?.get("text").orEmpty().ifBlank { "（还没有页面，在下面加一页）" },
                    fontSize = 13.sp, lineHeight = 19.sp,
                    color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(6.dp))
                FeatureActions(
                    "上一页" to { if (idx > 0) idx-- },
                    "下一页" to { if (idx < pages.lastIndex) idx++ },
                    "删本页" to {
                        cur?.let { KB.remove("story_pages", it["id"].orEmpty()) }
                        if (idx > 0) idx--; tick++
                    },
                )
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("加一页")
            FeatureCard {
                FeatureField(text, { text = it }, "这一页的内容", singleLine = false)
                Spacer(Modifier.height(4.dp))
                FeatureActions("追加" to {
                    if (text.isNotBlank()) {
                        KB.add("story_pages", JSONObject().put("text", text.trim()))
                        text = ""; idx = kbList("story_pages").lastIndex.coerceAtLeast(0); tick++
                    }
                })
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 论坛编辑器 + 搜索：写帖子进 col_forum，按标题/板块过滤。对照 forum_composer/forum_search。 */
@Composable
fun ForumComposerScreen() {
    var tick by remember { mutableStateOf(0) }
    val posts = remember(tick) { kbList("col_forum") }
    var title by remember { mutableStateOf("") }
    var board by remember { mutableStateOf("") }
    var body by remember { mutableStateOf("") }
    var query by remember { mutableStateOf("") }
    val shown = posts.filter {
        query.isBlank() || it["title"].orEmpty().contains(query, true) ||
            it["board"].orEmpty().contains(query, true)
    }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("论坛 · 发帖与搜索", "写帖子、按标题/板块搜。数据只存本机，不联网、不上传。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(title, { title = it }, "标题")
                Spacer(Modifier.height(6.dp))
                FeatureField(board, { board = it }, "板块")
                Spacer(Modifier.height(6.dp))
                FeatureField(body, { body = it }, "正文", singleLine = false)
                Spacer(Modifier.height(4.dp))
                FeatureActions("发布" to {
                    if (title.isNotBlank()) {
                        KB.add("col_forum", JSONObject()
                            .put("title", title.trim()).put("board", board.trim())
                            .put("body", body.trim()))
                        title = ""; board = ""; body = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureCard {
                FeatureField(query, { query = it }, "搜索标题 / 板块")
                Text("命中 ${shown.size} / ${posts.size}", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(8.dp))
        }
        shown.forEach { p ->
            item {
                FeatureCard {
                    Text(p["title"].orEmpty(), fontSize = 12.5.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text("[${p["board"].orEmpty()}]", fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.primary)
                    if (p["body"].orEmpty().isNotBlank()) {
                        Spacer(Modifier.height(3.dp))
                        Text(p["body"].orEmpty().take(160), fontSize = 11.sp, lineHeight = 15.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    FeatureActions("删除" to { KB.remove("col_forum", p["id"].orEmpty()); tick++ })
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 设置全局搜索：把所有开关项与集合模块名聚成一个可搜清单。对照 settings_search_page。 */
@Composable
fun SettingsSearchScreen() {
    var query by remember { mutableStateOf("") }
    val entries = remember {
        buildList {
            SettingGroups.interfacePage.forEach { add("界面与交互" to it.label) }
            SettingGroups.privacyPage.forEach { add("隐私与分享" to it.label) }
            SettingGroups.notifyPage.forEach { add("通知设置" to it.label) }
            SettingGroups.voicePage.forEach { add("语音设置" to it.label) }
            SettingGroups.mediaPage.forEach { add("媒体设置" to it.label) }
            SettingGroups.performancePage.forEach { add("性能" to it.label) }
            SettingGroups.replyPrefs.forEach { add("回复偏好" to it.label) }
            SettingGroups.safeMode.forEach { add("安全附加" to it.label) }
            SettingGroups.webMode.forEach { add("网页模式" to it.label) }
            Collections.all.forEach { add("能力模块" to it.title) }
        }
    }
    val shown = entries.filter { query.isBlank() || it.first.contains(query, true) || it.second.contains(query, true) }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("设置搜索", "一次搜遍全部开关与模块名。对照 DSH Go 的 settings_search_page：设置多了要能搜。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard { FeatureField(query, { query = it }, "输入关键词")
                Text("${shown.size} / ${entries.size} 项", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
            Spacer(Modifier.height(8.dp))
        }
        shown.forEach { (group, label) ->
            item {
                FeatureCard { FeatureKV(group, label) }
                Spacer(Modifier.height(5.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 快捷面板：定制下拉快捷项，逐项启停。对照 quick_panel_items_sheet。 */
@Composable
fun QuickPanelScreen() {
    var tick by remember { mutableStateOf(0) }
    val items = remember(tick) {
        val defaults = listOf("开直连", "复制口令", "探活对端", "安全模式", "全局搜索", "出图", "终端", "桌宠")
        val saved = kbList("quick_items")
        if (saved.isEmpty()) defaults.map { mapOf("id" to it, "label" to it, "enabled" to "1") }
        else saved
    }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("快捷面板", "定制下拉里出现哪些快捷项，逐项启停。对照 quick_panel_items_sheet。"); Spacer(Modifier.height(8.dp)) }
        items.forEach { it0 ->
            item {
                val on = it0["enabled"].orEmpty() != "0"
                FeatureCard {
                    FeatureToggle(it0["label"].orEmpty(), "", on) { v ->
                        val arr = KB.array("quick_items")
                        if (arr.length() == 0) {
                            items.forEach { m ->
                                KB.add("quick_items", JSONObject()
                                    .put("id", m["id"]).put("label", m["label"])
                                    .put("enabled", if (m["id"] == it0["id"]) (if (v) "1" else "0") else m["enabled"]))
                            }
                        } else {
                            KB.update("quick_items", it0["id"].orEmpty(),
                                JSONObject().put("enabled", if (v) "1" else "0"))
                        }
                        tick++
                    }
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
