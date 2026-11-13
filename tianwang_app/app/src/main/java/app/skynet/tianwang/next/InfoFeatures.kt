package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
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
 * 信息族：对照 DSH Go 的 account / about / changelog / whats_new / update_sheet /
 * contributors / third_party_licenses / share_card / usage_sheet。
 *
 * 天网原来只有 MineAboutScreen（版本与后端）。这里补齐「本机身份卡、变更日志、
 * 用量统计、分享卡文本、第三方清单入口」，都是只读 + 复制到剪贴板，不联网、不上传。
 */
object InfoBits {
    fun deviceLine(): String = runCatching {
        "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL} · Android ${android.os.Build.VERSION.RELEASE}"
    }.getOrDefault("未知设备")

    fun appVersion(): String = runCatching {
        val p = App.ctx.packageManager.getPackageInfo(App.ctx.packageName, 0)
        "${p.versionName} (${p.longVersionCode})"
    }.getOrDefault("开发版")

    fun now(): String = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date())

    val changelog = listOf(
        "新增" to "桌宠（形象/四态台词/悬浮开关）",
        "新增" to "终端：手机 shell 命令执行 + 历史",
        "新增" to "Bot / 房间 / 团队 / 插件 / 技能预设等集合模块",
        "新增" to "论坛、酒馆（角色·世界书·正则·快捷回复）",
        "新增" to "应用规则、分享链接、工作区、日程与作业登记",
        "新增" to "设置族：界面/隐私/通知/语音/媒体/性能/回复/安全/网页/语言",
        "新增" to "远程控制台：局域网直连控制手机与电脑",
        "新增" to "LAN 文件互传：pcctl.file.* 与 self.file.*",
        "修复" to "远程控制台编译错误（border / rememberCoroutineScope / Divider）",
        "改进" to "CI 改用项目自带 Gradle wrapper，与本地 AGP 对齐",
    )
}

private fun copyText(ctx: Context, s: String) {
    val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
    cm.setPrimaryClip(ClipData.newPlainText("tianwang", s))
}

@Composable
fun AccountScreen() {
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("账户与本机", "这台手机作为对等节点的身份。身份只在本机，不登录、不上传。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureKV("设备", InfoBits.deviceLine())
                FeatureKV("设备 ID", Prefs.deviceId.take(16).ifBlank { "未生成" })
                FeatureKV("App 版本", InfoBits.appVersion())
                FeatureKV("本机名", Prefs.deviceName.ifBlank { "未设置" })
                FeatureKV("局域网端口", Prefs.lanPort.toString())
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            var name by remember { mutableStateOf(Prefs.deviceName) }
            FeatureCard {
                Text("对外显示的本机名", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(4.dp))
                FeatureField(name, { name = it }, "给这台设备起个短名")
                Spacer(Modifier.height(4.dp))
                FeatureActions("保存" to { Prefs.deviceName = name.trim() })
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            Text("对等节点没有中心账号：这台设备凭自己的 ID 与口令跟别的设备直连，凭据不进云端。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
fun ChangelogScreen() {
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("更新日志", "这一版加了什么、修了什么。纯本地清单，不拉取远端。"); Spacer(Modifier.height(8.dp)) }
        InfoBits.changelog.forEach { (tag, line) ->
            item {
                FeatureCard {
                    Text("[$tag] $line", fontSize = 12.sp,
                        color = if (tag == "修复") MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.height(7.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
fun UsageScreen() {
    val caps = runCatching { PhoneMcp.toolCount() }.getOrDefault(0)
    val peers = runCatching { LanHub.peers().size }.getOrDefault(0)
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("用量与状态", "只读快照：本机挂了多少能力、名册里几台对端、事件记录几条。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureKV("本机工具数", caps.toString())
                FeatureKV("集群名册", "$peers 台")
                FeatureKV("动作审计", "${ActionLog.list(50).size} 条（内存）")
                FeatureKV("直连服务", if (LanHub.running) "运行中 · ${LanHub.port}" else "未启动")
                FeatureKV("生成时间", InfoBits.now())
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureActions("复制状态" to {
                copyText(App.ctx, "工具 $caps · 名册 $peers · 直连 ${if (LanHub.running) LanHub.port else "关"} · ${InfoBits.now()}")
            })
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
fun ShareCardScreen() {
    var text by remember { mutableStateOf("") }
    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item { FeatureHeader("分享卡", "把一段结论做成纯文本卡片，复制到剪贴板直接发给别人。不生成图片、不联网上传。"); Spacer(Modifier.height(8.dp)) }
        item {
            FeatureCard {
                FeatureField(text, { text = it }, "要放进卡片的内容", singleLine = false)
                Spacer(Modifier.height(6.dp))
                FeatureActions(
                    "生成并复制" to {
                        val card = "—— 天网 · 对等节点 ——\n$text\n\n${InfoBits.deviceLine()} · ${InfoBits.now()}"
                        copyText(App.ctx, card)
                    },
                    "填示例" to { text = "手机通过与电脑局域网直连，可读系统信息、列窗口、传文件。" },
                )
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureCard {
                Text("预览", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(4.dp))
                Text(if (text.isBlank()) "（先在上面写点内容）" else text,
                    fontSize = 13.sp, lineHeight = 19.sp,
                    color = MaterialTheme.colorScheme.onSurface)
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}
