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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 内容呈现层：对照 DSH Go 里体量最大的一块 —— chat/render（代码块 / diff /
 * 数学 / 表格 / mermaid / ansi）、trajectory_sheet / step_detail / turn_process、
 * artifact_preview / media_grid / media_viewer。
 *
 * 天网的消息流已能发文字，但「工具过程折叠成一行、产物能单看、一段富文本能
 * 预览它会被怎么渲染」这些产品层的东西没有。这里做成本机可操作的页：
 * 渲染预览给一个输入框现场分类、轨迹读 [ActionLog] 真数据、产物读 [KB]。
 * 不重写 ChatScreen 的渲染器，只把「看得到、查得到」先补齐。
 */

private fun stamp(t: Long): String = runCatching {
    SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(t))
}.getOrDefault("")

/** 对一段 Markdown 做轻量分类：数出会被几种渲染器接住。对照 chat/render 目录。 */
private fun classify(md: String): List<Pair<String, Int>> {
    val lines = md.lines()
    return listOf(
        "代码块" to lines.count { it.trim().startsWith("```") } / 2,
        "表格" to lines.count { it.trim().startsWith("|") && it.count { c -> c == '|' } >= 2 },
        "数学 $...$" to Regex("\\$[^$]+\\$").findAll(md).count(),
        "标题" to lines.count { it.trimStart().startsWith("#") },
        "diff 行" to lines.count { it.startsWith("+") || it.startsWith("-") },
        "列表" to lines.count { it.trimStart().startsWith("- ") || it.trimStart().startsWith("* ") },
        "引用" to lines.count { it.trimStart().startsWith(">") },
    )
}

@Composable
fun RenderShowcaseScreen() {
    var md by remember {
        mutableStateOf(
            listOf(
                "```kotlin",
                "fun main() = 42",
                "```",
                "",
                "| A | B |",
                "|---|---|",
                "| 1 | 2 |",
                "",
                "# 概览",
                "质能方程写成 E=mc^2 的形态",
                "- 一条列表",
                "+ 新行",
                "- 旧行",
                "> 一段引用",
            ).joinToString("\n")
        )
    }
    val parts = classify(md)
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("富文本渲染预览", "粘一段 Markdown，看它会被代码块/表格/数学/标题/diff 等几种渲染器接住。对照 DSH Go 的 chat/render。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(md, { md = it }, "粘贴 Markdown", singleLine = false)
                Spacer(Modifier.height(6.dp))
                Text("识别结果（${parts.sumOf { it.second }} 处）：", fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                parts.forEach { (k, n) -> FeatureKV(k, n.toString()) }
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("支持的渲染器")
            FeatureCard {
                listOf(
                    "code_block" to "围栏代码块，带语言与复制",
                    "diff_view" to "增删行高亮",
                    "table_syntax" to "管道表格",
                    "markdown_math" to "$...$ / $$...$$ 公式",
                    "mermaid_preview" to "流程图（占位）",
                    "ansi_text" to "终端 ANSI 颜色",
                    "markdown_body" to "正文标题/列表/引用",
                ).forEach { (k, v) -> FeatureKV(k, v) }
            }
            Spacer(Modifier.height(4.dp))
            Text("本页只统计与预览，不做真正的图形渲染；接 ChatScreen 时复用同一套分类器。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 过程轨迹：读本机动作审计，按时间列成步骤。对照 trajectory_sheet / turn_process。 */
@Composable
fun TrajectoryScreen() {
    val records = remember { runCatching { ActionLog.list(80) }.getOrDefault(emptyList()) }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("过程轨迹", "本机远控/工具调用的动作序列，按时间列出。对照 DSH Go 把过程折叠成一行、点开看每步。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard { FeatureKV("记录条数", records.size.toString())
                FeatureKV("来源", "ActionLog（内存 + 本机落盘）") }
            Spacer(Modifier.height(10.dp))
        }
        if (records.isEmpty()) {
            item { FeatureCard { Text("还没有动作记录。跑一次工具调用或远控后，这里会逐步列出。",
                fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) } }
        }
        records.forEach { r ->
            item {
                FeatureCard {
                    Text("${if (r.ok) "✓" else "✗"} ${r.name}", fontSize = 12.sp,
                        color = if (r.ok) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.error)
                    Text("${stamp(r.at)} · ${r.ms}ms", fontSize = 10.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    val line = runCatching { ActionLog.format(r) }.getOrDefault("")
                    if (line.isNotBlank()) {
                        Spacer(Modifier.height(3.dp))
                        Text(line.take(160), fontSize = 10.5.sp, lineHeight = 15.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (r.err.isNotBlank()) Text(r.err.take(120), fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.error)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 产物卡：把 KB 里登记的产物/资源单看一眼。对照 artifact_preview / artifact_card。 */
@Composable
fun ArtifactScreen() {
    var tick by remember { mutableStateOf(0) }
    val rows = remember(tick) { kbList("col_links") + kbList("col_reports") }
    var name by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("产物", "分享链接、检查报告等产物的聚合视图，也能直接登记一条。对照 artifact_card。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(name, { name = it }, "登记一条产物名")
                Spacer(Modifier.height(4.dp))
                FeatureActions("登记" to {
                    if (name.isNotBlank()) {
                        KB.add("col_reports", org.json.JSONObject()
                            .put("name", name.trim()).put("scope", "本机").put("finding", "待补充"))
                        name = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(10.dp))
        }
        item { FeatureSection("聚合（${rows.size}）") }
        rows.forEach { r ->
            item {
                FeatureCard {
                    Text(r["name"].orEmpty().ifBlank { r["title"].orEmpty() }.ifBlank { "（未命名）" },
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                    val sub = r["finding"].orEmpty().ifBlank { r["target"].orEmpty() }
                    if (sub.isNotBlank()) Text(sub.take(100), fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
