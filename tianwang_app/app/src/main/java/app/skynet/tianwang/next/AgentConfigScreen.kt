package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/** 云侧连接配置：服务地址 + Token + 保存 + 测试。 */
@Composable
fun AgentConfigScreen() {
    var url by remember { mutableStateOf(Net.loadConfig().url) }
    var token by remember { mutableStateOf(Net.loadConfig().token) }
    var msg by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    Column(Modifier.padding(horizontal = 4.dp, vertical = 6.dp)) {
        Text("云侧连接", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, top = 4.dp, bottom = 6.dp))
        OutlinedTextField(url, { url = it }, label = { Text("服务地址") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp),
            singleLine = true)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(token, { token = it }, label = { Text("Token") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp),
            singleLine = false, minLines = 2)
        Spacer(Modifier.height(10.dp))
        Row(Modifier.padding(horizontal = 4.dp)) {
            Button(onClick = {
                Net.saveConfig(url, token)
                msg = "已保存"
            }) { Text("保存") }
            Spacer(Modifier.width(8.dp))
            Button(onClick = {
                scope.launch {
                    msg = "测试中…"
                    runCatching {
                        val ps = Net.getProviders()
                        "连接成功 · 网关 ${ps.size} 个"
                    }.onSuccess { msg = it }.onFailure { msg = "连接失败：${it.message}" }
                }
            }) { Text("测试连接") }
        }
        if (msg.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            Text(msg, fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 8.dp))
        }
        Spacer(Modifier.height(6.dp))
        Text("电脑端 `python -m fleet.agent_server` 启动后，用 `adb reverse tcp:9124 tcp:9124` 把它映射到手机。",
            fontSize = 10.5.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 8.dp))
    }
}
