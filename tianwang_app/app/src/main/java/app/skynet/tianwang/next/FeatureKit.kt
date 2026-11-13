package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * 轻量功能基座：给一批「对照 DSH Go 补齐的产品模块」共用。
 *
 * 为什么单独抽出来：这些模块都是「手机上人能增删改、关掉 App 还在」的本地数据，
 * 各自写一遍 SharedPreferences / JSON 文件读写只会制造重复。统一成 [KB] 一个
 * JSON 文件键值库 + 一套 Material3 卡片/表单组件，每个模块只写自己的业务字段。
 *
 * 纪律：
 *  - 只存本机 filesDir，不联网、不上传、不回显密钥；
 *  - 写盘走「临时文件 → 改名」，避免写到一半进程被杀留下半截 JSON；
 *  - 这一层不做权限、不碰无障碍、不改系统状态，纯数据 + 纯 UI。
 */

/** 一个 JSON 文件里的键值库：每个模块用一个 key 存一个 JSONArray。 */
object KB {
    private const val FILE = "feature_kv.json"

    private fun file(): File = File(App.ctx.filesDir, FILE)
    private fun tmp(): File = File(App.ctx.filesDir, "$FILE.tmp")

    private fun root(): JSONObject {
        val f = file()
        if (!f.isFile) return JSONObject()
        return runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrDefault(JSONObject())
    }

    private fun write(o: JSONObject) {
        runCatching {
            val t = tmp()
            t.writeText(o.toString(), Charsets.UTF_8)
            if (!t.renameTo(file())) file().writeText(o.toString(), Charsets.UTF_8)
        }
    }

    /** 读一个列表；不存在 / 坏了都回空，不抛。 */
    fun array(key: String): JSONArray = root().optJSONArray(key) ?: JSONArray()

    /** 整个列表覆盖写回。 */
    fun put(key: String, arr: JSONArray) {
        val o = root()
        o.put(key, arr)
        write(o)
    }

    /** 读一个字符串值。 */
    fun str(key: String, def: String = ""): String = root().optString(key, def).ifEmpty { def }

    /** 写一个字符串值。 */
    fun setStr(key: String, v: String) {
        val o = root()
        o.put(key, v)
        write(o)
    }

    /** 追加一条（自动补 id）。 */
    fun add(key: String, item: JSONObject) {
        if (item.optString("id").isBlank()) item.put("id", UUID.randomUUID().toString())
        if (!item.has("at")) item.put("at", System.currentTimeMillis())
        val arr = array(key)
        arr.put(item)
        put(key, arr)
    }

    /** 按 id 删一条。 */
    fun remove(key: String, id: String) {
        val arr = array(key)
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("id") == id) continue
            out.put(o)
        }
        put(key, out)
    }

    /** 按 id 覆盖一条字段。 */
    fun update(key: String, id: String, patch: JSONObject) {
        val arr = array(key)
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("id") != id) continue
            val keys = patch.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                o.put(k, patch.get(k))
            }
            arr.put(i, o)
            break
        }
        put(key, arr)
    }

    fun list(key: String): List<JSONObject> {
        val arr = array(key)
        val out = ArrayList<JSONObject>(arr.length())
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            out.add(o)
        }
        return out
    }
}

/** 把一个 JSONObject 列表转成 Compose 能安全遍历的快照，避免重组时读半截。 */
fun kbList(key: String): List<Map<String, String>> = KB.list(key).map { o ->
    val m = LinkedHashMap<String, String>()
    val keys = o.keys()
    while (keys.hasNext()) {
        val k = keys.next()
        m[k] = o.opt(k)?.toString() ?: ""
    }
    m
}

/** 通用页头：标题 + 一句定位说明。对照 DSH Go 每个 page 都有 hero/说明的产品感。 */
@Composable
fun FeatureHeader(title: String, sub: String) {
    Column(Modifier.fillMaxWidth().padding(bottom = 4.dp)) {
        Text(title, fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(4.dp))
        Text(sub, fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** 通用分组标题。 */
@Composable
fun FeatureSection(label: String) {
    Spacer(Modifier.height(8.dp))
    Text(label, fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
        color = MaterialTheme.colorScheme.onSurface)
    Spacer(Modifier.height(6.dp))
}

/** 通用卡片：一块可点/不可点的浅色容器，浅底深字，适配当前浅色主题。 */
@Composable
fun FeatureCard(
    modifier: Modifier = Modifier,
    highlighted: Boolean = false,
    onClick: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    val shape = RoundedCornerShape(12.dp)
    var m: Modifier = modifier
        .fillMaxWidth()
        .clip(shape)
        .background(MaterialTheme.colorScheme.surface)
        .border(
            width = if (highlighted) 1.5.dp else 1.dp,
            color = if (highlighted) MaterialTheme.colorScheme.primary
            else MaterialTheme.colorScheme.outlineVariant,
            shape = shape,
        )
    if (onClick != null) m = m.clickable { onClick() }
    Column(m.padding(12.dp)) { content() }
}

/** 通用单行输入。 */
@Composable
fun FeatureField(
    value: String,
    onChange: (String) -> Unit,
    label: String,
    modifier: Modifier = Modifier,
    singleLine: Boolean = true,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label, fontSize = 11.sp) },
        singleLine = singleLine,
        modifier = modifier.fillMaxWidth(),
        textStyle = androidx.compose.ui.text.TextStyle(fontSize = 12.sp),
    )
}

/** 通用开关行。 */
@Composable
fun FeatureToggle(label: String, sub: String = "", checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(label, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface)
            if (sub.isNotBlank()) {
                Text(sub, fontSize = 10.5.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Spacer(Modifier.width(8.dp))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

/** 通用动作按钮行（文字按钮，不依赖图标库，避免图标缺失编译失败）。 */
@Composable
fun FeatureActions(vararg actions: Pair<String, () -> Unit>) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        actions.forEach { (label, fn) ->
            TextButton(onClick = fn) { Text(label, fontSize = 11.5.sp) }
        }
    }
}

/** 一行键值对展示。 */
@Composable
fun FeatureKV(k: String, v: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
        Text(k, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(72.dp))
        Text(v, fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurface)
    }
}
