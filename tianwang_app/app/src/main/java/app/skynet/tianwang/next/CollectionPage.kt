package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject

/**
 * 集合页：把「一个模块 = 一组可增删改的本地记录」做成可复用组件。
 *
 * 对照 DSH Go 里成片的产品模块 —— Bots / Rooms / Team / Plugins / Skills /
 * Presets / Schedules / Forum / Tavern(角色·世界书·正则·快捷回复) / App rules /
 * Hosted links / Workspaces —— 它们的骨架都是「列表 + 新建表单 + 启用开关 + 删除」。
 * 与其每个写一遍 RecyclerView 样板，这里用 [CollectionSpec] 描述字段，一套 UI 全渲染。
 *
 * 纪律：每条记录都是本机 [KB] 里的一行 JSON，不联网、不上传、不含密钥；
 * 「启用」只改本地标记，是否真的驱动业务由对应模块后续接，不自称已生效。
 */

/** 一个字段：key 存进 JSON，label 给人看，multiline 决定是否多行输入。 */
data class ColField(val key: String, val label: String, val multiline: Boolean = false)

/** 一个模块：key 同时是 KB 的键，fields 决定新建表单与卡片展示。 */
data class CollectionSpec(
    val key: String,
    val title: String,
    val sub: String,
    val fields: List<ColField>,
    val itemHint: String = "点卡片看内容 · 可启用/停用/删除",
    val defaultEnabled: Boolean = true,
)

/** 模块注册表：新增一个产品模块 = 加一行 spec，无需再写 UI。 */
object Collections {
    val bots = CollectionSpec("col_bots", "Bot 与智能体", "对照 DSH Go 的 Bots：给每个 Bot 起名字、写人设、绑模型、单独启停。",
        listOf(ColField("name", "名称"), ColField("role", "人设 / 系统提示", true), ColField("model", "模型")))

    val rooms = CollectionSpec("col_rooms", "群聊房间", "多个 Bot/人在一个房间里协作，可增删房间、写房间说明。",
        listOf(ColField("name", "房间名"), ColField("topic", "话题", true), ColField("members", "成员（逗号分隔）")))

    val team = CollectionSpec("col_team", "Agent 团队", "把多个席位编成一个团队：席位名、职责、上级。",
        listOf(ColField("name", "席位名"), ColField("duty", "职责", true), ColField("lead", "上级席位")))

    val plugins = CollectionSpec("col_plugins", "插件与工具卡", "已装/可用的原生工具卡清单，逐个启停、注明来源。",
        listOf(ColField("name", "插件名"), ColField("version", "版本"), ColField("source", "来源"), ColField("note", "说明", true)))

    val skills = CollectionSpec("col_skills", "技能与预设", "Agent 预设、技能包、斜杠命令：名字 + 触发词 + 内容。",
        listOf(ColField("name", "名称"), ColField("trigger", "触发词"), ColField("content", "内容 / 提示词", true)))

    val schedules = CollectionSpec("col_schedules", "日程与定时任务", "cron 表达式 + 要跑的动作 + 备注，先登记再接调度器。",
        listOf(ColField("name", "任务名"), ColField("cron", "cron，如 0 9 * * *"), ColField("action", "动作", true)),
        itemHint = "仅登记，不自动执行；本项目不建常驻定时器")

    val jobs = CollectionSpec("col_jobs", "作业队列", "长任务登记：目标、状态、结果摘要。",
        listOf(ColField("name", "作业名"), ColField("target", "目标"), ColField("status", "状态"), ColField("result", "结果摘要", true)))

    val forum = CollectionSpec("col_forum", "社区论坛", "对照 DSH Go 的 Forum：本地发帖、看帖、记板块。",
        listOf(ColField("title", "标题"), ColField("board", "板块"), ColField("body", "正文", true)))

    val characters = CollectionSpec("col_characters", "酒馆 · 角色卡", "Tavern 角色扮演的角色卡：名字、设定、开场白。",
        listOf(ColField("name", "角色名"), ColField("desc", "设定", true), ColField("greeting", "开场白", true)))

    val lorebooks = CollectionSpec("col_lorebooks", "酒馆 · 世界书", "关键词触发的背景设定条目。",
        listOf(ColField("key", "关键词"), ColField("content", "世界观内容", true), ColField("order", "优先级")))

    val regex = CollectionSpec("col_regex", "酒馆 · 正则脚本", "对文本做替换的正则规则：查找、替换、作用目标。",
        listOf(ColField("name", "规则名"), ColField("find", "查找正则"), ColField("replace", "替换为"), ColField("scope", "作用目标")))

    val quickReplies = CollectionSpec("col_quick_replies", "快捷回复", "常用话术一键插入：标题 + 内容。",
        listOf(ColField("title", "标题"), ColField("content", "内容", true)))

    val rules = CollectionSpec("col_rules", "应用规则", "手机操控的分级规则：对哪些 App 允许/需确认/禁止，写明包名。",
        listOf(ColField("name", "规则名"), ColField("package", "包名"), ColField("level", "等级 allow/ask/deny"), ColField("note", "备注", true)))

    val links = CollectionSpec("col_links", "分享链接", "对照 DSH Go 的托管分享链接：标题、目标、有效期。",
        listOf(ColField("title", "标题"), ColField("target", "指向"), ColField("expire", "有效期")))

    val workspaces = CollectionSpec("col_workspaces", "工作区", "对照 workspace/files：登记工作区目录与用途。",
        listOf(ColField("name", "名称"), ColField("path", "路径"), ColField("note", "用途", true)))

    val folders = CollectionSpec("col_folders", "会话文件夹", "对照 drawer/folder_*：把会话按文件夹归类，登记名称与配色。",
        listOf(ColField("name", "文件夹名"), ColField("color", "标记色"), ColField("note", "备注", true)))

    val bookmarks = CollectionSpec("col_bookmarks", "书签", "对照 tavern_bookmarks：在长对话里插书签，记位置与摘要。",
        listOf(ColField("name", "书签名"), ColField("at", "位置"), ColField("note", "摘要", true)))

    val branches = CollectionSpec("col_branches", "对话分支", "对照 tavern_branches：从某条消息分叉出的支线。",
        listOf(ColField("name", "分支名"), ColField("from", "从哪条"), ColField("note", "说明", true)))

    val presets = CollectionSpec("col_presets", "回复预设", "对照 tavern_presets：一组提示词/参数的组合，可启停。",
        listOf(ColField("name", "预设名"), ColField("params", "参数", true), ColField("note", "说明", true)))

    val personas = CollectionSpec("col_personas", "人格面具", "对照 tavern_personas：切换说话风格的人设。",
        listOf(ColField("name", "人格名"), ColField("style", "风格", true), ColField("note", "备注", true)))

    val providers = CollectionSpec("col_providers", "模型提供方", "对照 providers_page：登记 API 地址、模型名、备注（不存密钥）。",
        listOf(ColField("name", "名称"), ColField("base", "API 地址"), ColField("model", "模型名"), ColField("note", "备注", true)))

    val subscriptions = CollectionSpec("col_subscriptions", "订阅与用量", "对照 subscription_page：登记套餐与额度提醒线。",
        listOf(ColField("name", "名称"), ColField("quota", "额度"), ColField("reset", "重置周期")))

    val reports = CollectionSpec("col_reports", "检查报告", "对照 tavern_report_page：一次审查的结论与建议。",
        listOf(ColField("name", "报告名"), ColField("scope", "范围"), ColField("finding", "结论", true)))

    val runs = CollectionSpec("col_runs", "运行记录", "对照 tavern_runs_page：登记每次长跑的输入、结果与耗时。",
        listOf(ColField("name", "标题"), ColField("input", "输入", true), ColField("result", "结果", true)))

    val all = listOf(
        bots, rooms, team, plugins, skills, schedules, jobs, forum,
        characters, lorebooks, regex, quickReplies, rules, links, workspaces,
        folders, bookmarks, branches, presets, personas, providers,
        subscriptions, reports, runs,
    )

    fun byKey(k: String): CollectionSpec = all.firstOrNull { it.key == k } ?: bots
}

@Composable
fun CollectionScreen(spec: CollectionSpec) {
    var tick by remember { mutableStateOf(0) }
    val rows = remember(tick) { kbList(spec.key) }
    var showAdd by remember { mutableStateOf(false) }
    var detail by remember { mutableStateOf<Map<String, String>?>(null) }
    // 表单值：按字段 key 存一份可变状态
    val form = remember { androidx.compose.runtime.mutableStateMapOf<String, String>() }

    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item {
            FeatureHeader(spec.title, spec.sub)
            Spacer(Modifier.height(8.dp))
        }
        item {
            FeatureActions(
                "新建" to { form.clear(); showAdd = true },
                "全部启用" to {
                    kbList(spec.key).forEach {
                        KB.update(spec.key, it["id"].orEmpty(), JSONObject().put("enabled", "1"))
                    }
                    tick++
                },
                "全部停用" to {
                    kbList(spec.key).forEach {
                        KB.update(spec.key, it["id"].orEmpty(), JSONObject().put("enabled", "0"))
                    }
                    tick++
                },
            )
            Spacer(Modifier.height(4.dp))
            Text("${rows.size} 条 · ${spec.itemHint}",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(8.dp))
        }
        rows.forEach { row ->
            item {
                val on = row["enabled"].orEmpty().ifBlank {
                    if (spec.defaultEnabled) "1" else "0"
                } == "1"
                val head = row[spec.fields.firstOrNull()?.key.orEmpty()].orEmpty()
                    .ifBlank { "（未命名）" }
                FeatureCard(onClick = { detail = row }) {
                    Text(head, fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                    val second = spec.fields.getOrNull(1)
                    if (second != null) {
                        val v = row[second.key].orEmpty()
                        if (v.isNotBlank()) {
                            Text(v.take(80), fontSize = 10.5.sp, maxLines = 2,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                    Spacer(Modifier.height(4.dp))
                    FeatureToggle(
                        label = if (on) "已启用" else "已停用",
                        sub = "启用状态只存本机标记，不自动触发联网或调度",
                        checked = on,
                        onChange = { v ->
                            KB.update(spec.key, row["id"].orEmpty(),
                                JSONObject().put("enabled", if (v) "1" else "0"))
                            tick++
                        },
                    )
                    FeatureActions(
                        "删除" to { KB.remove(spec.key, row["id"].orEmpty()); tick++ },
                    )
                }
                Spacer(Modifier.height(8.dp))
            }
        }
        if (rows.isEmpty()) {
            item {
                FeatureCard {
                    Text("还没有内容。点「新建」加第一条，配置只存这台手机。",
                        fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }

    if (showAdd) {
        AlertDialog(
            onDismissRequest = { showAdd = false },
            title = { Text("新建 · ${spec.title}") },
            text = {
                androidx.compose.foundation.layout.Column {
                    spec.fields.forEach { f ->
                        FeatureField(
                            value = form[f.key] ?: "",
                            onChange = { form[f.key] = it },
                            label = f.label,
                            singleLine = !f.multiline,
                        )
                        Spacer(Modifier.height(6.dp))
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val o = JSONObject()
                    spec.fields.forEach { f -> o.put(f.key, form[f.key] ?: "") }
                    o.put("enabled", if (spec.defaultEnabled) "1" else "0")
                    KB.add(spec.key, o)
                    showAdd = false
                    tick++
                }) { Text("保存") }
            },
            dismissButton = {
                TextButton(onClick = { showAdd = false }) { Text("取消") }
            },
        )
    }

    detail?.let { d ->
        AlertDialog(
            onDismissRequest = { detail = null },
            title = { Text(d[spec.fields.firstOrNull()?.key.orEmpty()].orEmpty().ifBlank { "详情" }) },
            text = {
                androidx.compose.foundation.layout.Column {
                    spec.fields.forEach { f ->
                        FeatureKV(f.label, d[f.key].orEmpty().ifBlank { "—" })
                    }
                    FeatureKV("启用", if (d["enabled"] == "0") "否" else "是")
                }
            },
            confirmButton = {
                TextButton(onClick = { detail = null }) { Text("关闭") }
            },
        )
    }
}
