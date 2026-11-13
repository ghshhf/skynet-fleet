package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.util.Base64
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
 * 独立动作页：对照 DSH Go 里不是「列表」而是「一次动作 / 一份只读信息」的模块 ——
 * share_code(导入导出)、update_sheet、network_details_sheet、access_walk、
 * verify_page、third_party_licenses、contributors、terminal_keys。
 *
 * 每个都做成本机可操作的东西：分享码真编解码、网络详情真读 [LanProto] 地址、
 * 引导真读 [Perms] 快照、许可与贡献者是只读清单。不联网、不上传、不谎称已生效。
 */
private fun exCopy(s: String) {
    val cm = App.ctx.getSystemService(ClipboardManager::class.java) ?: return
    cm.setPrimaryClip(ClipData.newPlainText("tianwang", s))
}

/** 分享码：把一段配置编成可复制的 base64，也能粘回来解开。对照 share_code/import_sheet。 */
@Composable
fun ShareCodeScreen() {
    var raw by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var decoded by remember { mutableStateOf("") }
    var err by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("分享码", "把一段配置编成一串码发给别人，对方粘回来就能还原。编码只在本机，密钥别放进去。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureSection("编码（本机 → 码）")
            FeatureCard {
                FeatureField(raw, { raw = it }, "要编码的内容（文本或 JSON）", singleLine = false)
                Spacer(Modifier.height(4.dp))
                FeatureActions(
                    "生成码" to {
                        code = runCatching {
                            val o = JSONObject().put("v", 1).put("text", raw)
                            Base64.encodeToString(o.toString().toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
                        }.getOrDefault("")
                    },
                    "复制码" to { if (code.isNotBlank()) exCopy(code) },
                )
                if (code.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(code.take(120), fontSize = 10.sp, lineHeight = 14.sp,
                        color = MaterialTheme.colorScheme.primary)
                }
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("解码（码 → 本机）")
            FeatureCard {
                FeatureField(code, { code = it }, "粘进来的分享码", singleLine = false)
                Spacer(Modifier.height(4.dp))
                FeatureActions(
                    "解开" to {
                        err = ""; decoded = ""
                        val r = runCatching {
                            val bytes = Base64.decode(code.trim(), Base64.NO_WRAP)
                            val o = JSONObject(String(bytes, Charsets.UTF_8))
                            o.optString("text").ifBlank { o.toString() }
                        }
                        r.onSuccess { decoded = it }.onFailure { err = "解不开：${it.message}" }
                    },
                    "复制结果" to { if (decoded.isNotBlank()) exCopy(decoded) },
                )
                if (decoded.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(decoded.take(400), fontSize = 11.sp, lineHeight = 16.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                }
                if (err.isNotBlank()) Text(err, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 网络详情：真读本机地址清单 / 直连状态 / DDNS。对照 remote/network_details_sheet。 */
@Composable
fun NetworkDetailScreen() {
    val addrs = remember { LanProto.localAddresses() }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("网络详情", "这台手机现在能被谁连到：地址清单、直连状态、稳定名字。只读，不改网络。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureKV("直连服务", if (LanHub.running) "运行中 · ${LanHub.port}" else "未启动")
                FeatureKV("本机名", Prefs.deviceName.ifBlank { "未设置" })
                FeatureKV("DDNS", if (Prefs.ddnsProvider != Ddns.PROVIDER_OFF) Ddns.nameOf(Ddns.cfg()) else "未启用")
                FeatureKV("最近错误", LanHub.lastError.ifBlank { "无" })
            }
            Spacer(Modifier.height(10.dp))
        }
        item { FeatureSection("可入站地址（${addrs.size}）") }
        addrs.forEach { a ->
            item {
                FeatureCard {
                    Text("${LanProto.bracket(a.host)}${if (LanHub.running) ":${LanHub.port}" else ""}",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
                    Text(a.family.label, fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        if (addrs.isEmpty()) {
            item { FeatureCard { Text("没有可入站地址：先连 Wi-Fi，或确认系统没关 IPv6。",
                fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant) } }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 无障碍引导：真读权限矩阵快照，逐条给下一步。对照 onboarding/access_walk。 */
@Composable
fun AccessWalkScreen() {
    val snap = remember { runCatching { Perms.format(Perms.snapshot(App.ctx)) }
        .getOrDefault("权限快照读取失败") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("权限引导", "逐条看每条能力走哪条通道、现在通不通、缺什么。只读，不代点授权。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                Text(snap, fontSize = 11.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurface)
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            Text("对照 DSH Go 的 access_walk：引导不是一次弹窗，是让人知道「哪条没通、去哪开」。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 验证：本机口令自检（只比长度与是否已设置，不回显）。对照 verify_page。 */
@Composable
fun VerifyScreen() {
    var input by remember { mutableStateOf("") }
    var result by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("验证", "核对一段口令是不是本机当前直连口令。不回显、不记录输入。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(input, { input = it }, "粘贴待核对的口令")
                Spacer(Modifier.height(4.dp))
                FeatureActions("核对" to {
                    val cur = Prefs.lanToken
                    result = when {
                        cur.isBlank() -> "本机还没生成直连口令：先开一次直连服务。"
                        input.trim() == cur -> "一致：这是本机当前直连口令。"
                        else -> "不一致：不是本机当前口令。"
                    }
                })
                if (result.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(result, fontSize = 11.5.sp,
                        color = if (result.startsWith("一致")) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                }
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 终端快捷命令：点一下把常用命令塞进终端输入（写 KB，终端页可读取）。对照 terminal_keys。 */
@Composable
fun TerminalKeysScreen() {
    var tick by remember { mutableStateOf(0) }
    val custom = remember(tick) { kbList("term_keys") }
    var label by remember { mutableStateOf("") }
    var command by remember { mutableStateOf("") }
    val presets = listOf(
        "设备型号" to "getprop ro.product.model",
        "系统版本" to "getprop ro.build.version.release",
        "已装包" to "pm list packages -3",
        "磁盘" to "df -h",
        "进程" to "ps -A | head -40",
        "网络" to "ip addr",
    )
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("终端快捷命令", "常用命令一键填进终端，也能自己加。命令存本机，不自动执行。"); Spacer(Modifier.height(8.dp)) }
        item { FeatureSection("内置") }
        presets.forEach { (label0, cmd) ->
            item {
                FeatureCard(onClick = { KB.setStr("term_prefill", cmd) }) {
                    Text(label0, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface)
                    Text(cmd, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            FeatureSection("自定义")
            FeatureCard {
                FeatureField(label, { label = it }, "名称")
                Spacer(Modifier.height(6.dp))
                FeatureField(command, { command = it }, "命令")
                Spacer(Modifier.height(4.dp))
                FeatureActions("添加" to {
                    if (label.isNotBlank() && command.isNotBlank()) {
                        KB.add("term_keys", JSONObject().put("label", label.trim()).put("cmd", command.trim()))
                        label = ""; command = ""; tick++
                    }
                })
            }
            Spacer(Modifier.height(8.dp))
        }
        custom.forEach { r ->
            item {
                FeatureCard(onClick = { KB.setStr("term_prefill", r["cmd"].orEmpty()) }) {
                    Text(r["label"].orEmpty(), fontSize = 12.sp)
                    Text(r["cmd"].orEmpty(), fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    FeatureActions("删除" to { KB.remove("term_keys", r["id"].orEmpty()); tick++ })
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

/** 第三方许可与贡献者：只读清单。对照 third_party_licenses / contributors。 */
@Composable
fun CreditsScreen() {
    val libs = listOf(
        "Jetpack Compose / Material3" to "Apache-2.0",
        "OkHttp" to "Apache-2.0",
        "org.json" to "公开域 / Android 内置",
        "Kotlin 标准库" to "Apache-2.0",
        "本地 GGUF 推理" to "随模型许可",
    )
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("许可与致谢", "本机构建用到的开源组件。只读清单，不联网拉取。"); Spacer(Modifier.height(8.dp)) }
        item { FeatureSection("开源组件") }
        libs.forEach { (name, lic) ->
            item {
                FeatureCard { FeatureKV(name, lic) }
                Spacer(Modifier.height(6.dp))
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            FeatureCard {
                Text("产品形态参考 DSH Go（AGPL-3.0）的模块划分，界面与代码均为本机独立实现，未搬用其源码与资源。",
                    fontSize = 10.5.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

/** 更新检查：只读版本 + 本地更新日志，不联网。对照 update_sheet / whats_new_page。 */
@Composable
fun UpdateScreen() {
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("更新", "当前版本与本次变更。检查只核对本机，不访问应用商店、不自动下载。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureKV("当前版本", InfoBits.appVersion())
                FeatureKV("设备", InfoBits.deviceLine())
                FeatureKV("本机时间", InfoBits.now())
            }
            Spacer(Modifier.height(10.dp))
        }
        item { FeatureSection("本次变更（${InfoBits.changelog.size}）") }
        InfoBits.changelog.take(12).forEach { (tag, line) ->
            item {
                FeatureCard { Text("[$tag] $line", fontSize = 11.5.sp,
                    color = MaterialTheme.colorScheme.onSurface) }
                Spacer(Modifier.height(6.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
