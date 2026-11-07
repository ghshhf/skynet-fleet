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

/** 云侧连接配置：服务地址 + Token + 配对 + 保存 + 测试。 */
@Composable
fun AgentConfigScreen() {
    var url by remember { mutableStateOf(Net.loadConfig().url) }
    var token by remember { mutableStateOf(Net.loadConfig().token) }
    var msg by remember { mutableStateOf("") }
    var paired by remember { mutableStateOf(Prefs.paired()) }
    var pairedId by remember { mutableStateOf(Prefs.deviceId) }
    var code by remember { mutableStateOf("") }
    var pairing by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    Column(Modifier.padding(horizontal = 4.dp, vertical = 6.dp)) {
        Text("云侧连接", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, top = 4.dp, bottom = 6.dp))
        OutlinedTextField(url, { url = it }, label = { Text("服务地址") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp),
            singleLine = true)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(token, { token = it }, label = { Text("Token（配对后可留空）") },
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
                    // 🔴 先落盘再测：getProviders 读的是已保存配置，
                    // 不先 save 就点测试 = 拿旧地址测，必错
                    Net.saveConfig(url, token)
                    msg = "测试中…"
                    runCatching {
                        val ps = Net.getProviders()
                        if (ps.isEmpty()) "连接成功但没拉到网关 —— 检查云侧是否启动"
                        else "连接成功 · 网关 ${ps.size} 个（活 ${ps.count { it.status == "活" }}）"
                    }.onSuccess { msg = it }.onFailure { msg = "连接失败：${it.message}" }
                }
            }) { Text("测试连接") }
        }

        // ---- 长期凭据：配一次，电脑重启也不用再配 ----
        Spacer(Modifier.height(14.dp))
        Text("配对这台手机", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, bottom = 6.dp))
        if (paired) {
            Text("已配对 · $pairedId", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 8.dp))
            Spacer(Modifier.height(6.dp))
            Row(Modifier.padding(horizontal = 4.dp)) {
                Button(onClick = {
                    Prefs.clearDevice()
                    paired = false
                    pairedId = ""
                    msg = "已解除配对"
                }) { Text("解除配对") }
            }
            Spacer(Modifier.height(8.dp))
        }
        OutlinedTextField(code, { code = it }, label = { Text("6 位配对码") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp),
            singleLine = true)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.padding(horizontal = 4.dp)) {
            Button(enabled = !pairing, onClick = {
                if (code.trim().length != 6) {
                    msg = "配对码是 6 位数字"
                } else {
                    scope.launch {
                        pairing = true
                        msg = "配对中…"
                        // 先落盘地址：配对请求直接打这个地址
                        Net.saveConfig(url, token)
                        runCatching { Net.claim(url, code, Prefs.deviceName) }
                            .onSuccess {
                                Prefs.saveDevice(it.deviceId, it.secret, Prefs.deviceName)
                                // 配完不留旧令牌：留着以后分不清是哪把钥匙在生效
                                Net.saveConfig(url, "")
                                token = ""
                                paired = true
                                pairedId = Prefs.deviceId
                                code = ""
                                msg = "配对成功：${Prefs.deviceName}（以后不用再配）"
                            }
                            .onFailure { msg = "配对失败：${it.message}" }
                        pairing = false
                    }
                }
            }) { Text(if (paired) "重配" else "配对") }
        }

        if (msg.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            Text(msg, fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 8.dp))
        }
        Spacer(Modifier.height(6.dp))
        Text("配对码显示在电脑端界面上，6 位数字。配对一次长期有效；被撤销时才需要重配。\n" +
            "没配对时可用 Token + `adb reverse tcp:9124 tcp:9124`。",
            fontSize = 10.5.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 8.dp))
    }
}
