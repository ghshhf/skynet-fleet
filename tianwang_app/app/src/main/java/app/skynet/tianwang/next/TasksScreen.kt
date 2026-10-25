package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** 任务进度（开发时手机看整体进度）：阶段 + 近期提交 + 分支。 */
@Composable
fun TasksScreen() {
    var board by remember { mutableStateOf<TaskBoard?>(null) }
    var err by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        if (Net.hasConfig()) {
            runCatching { Net.getTasks() }
                .onSuccess { board = it; loading = false }
                .onFailure { err = it.message ?: "失败"; loading = false }
        } else {
            err = "未连接云侧：去「设置」填地址和 Token"
            loading = false
        }
    }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("任务进度", style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Text("项目整体进度（计划书阶段 + 近期提交）", fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        board?.branch?.let {
            Spacer(Modifier.height(4.dp))
            Text("分支：$it", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        if (loading) Text("加载中…", fontSize = 12.sp)
        if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error, fontSize = 12.sp)

        board?.phases?.let { phases ->
            LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(phases) { p ->
                    Row(Modifier.fillMaxWidth()
                        .padding(horizontal = 4.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        Text(if (p.done) "✅" else "⬜", fontSize = 15.sp)
                        Spacer(Modifier.width(10.dp))
                        Text("${p.id}  ${p.name}", fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurface)
                    }
                }
            }
        }
        Spacer(Modifier.height(14.dp))
        Text("近期提交", fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(4.dp))
        board?.recentCommits?.let { commits ->
            for (c in commits) {
                Text(c, fontSize = 11.sp, fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                HorizontalDivider(Modifier.padding(vertical = 3.dp))
            }
        }
    }
}
