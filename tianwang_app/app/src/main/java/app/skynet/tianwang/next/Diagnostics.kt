package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 诊断包：出问题时能一次把「这台手机现在到底是什么状态」交给别人看，不用靠猜。
 *
 * 对照 DSH Go 的 diagnostics（bundle / redact / markdown / export）：
 * 它把脱敏当成硬要求，因为诊断文本是要**发出去**的。天网原来只有 `self_diag`
 * 这个工具能在控制台回一段文字，没有可导出的包，也没有脱敏层。
 *
 * 两条硬规则：
 * 1. 只收集**状态**，不收集内容（不读会话正文、不读资源卡、不读网盘目录）。
 * 2. 生成后整段再过一遍 [redact]：中继地址 / 房间 / 密钥 / token / 长十六进制串一律抹掉。
 *    用户拿到的文本可以直接分享，不需要自己先删几行。
 */
object Diagnostics {

    private val KV_SECRET = Regex(
        "(?i)\\b(token|secret|pass|password|passwd|api[_-]?key|key|room|authorization)\\b\\s*[=:]\\s*\\S+",
    )
    private val URL_CRED = Regex("(?i)([a-z][a-z0-9+.-]*://)[^/@\\s]+:[^/@\\s]+@")
    private val WS_URL = Regex("(?i)wss?://\\S+")
    private val LONG_HEX = Regex("\\b[0-9a-fA-F]{24,}\\b")
    private val BEARER = Regex("(?i)Bearer\\s+\\S+")

    fun redact(s: String): String = s
        .replace(URL_CRED) { m -> "${m.groupValues[1]}<凭据已隐藏>@" }
        .replace(WS_URL, "<中继地址已隐藏>")
        .replace(BEARER, "Bearer <已隐藏>")
        .replace(KV_SECRET) { m ->
            val key = m.groupValues[1]
            "$key=<已隐藏>"
        }
        .replace(LONG_HEX, "<已隐藏>")

    private fun stamp(at: Long): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(Date(at))

    /** 生成 Markdown。只读本机状态，不联网、不要求任何服务在线。 */
    fun build(ctx: Context): String {
        val ver = runCatching {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "未知"
        }.getOrDefault("未知")
        val env = runCatching { Perms.snapshot(ctx) }.getOrDefault(PermEnv())
        val sb = StringBuilder()
        sb.append("# 天网 Next 诊断包\n\n")
        sb.append("- 生成时间：").append(stamp(System.currentTimeMillis())).append('\n')
        sb.append("- App 版本：").append(ver).append('\n')
        sb.append("- 设备：").append(Build.MANUFACTURER).append(' ').append(Build.MODEL).append('\n')
        sb.append("- Android：").append(Build.VERSION.RELEASE)
            .append(" (SDK ").append(Build.VERSION.SDK_INT).append(")\n")
        sb.append("- ABI：").append(Build.SUPPORTED_ABIS.joinToString(", ")).append('\n')
        sb.append("- 本机工具数：").append(runCatching { PhoneMcp.toolCount() }.getOrDefault(-1)).append('\n')
        sb.append("- 外部 MCP 工具数：")
            .append(runCatching { McpServers.cachedTools().size }.getOrDefault(-1)).append('\n')
        sb.append("- 本地模型数：").append(runCatching { ModelCatalog.local().size }.getOrDefault(-1)).append('\n')
        sb.append("- 会话数：").append(runCatching { SessionStore.load().size }.getOrDefault(-1)).append('\n')
        sb.append('\n')

        sb.append("## 通道状态\n\n")
        for ((k, v) in SafeMode.statusLines()) sb.append("- ").append(k).append("：").append(v).append('\n')
        sb.append("- 无障碍：")
            .append(runCatching { PhoneSelf.statusLine(ctx) }.getOrDefault("读取失败")).append('\n')
        sb.append("- 入网已配置：").append(if (Prefs.mcpConfigured()) "是" else "否").append('\n')
        sb.append("- 动态域名已发布：").append(if (Prefs.ddnsEnabled && Prefs.ddnsLastIp.isNotBlank()) "是" else "否").append('\n')
        sb.append('\n')

        sb.append("## 权限矩阵\n\n```\n")
        sb.append(Perms.format(env)).append("\n```\n\n")

        sb.append("## 最近动作（内存审计，最多 20 条）\n\n")
        val acts = runCatching { ActionLog.list(20) }.getOrDefault(emptyList())
        if (acts.isEmpty()) {
            sb.append("（本次启动还没有远控动作）\n")
        } else {
            for (a in acts) {
                sb.append("- ").append(stamp(a.at)).append(" · ").append(a.name)
                    .append(" · ").append(if (a.ok) "成功" else "失败")
                    .append(" · ").append(a.ms).append("ms\n")
                sb.append("  - 参数：").append(a.args).append('\n')
                if (a.err.isNotBlank()) sb.append("  - 错误：").append(a.err).append('\n')
            }
        }
        sb.append('\n')
        sb.append("> 本包只含状态，不含会话正文、资源内容或网盘目录；")
        sb.append("密钥 / 房间 / 令牌 / 中继地址已自动脱敏。\n")
        return redact(sb.toString())
    }

    /** 系统分享：文本直接发出去，不落盘、不需要文件权限。 */
    fun share(ctx: Context, text: String) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, "天网 Next 诊断包")
            putExtra(Intent.EXTRA_TEXT, text)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { ctx.startActivity(Intent.createChooser(i, "分享诊断包").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    fun copy(ctx: Context, text: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("天网诊断包", text))
    }
}

@Composable
fun DiagnosticsScreen() {
    val ctx = LocalContext.current
    var text by remember { mutableStateOf(Diagnostics.build(ctx)) }
    var note by remember { mutableStateOf("") }
    val lines = text.lines()

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("诊断与导出", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("只收集状态，密钥 / 房间 / 令牌 / 中继地址导出前自动抹掉。发出去之前可先在下面核一遍。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                text = Diagnostics.build(ctx)
                note = "已重新生成 · " + lines.size + " 行"
            }, modifier = Modifier.weight(1f)) { Text("重新生成") }
            Button(onClick = {
                Diagnostics.copy(ctx, text)
                note = "已复制到剪贴板（" + text.length + " 字）"
            }, modifier = Modifier.weight(1f)) { Text("复制") }
            Button(onClick = {
                Diagnostics.share(ctx, text)
                note = "已交给系统分享"
            }, modifier = Modifier.weight(1f)) { Text("分享") }
        }
        if (note.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(note, fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(10.dp))
        LazyColumn(
            Modifier.fillMaxWidth()
                .weight(1f)
                .clip(RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(10.dp),
        ) {
            items(lines) { line ->
                Text(
                    line.ifBlank { " " },
                    fontSize = 10.5.sp,
                    lineHeight = 15.sp,
                    fontFamily = FontFamily.Monospace,
                    color = if (line.startsWith("#") || line.startsWith("##"))
                        MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            item {
                TextButton(onClick = { note = "共 ${lines.size} 行 / ${text.length} 字" }) {
                    Text("到底了 · 点这里看统计")
                }
            }
        }
    }
}
