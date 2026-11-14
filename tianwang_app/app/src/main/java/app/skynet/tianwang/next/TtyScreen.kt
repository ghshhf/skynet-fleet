package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

/**
 * 会话终端：手机端第一个「能持续干活」的界面。
 *
 * 它不是又一个仪表 —— 底层是 [TtySession]，跟 AI 通过 `self_tty_*` 工具用的是**同一个会话**。
 * 人能在这里敲命令，AI 也能往同一个会话写；两边看到的是同一份输出流。
 *
 * 这个屏归「人 / AI 共用」，因为会话本身两边都要用：
 *  - 人：看一眼、续一句、按 Ctrl-C；
 *  - AI：直接调工具拿增量输出，不必经过界面。
 */
@Composable
fun TtyScreen() {
    var sessions by remember { mutableStateOf(TtySession.list()) }
    var current by remember { mutableStateOf("") }
    var input by remember { mutableStateOf("") }
    var enter by remember { mutableStateOf(true) }
    var output by remember { mutableStateOf("") }
    var cursor by remember { mutableStateOf(0L) }
    var notice by remember { mutableStateOf("") }
    var cwd by remember { mutableStateOf("") }

    // 轮询取增量：会话是长驻的，界面只负责把「新出来的那段」贴上来，不重绘全量。
    LaunchedEffect(current) {
        if (current.isBlank()) return@LaunchedEffect
        while (true) {
            val r = TtySession.read(current, cursor, 16_000)
            if (r != null) {
                cursor = r.cursor
                if (r.text.isNotEmpty()) {
                    output = (output + r.text).takeLast(40_000)
                }
                if (!r.running) notice = "会话 $current 已退出（exit=${r.exit ?: "?"}）"
            }
            if (TtySession.list().none { it.id == current }) break
            delay(400)
        }
    }

    fun refresh() {
        sessions = TtySession.list()
    }

    fun open() {
        runCatching { TtySession.open(cwd.trim(), false) }
            .onSuccess { id ->
                current = id
                output = ""
                cursor = 0L
                notice = "已开 $id，可以敲命令了。"
                refresh()
            }
            .onFailure { notice = "开会话失败：${it.message ?: it.javaClass.simpleName}" }
    }

    Column(Modifier.fillMaxSize().padding(14.dp).verticalScroll(rememberScrollState())) {
        FeatureHeader("会话终端", "长驻 shell：stdin 可写、输出流式、不截断；AI 走 self_tty_* 是同一个会话")

        FeatureCard {
            Text("后端：${TtySession.supportedModes()}", fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(3.dp))
            Text("上限 ${TtySession.MAX_SESSIONS} 个会话 · 每个保留最近 ${TtySession.MAX_KEEP / 1024}KB",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(8.dp))
            FeatureField(value = cwd, onChange = { cwd = it }, label = "工作目录（留空用应用私有目录）")
            Spacer(Modifier.height(6.dp))
            FeatureActions(
                "开会话" to { open() },
                "刷新列表" to { refresh(); notice = "已刷新。" },
            )
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("会话（${sessions.size}）")
        if (sessions.isEmpty()) {
            Text("还没有会话。点上面的「开会话」开一个。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            sessions.forEach { s ->
                FeatureCard(onClick = {
                    current = s.id
                    output = ""
                    cursor = 0L
                    notice = "切到 ${s.id}，从当前末尾继续取新输出。"
                }) {
                    Text("${s.id} · ${if (s.running) "运行中" else "已退出(exit=${s.exit ?: "?"})"}",
                        fontSize = 11.5.sp, fontWeight = androidx.compose.ui.text.font.FontWeight.Medium,
                        color = if (s.id == current) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(2.dp))
                    Text("${s.mode.label} · 累计 ${s.total} 字 / 保留 ${s.kept} · ${s.cwd}",
                        fontSize = 9.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(4.dp))
            }
        }

        if (current.isNotBlank()) {
            Spacer(Modifier.height(10.dp))
            FeatureSection("输入 → $current")
            FeatureCard {
                FeatureField(value = input, onChange = { input = it }, label = "命令或要回答的内容（y / 密码 / 路径都行）")
                Spacer(Modifier.height(6.dp))
                FeatureToggle("发送后补回车", "关掉就只写内容，不按回车（适合回答 y/n 之外的交互）", enter) { enter = it }
                Spacer(Modifier.height(6.dp))
                FeatureActions(
                    "发送" to {
                        if (input.isEmpty() && enter) {
                            // 空输入 + 回车 = 直接给一个回车，很多交互程序就等这个。
                            TtySession.write(current, "", true)
                        } else if (TtySession.write(current, input, enter)) {
                            input = ""
                        } else {
                            notice = "写入失败：会话不在了"
                        }
                    },
                    "中断 Ctrl-C" to {
                        TtySession.write(current, "\u0003", false)
                        notice = "已发送 Ctrl-C。"
                    },
                    "关闭会话" to {
                        if (TtySession.close(current)) {
                            notice = "已关闭 $current（先 TERM，1 秒不退才强杀）"
                            output = ""
                            current = ""
                            refresh()
                        } else {
                            notice = "会话已经不在了"
                        }
                    },
                )
                if (notice.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
                }
            }

            Spacer(Modifier.height(10.dp))
            FeatureSection("输出（cursor=$cursor）")
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(9.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(11.dp),
            ) {
                Text(
                    output.ifBlank { "（还没有输出）" },
                    fontSize = 10.5.sp,
                    lineHeight = 15.sp,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
        }

        Spacer(Modifier.height(20.dp))
    }
}
