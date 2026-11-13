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

/**
 * 对话增强：对照 DSH Go 的 slash_palette / approval_card / question_card /
 * message_actions / mention / quick_panel / goal_bar / todo_bar / trajectory_sheet。
 *
 * 天网已有 ChatScreen 的消息流，但「输入时的斜杠命令、工具审批、向用户提问、
 * 任务目标条、待办条、消息快捷操作」这些产品层的事没有独立可配置的地方。
 * 这里做成可编辑的本地面板：命令、快捷操作、目标、待办都能增删改并落 [KB]，
 * 供后续 ChatScreen 直接读同一份数据接上，不在这里重写消息流。
 */
object ChatEnhance {
    const val K_SLASH = "chat_slash"
    const val K_ACTIONS = "chat_actions"
    const val K_GOAL = "chat_goal"
    const val K_TODO = "chat_todo"

    val defaultSlash = listOf(
        "/model" to "切换模型",
        "/clear" to "清空当前会话",
        "/compact" to "压缩上下文",
        "/web" to "这次带上联网",
        "/plan" to "先给计划再动手",
        "/diff" to "看这次改动",
        "/share" to "把这段做成分享卡",
    )
    val defaultActions = listOf(
        "复制" to "把这条消息复制到剪贴板",
        "引用" to "把这条作为下一条的引用",
        "重跑" to "从这条重新生成",
        "收藏" to "存进收藏卡片",
        "做成任务" to "把这条登记成待办",
    )
}

@Composable
fun SlashPaletteScreen() {
    var tick by remember { mutableStateOf(0) }
    val custom = remember(tick) { kbList(ChatEnhance.K_SLASH) }
    var cmd by remember { mutableStateOf("") }
    var desc by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("斜杠命令", "输入 / 唤出的命令面板。内置命令固定，自定义命令可增删，配置落本机。"); Spacer(Modifier.height(8.dp)) }
        item { FeatureSection("内置命令") }
        ChatEnhance.defaultSlash.forEach { (c, d) ->
            item {
                FeatureCard {
                    Text(c, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.primary)
                    Text(d, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            FeatureSection("自定义命令")
            FeatureCard {
                FeatureField(cmd, { cmd = it }, "命令，如 /standup")
                Spacer(Modifier.height(6.dp))
                FeatureField(desc, { desc = it }, "说明")
                Spacer(Modifier.height(4.dp))
                FeatureActions("添加" to {
                    if (cmd.isNotBlank()) {
                        KB.add(ChatEnhance.K_SLASH, JSONObject()
                            .put("cmd", cmd.trim()).put("desc", desc.trim()))
                        cmd = ""; desc = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(8.dp))
        }
        custom.forEach { r ->
            item {
                FeatureCard {
                    Text(r["cmd"].orEmpty(), fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                    Text(r["desc"].orEmpty(), fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    FeatureActions("删除" to { KB.remove(ChatEnhance.K_SLASH, r["id"].orEmpty()); tick++ })
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
fun MessageActionsScreen() {
    var tick by remember { mutableStateOf(0) }
    val custom = remember(tick) { kbList(ChatEnhance.K_ACTIONS) }
    var label by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("消息快捷操作", "长按消息能做什么。内置动作固定，另可加自定义动作名。"); Spacer(Modifier.height(8.dp)) }
        item { FeatureSection("内置动作") }
        ChatEnhance.defaultActions.forEach { (a, d) ->
            item {
                FeatureCard { Text(a, fontSize = 12.5.sp); Text(d, fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
                Spacer(Modifier.height(6.dp))
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            FeatureSection("自定义动作")
            FeatureCard {
                FeatureField(label, { label = it }, "动作名")
                Spacer(Modifier.height(4.dp))
                FeatureActions("添加" to {
                    if (label.isNotBlank()) {
                        KB.add(ChatEnhance.K_ACTIONS, JSONObject().put("label", label.trim()))
                        label = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(8.dp))
        }
        custom.forEach { r ->
            item {
                FeatureCard {
                    Text(r["label"].orEmpty(), fontSize = 12.sp)
                    FeatureActions("删除" to { KB.remove(ChatEnhance.K_ACTIONS, r["id"].orEmpty()); tick++ })
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 目标条 + 待办条：这一轮要达成什么、还剩哪些步骤。对照 goal_bar / todo_bar。 */
@Composable
fun GoalTodoScreen() {
    var tick by remember { mutableStateOf(0) }
    val todos = remember(tick) { kbList(ChatEnhance.K_TODO) }
    var goal by remember { mutableStateOf(KB.str(ChatEnhance.K_GOAL, "")) }
    var item by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("目标与待办", "一轮任务的目标一句话，待办逐条勾掉。都只存本机，不建常驻定时器。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureSection("本轮目标")
            FeatureCard {
                FeatureField(goal, { goal = it; KB.setStr(ChatEnhance.K_GOAL, it) }, "这一轮要达成什么", singleLine = false)
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("待办（${todos.size}）")
            FeatureCard {
                FeatureField(item, { item = it }, "新增一条待办")
                Spacer(Modifier.height(4.dp))
                FeatureActions("添加" to {
                    if (item.isNotBlank()) {
                        KB.add(ChatEnhance.K_TODO, JSONObject()
                            .put("text", item.trim()).put("done", "0"))
                        item = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(8.dp))
        }
        todos.forEach { r ->
            item {
                val done = r["done"] == "1"
                FeatureCard(onClick = {
                    KB.update(ChatEnhance.K_TODO, r["id"].orEmpty(),
                        JSONObject().put("done", if (done) "0" else "1"))
                    tick++
                }) {
                    Text((if (done) "✓ " else "○ ") + r["text"].orEmpty(), fontSize = 12.5.sp,
                        color = if (done) MaterialTheme.colorScheme.onSurfaceVariant
                        else MaterialTheme.colorScheme.onSurface)
                    FeatureActions("删除" to { KB.remove(ChatEnhance.K_TODO, r["id"].orEmpty()); tick++ })
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
