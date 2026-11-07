package app.skynet.tianwang.next

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/**
 * 两件不相干的事，彻底分开摆：
 *  1) 大模型端点 —— 手动添加任意 OpenAI 兼容端点，手机拿密钥直连厂商，主项；
 *  2) 局域网节点 —— 同 Wi-Fi 下平级共享/互调时才用，可留空，不是前提。
 *
 * 🔴 不预设任何厂商清单：加端点 / 换密钥 / 删端点全在界面上完成，
 * 加一家新厂商不需要改代码、不需要发版。
 */
@Composable
fun AgentConfigScreen() {
    val scope = rememberCoroutineScope()
    var list by remember { mutableStateOf(LlmConfig.all()) }
    var activeId by remember { mutableStateOf(LlmConfig.get().id) }
    // null = 表单收起；id 为空的是「新增」，有 id 的是「编辑」
    var editing by remember { mutableStateOf<LlmSettings?>(null) }
    var fLabel by remember { mutableStateOf("") }
    var fBase by remember { mutableStateOf("") }
    var fKey by remember { mutableStateOf("") }
    var fModel by remember { mutableStateOf("") }
    var showKey by remember { mutableStateOf(false) }

    var nodeUrl by remember { mutableStateOf(Net.rawUrl()) }
    var nodeToken by remember { mutableStateOf(Net.loadConfig().token) }
    var msg by remember { mutableStateOf("") }
    var paired by remember { mutableStateOf(Prefs.paired()) }
    var pairedId by remember { mutableStateOf(Prefs.deviceId) }
    var code by remember { mutableStateOf("") }
    var pairing by remember { mutableStateOf(false) }

    fun refresh() {
        list = LlmConfig.all()
        activeId = LlmConfig.get().id
    }

    fun openEditor(s: LlmSettings) {
        editing = s
        fLabel = s.label
        fBase = s.base
        fKey = s.key
        fModel = s.model
        showKey = false
    }

    fun persist(): LlmSettings {
        val saved = LlmConfig.save(
            LlmSettings(editing?.id.orEmpty(), fLabel, fBase, fKey, fModel))
        editing = saved
        fModel = saved.model
        refresh()
        return saved
    }

    Column(Modifier.padding(horizontal = 4.dp, vertical = 6.dp)) {

        // ---------------------------------------------------------------- 主项
        SectionLabel("大模型端点 · 手机自己用")
        Text(
            "点「添加端点」自己填名称 / 地址 / 密钥 / 模型，任意多条，随时改随时删。" +
                "手机拿密钥直连厂商，不要求电脑装服务、开机或在线；默认只走免费档，模型名留空时测试也只自动填免费模型，付费档自己手填。",
            fontSize = 10.5.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, bottom = 8.dp),
        )

        if (list.isEmpty()) {
            Text("还没有端点。添加一条就能在模型页里直接选它聊天。",
                fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, bottom = 8.dp))
        } else {
            list.forEach { s ->
                Row(
                    Modifier.fillMaxWidth()
                        .clickable { LlmConfig.setActive(s.id); refresh(); msg = "已切换到 ${s.title()}" }
                        .padding(horizontal = 4.dp, vertical = 9.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(s.title(), fontSize = 12.5.sp,
                            fontWeight = if (s.id == activeId) FontWeight.SemiBold else FontWeight.Normal,
                            color = MaterialTheme.colorScheme.onSurface)
                        Text(
                            (s.model.ifBlank { "模型待读取" }) +
                                if (s.id == activeId) " · 当前" else " · 点击设为当前",
                            fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    TextButton(onClick = { openEditor(s) }) {
                        Text("编辑", fontSize = 11.sp)
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(horizontal = 4.dp))
            }
        }

        Spacer(Modifier.height(6.dp))
        Row(Modifier.padding(horizontal = 4.dp)) {
            Button(onClick = {
                editing = LlmSettings()
                fLabel = ""; fBase = ""; fKey = ""; fModel = ""
                showKey = false
            }) { Text("添加端点") }
        }

        editing?.let { cur ->
            Spacer(Modifier.height(10.dp))
            Text(if (cur.id.isBlank()) "新端点" else "编辑：${cur.title()}",
                fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(start = 4.dp, bottom = 6.dp))
            OutlinedTextField(fLabel, { fLabel = it },
                label = { Text("名称（自己起，例：某某云）") },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(fBase, { fBase = it },
                label = { Text("API 地址（OpenAI 兼容，写到 /v1）") },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(fKey, { fKey = it }, label = { Text("API 密钥") },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true,
                visualTransformation = if (showKey) VisualTransformation.None
                else PasswordVisualTransformation(),
                trailingIcon = {
                    TextButton(onClick = { showKey = !showKey }) {
                        Text(if (showKey) "隐藏" else "显示", fontSize = 10.sp)
                    }
                })
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(fModel, { fModel = it },
                label = { Text("模型名（留空则只自动填免费模型）") },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
            Spacer(Modifier.height(8.dp))
            Row(Modifier.padding(horizontal = 4.dp)) {
                Button(onClick = {
                    persist()
                    msg = "端点已存本机 · 模型页会自动出现这一条"
                }) { Text("保存") }
                Spacer(Modifier.width(8.dp))
                Button(onClick = {
                    val s = persist()
                    msg = "测试中…"
                    scope.launch {
                        LlmConfig.test(s.id)
                            .onSuccess {
                                fModel = LlmConfig.byId(s.id)?.model ?: fModel
                                editing = LlmConfig.byId(s.id)
                                refresh()
                                msg = it
                            }
                            .onFailure { msg = "端点测试失败：${it.message}" }
                    }
                }) { Text("测试") }
                Spacer(Modifier.width(8.dp))
                TextButton(onClick = { editing = null; msg = "" }) { Text("收起") }
            }
            if (cur.id.isNotBlank()) {
                TextButton(onClick = {
                    LlmConfig.remove(cur.id)
                    editing = null
                    refresh()
                    msg = "端点已删除"
                }, modifier = Modifier.padding(start = 4.dp)) {
                    Text("删除这个端点", fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.error)
                }
            }
        }

        HorizontalDivider(Modifier.padding(vertical = 16.dp))

        // ---------------------------------------------------------------- 可选项
        SectionLabel("局域网节点 · 可选，可留空")
        Text(
            "只在同一 Wi-Fi / USB 反代下跟别的节点共享与互调时才填。" +
                "留空不影响手机自己爬、自己存、自己查、自己聊。",
            fontSize = 10.5.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 4.dp, bottom = 8.dp),
        )
        OutlinedTextField(nodeUrl, { nodeUrl = it },
            label = { Text("节点地址（可留空）") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(nodeToken, { nodeToken = it },
            label = { Text("节点令牌（配对后可留空）") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.padding(horizontal = 4.dp)) {
            Button(onClick = {
                Net.saveConfig(nodeUrl, nodeToken)
                msg = if (nodeUrl.isBlank()) "节点地址留空 · 本机能力不受影响"
                else "局域网节点已保存"
            }) { Text("保存节点") }
            Spacer(Modifier.width(8.dp))
            Button(onClick = {
                Net.saveConfig(nodeUrl, nodeToken)
                msg = "测试中…"
                scope.launch {
                    runCatching { Net.getProviders() }
                        .onSuccess { ps -> msg = "节点可达 · 网关 ${ps.size} 个" }
                        .onFailure { msg = "节点连不上（可留空）：${it.message}" }
                }
            }) { Text("测试节点") }
        }

        Spacer(Modifier.height(14.dp))
        SectionLabel("和这台手机配对（一次长期有效）")
        if (paired) {
            Text("已配对 · $pairedId", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 8.dp))
            Spacer(Modifier.height(6.dp))
            Row(Modifier.padding(horizontal = 4.dp)) {
                Button(onClick = {
                    Prefs.clearDevice(); paired = false; pairedId = ""
                    msg = "已解除配对"
                }) { Text("解除配对") }
            }
            Spacer(Modifier.height(8.dp))
        }
        OutlinedTextField(code, { code = it }, label = { Text("6 位配对码（可选）") },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp), singleLine = true)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.padding(horizontal = 4.dp)) {
            Button(enabled = !pairing, onClick = {
                if (code.trim().length != 6) {
                    msg = "配对码是 6 位数字"
                } else {
                    scope.launch {
                        pairing = true
                        msg = "配对中…"
                        Net.saveConfig(nodeUrl, nodeToken)
                        runCatching { Net.claim(nodeUrl, code, Prefs.deviceName) }
                            .onSuccess {
                                Prefs.saveDevice(it.deviceId, it.secret, Prefs.deviceName)
                                Net.saveConfig(nodeUrl, "")
                                nodeToken = ""
                                paired = true
                                pairedId = Prefs.deviceId
                                code = ""
                                msg = "配对成功：${Prefs.deviceName}"
                            }
                            .onFailure { msg = "配对失败：${it.message}" }
                        pairing = false
                    }
                }
            }) { Text(if (paired) "重配" else "配对") }
        }

        if (msg.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(msg, fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 8.dp))
        }
        Spacer(Modifier.height(8.dp))
        Text(
            "密钥与节点地址互不通用：密钥只发往你自己填的端点，节点地址只用于局域网互调。",
            fontSize = 10.5.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 8.dp),
        )
    }
}

@Composable
private fun SectionLabel(s: String) {
    Text(s, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 4.dp, bottom = 6.dp))
}
