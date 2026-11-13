package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 自主任务：点一下，手机自己按步骤跑完一串动作，并留下可回看的轨迹。
 *
 * 对照「手机里自己完成任务」的形态，但不靠云端、不靠电脑、不靠 Termux：
 * 每一步都是本机 shell 真执行（建目录、写文件、跑脚本、自检、清理），
 * 结果落到工作区，运行记录进历史，局域网对端也能用 self_code_task 触发。
 */
@Composable
fun TaskAgentScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var picked by remember { mutableStateOf(TaskTemplates.all.first().key) }
    var output by remember { mutableStateOf("还没跑。选一个任务，点「让它自己跑」。") }
    var running by remember { mutableStateOf(false) }

    val tpl = remember(picked) { TaskTemplates.of(picked) }
    val history = remember(tick) { CodeEngine.taskHistory() }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
    ) {
        FeatureHeader("自主任务", "手机自己按步骤跑完一组动作：建目录、写文件、跑脚本、自检、清理。")
        Spacer(Modifier.height(8.dp))

        FeatureCard(highlighted = true) {
            Text("当前引擎：${LinuxRuntime.status().engineLabel}", fontSize = 13.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(4.dp))
            Text("任务里的每一步都是本机真执行，输出原样回显；不联网、不借电脑、不需要 Termux。" +
                "对端也能通过 LAN 调 self_code_task 让这台手机跑。",
                fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("选一个任务")
        TaskTemplates.all.forEach { t ->
            FeatureCard(
                highlighted = picked == t.key,
                onClick = { picked = t.key },
            ) {
                Text(t.label, fontSize = 12.5.sp,
                    color = if (picked == t.key) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(3.dp))
                Text(t.sub, fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text("${t.steps.size} 步 · " + t.steps.joinToString(" → ") { it.label },
                    fontSize = 10.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(6.dp))
        }

        Spacer(Modifier.height(6.dp))
        FeatureActions(
            "让它自己跑" to {
                if (!running) {
                    running = true
                    output = "执行中：${tpl.label}（${tpl.steps.size} 步）…"
                    val key = picked
                    scope.launch(Dispatchers.IO) {
                        val log = CodeEngine.runTask(key) { _, _, _ -> }
                        withContext(Dispatchers.Main) {
                            output = log
                            running = false
                            tick++
                        }
                    }
                }
            },
            "看运行历史" to { onOpen("CODEHISTORY") },
            "看工作区" to { onOpen("WORKSPACE") },
            "运行时设置" to { onOpen("RUNTIME") },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("执行轨迹")
        FeatureCard {
            Text(output, fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurface)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("任务历史（${history.size}）")
        if (history.isEmpty()) {
            Text("还没有任务记录。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            history.take(8).forEach { h ->
                FeatureCard {
                    Text("${h["label"]} · ${h["steps"]} 步", fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text((h["log"].orEmpty()).take(160).replace('\n', ' '), fontSize = 10.sp,
                        maxLines = 2, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}
