package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 本地备份与恢复。
 *
 * 对照 DSH Go 的 profile / backup：用户的会话、收件箱、设置是这台手机上的
 * 真正资产，清数据 / 换机前得能整体带走。但这里绝不带走密钥 ——
 * 凡含 secret / token 的配置项一律不进备份包（备份文件很可能被分享出去）。
 *
 * 纪律：
 * - 会话 / 收件箱直接复用本机已落盘的原始文件内容，不重新序列化 ChatMsg，避免漂移。
 * - 写文件一律先写临时文件再改名，防止写到一半被系统杀进程留下半个 JSON。
 * - 恢复是覆盖式：先告诉用户会覆盖，再让他点。
 */
object Backup {
    const val APP = "tianwang-next"
    const val VERSION = 1

    /** 这些键名含 secret / token，属于凭据，备份时跳过、恢复时也不写回。 */
    private val SENSITIVE = Regex("(?i)secret|token")

    private fun dir(name: String): File = File(App.ctx.filesDir, name)

    private fun readRaw(name: String): Any {
        val f = dir(name)
        if (!f.isFile) return JSONArray()
        return runCatching { JSONArray(f.readText(Charsets.UTF_8)) }.getOrDefault(JSONArray())
    }

    /** 当前设置的脱敏快照：凡含 secret / token 的键一律跳过。 */
    fun exportPrefs(): JSONObject {
        val sp = App.ctx.getSharedPreferences("tianwang_prefs", Context.MODE_PRIVATE)
        val out = JSONObject()
        sp.all.forEach { (k, v) ->
            if (SENSITIVE.containsMatchIn(k)) return@forEach
            when (v) {
                is Boolean -> out.put(k, v)
                is Int -> out.put(k, v)
                is Long -> out.put(k, v)
                is Float -> out.put(k, v)
                is String -> out.put(k, v)
            }
        }
        return out
    }

    /** 导出成可读的 JSON 字符串。 */
    fun exportToString(): String {
        val obj = JSONObject()
        obj.put("app", APP)
        obj.put("version", VERSION)
        obj.put("exportedAt", System.currentTimeMillis())
        obj.put("sessions", readRaw("sessions.json"))
        obj.put("inbox", readRaw("share_inbox.json"))
        obj.put("prefs", exportPrefs())
        return obj.toString(2)
    }

    /** 导出并写进应用私有目录下的 backups/，返回文件路径。 */
    fun exportToFile(): String {
        val text = exportToString()
        val d = dir("backups"); d.mkdirs()
        val name = "tw_backup_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.json"
        val f = File(d, name)
        atomicWrite(f, text)
        return f.absolutePath
    }

    /** 从备份字符串恢复，返回（恢复会话数, 恢复收件箱数）。覆盖式。 */
    fun importFromString(text: String): Pair<Int, Int> {
        val obj = JSONObject(text)
        if (obj.optString("app") != APP) throw IllegalArgumentException("不是天网 Next 的备份文件")
        val ver = obj.optInt("version", 1)
        if (ver > VERSION) throw IllegalArgumentException("备份版本 $ver 高于当前应用 $VERSION，无法恢复")
        var sessN = 0
        var inboxN = 0
        obj.optJSONArray("sessions")?.let { arr ->
            atomicWrite(dir("sessions.json"), arr.toString())
            sessN = arr.length()
        }
        obj.optJSONArray("inbox")?.let { arr ->
            atomicWrite(dir("share_inbox.json"), arr.toString())
            inboxN = arr.length()
        }
        obj.optJSONObject("prefs")?.let { prefs ->
            val sp = App.ctx.getSharedPreferences("tianwang_prefs", Context.MODE_PRIVATE)
            val ed = sp.edit()
            val keys = prefs.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                if (SENSITIVE.containsMatchIn(k)) continue
                when (val v = prefs.opt(k)) {
                    is Boolean -> ed.putBoolean(k, v)
                    is Int -> ed.putInt(k, v)
                    is Long -> ed.putLong(k, v)
                    is Float -> ed.putFloat(k, v)
                    is String -> ed.putString(k, v)
                }
            }
            ed.apply()
        }
        return sessN to inboxN
    }

    private fun atomicWrite(target: File, text: String) {
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, "${target.name}.tmp")
        tmp.writeText(text, Charsets.UTF_8)
        if (!tmp.renameTo(target)) {
            target.writeText(text, Charsets.UTF_8)
            tmp.delete()
        }
    }

    fun copy(ctx: Context, text: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("天网备份", text))
    }
}

@Composable
fun BackupScreen() {
    val ctx = LocalContext.current
    var exported by remember { mutableStateOf(false) }
    var filePath by remember { mutableStateOf("") }
    var importText by remember { mutableStateOf("") }
    var note by remember { mutableStateOf("") }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("本地备份与恢复", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("把会话、收件箱、非敏感设置打包成一个 JSON，清数据或换机前先存一份。密钥 / token 不会进备份。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Text("导出", fontSize = 14.sp, fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                runCatching {
                    val s = Backup.exportToString()
                    Backup.copy(ctx, s)
                    exported = true
                    note = "已生成并复制到剪贴板，粘到别处保存即可"
                }.onFailure { note = "导出失败：${it.message}" }
            }, modifier = Modifier.weight(1f)) { Text("生成并复制") }
            Button(onClick = {
                runCatching {
                    filePath = Backup.exportToFile()
                    note = "已存到：$filePath"
                }.onFailure { note = "保存失败：${it.message}" }
            }, modifier = Modifier.weight(1f)) { Text("保存到文件") }
        }
        if (filePath.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(filePath, fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(6.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(8.dp))
        }
        Spacer(Modifier.height(14.dp))
        Text("导入（粘贴上面的 JSON）", fontSize = 14.sp, fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(6.dp))
        BasicTextField(
            value = importText,
            onValueChange = { importText = it },
            modifier = Modifier.fillMaxWidth().height(140.dp)
                .clip(RoundedCornerShape(9.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(10.dp),
            textStyle = TextStyle(fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            decorationBox = { inner ->
                if (importText.isEmpty()) Text("把备份 JSON 粘到这里", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                inner()
            },
        )
        Spacer(Modifier.height(8.dp))
        Button(onClick = {
            if (importText.isBlank()) { note = "先粘贴备份内容"; return@Button }
            runCatching {
                val (s, i) = Backup.importFromString(importText)
                note = "已恢复 会话 $s 条 / 收件箱 $i 条。重启 App 让开关类设置生效。"
            }.onFailure { note = "恢复失败：${it.message}" }
        }, modifier = Modifier.fillMaxWidth()) { Text("恢复") }
        if (note.isNotBlank()) {
            Spacer(Modifier.height(10.dp))
            Text(note, fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
        }
    }
}
