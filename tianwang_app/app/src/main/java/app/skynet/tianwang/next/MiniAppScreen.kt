package app.skynet.tianwang.next

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.webkit.WebView
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** 「在手机里编写应用」的最小真闭环：写 HTML/JS → 本机 WebView 跑 → 存成应用 → 导出给对端。 */
object MiniAppStore {
    private const val KEY = "mini.apps"

    fun list(): List<Map<String, String>> = kbList(KEY)

    fun htmlOf(name: String): String = KB.list(KEY)
        .firstOrNull { it.optString("name") == name }
        ?.optString("html").orEmpty()

    fun save(name: String, html: String) {
        val clean = name.trim().replace(Regex("[^A-Za-z0-9_.-]"), "_").ifBlank { "app.html" }
        val arr = KB.array(KEY)
        var found = false
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("name") == clean) {
                o.put("html", html)
                o.put("at", System.currentTimeMillis())
                arr.put(i, o)
                found = true
                break
            }
        }
        if (!found) {
            arr.put(JSONObject().put("id", clean).put("name", clean)
                .put("html", html).put("at", System.currentTimeMillis()))
        }
        KB.put(KEY, arr)
    }

    fun delete(name: String) {
        val arr = KB.array(KEY)
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("name") != name) out.put(o)
        }
        KB.put(KEY, out)
    }

    /** 导进「交换目录」，对端用 self_file_read 就能把这个小应用取走。 */
    fun export(name: String, html: String): String {
        val base = App.ctx.getExternalFilesDir(null) ?: App.ctx.filesDir
        val dir = File(base, "lanfiles").apply { mkdirs() }
        val clean = name.trim().replace(Regex("[^A-Za-z0-9_.-]"), "_").ifBlank { "app.html" }
        File(dir, clean).writeText(html, Charsets.UTF_8)
        return clean
    }
}

/** 内置几套真能点的小应用，不是纯展示卡片。 */
object MiniAppTemplates {
    val all = listOf(
        "计数器" to ("counter.html" to CodeTemplates.all.first { it.lang == Lang.HTML }.code),
        "待办清单" to ("todo.html" to
            "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>待办</title><style>body{font-family:sans-serif;padding:20px;background:#fafafa;color:#222}" +
            "li{margin:8px 0}input{padding:8px}</style></head><body>" +
            "<h2>待办清单</h2><input id='i' placeholder='写点什么'><button onclick='add()'>添加</button>" +
            "<ul id='l'></ul><script>function add(){var v=document.getElementById('i').value;" +
            "if(!v)return;var li=document.createElement('li');li.textContent=v;" +
            "document.getElementById('l').appendChild(li);document.getElementById('i').value=''}" +
            "</script></body></html>"),
        "单位换算" to ("convert.html" to
            "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>换算</title><style>body{font-family:sans-serif;padding:20px;background:#fafafa;color:#222}" +
            "input,button{font-size:16px;padding:8px;margin:4px 0}</style></head><body>" +
            "<h2>公里 → 英里</h2><input id='km' type='number' value='1'>" +
            "<button onclick='go()'>换算</button><p id='o'></p>" +
            "<script>function go(){var k=+document.getElementById('km').value;" +
            "document.getElementById('o').textContent=k+' km = '+(k*0.621371).toFixed(3)+' mi'}</script>" +
            "</body></html>"),
    )
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun MiniAppScreen() {
    var tick by remember { mutableStateOf(0) }
    var name by remember { mutableStateOf("counter.html") }
    var html by remember { mutableStateOf(MiniAppTemplates.all[0].second.second) }
    var previewHtml by remember { mutableStateOf(MiniAppTemplates.all[0].second.second) }
    var notice by remember { mutableStateOf("") }

    val apps = remember(tick) { MiniAppStore.list() }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
    ) {
        FeatureHeader("小应用工坊", "手机上写 HTML/JS，本机直接跑起来、存成应用、导出给对端。")
        Spacer(Modifier.height(8.dp))

        FeatureCard(highlighted = true) {
            Text("这是「自己写一个应用」的最短路径", fontSize = 13.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(4.dp))
            Text("编辑器写代码 → 预览区就是手机本机 WebView 在跑（JS 真执行）→ 保存成小应用 → " +
                "导出到交换目录，电脑/另一台手机用 self_file_read 取走。",
                fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("模板")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            MiniAppTemplates.all.forEach { (label, pair) ->
                TextButton(onClick = {
                    name = pair.first
                    html = pair.second
                    previewHtml = pair.second
                    notice = "已载入「$label」"
                }) { Text(label, fontSize = 11.sp) }
                Spacer(Modifier.width(4.dp))
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureField(value = name, onChange = { name = it }, label = "应用文件名")
        Spacer(Modifier.height(8.dp))
        FeatureSection("代码")
        OutlinedTextField(
            value = html,
            onValueChange = { html = it },
            modifier = Modifier.fillMaxWidth().height(230.dp),
            textStyle = TextStyle(fontSize = 11.sp, lineHeight = 15.sp),
            label = { Text("HTML / JS", fontSize = 10.sp) },
        )
        Spacer(Modifier.height(6.dp))
        if (notice.isNotBlank()) {
            Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
        }
        FeatureActions(
            "预览运行" to { previewHtml = html; notice = "已在预览区重新加载" },
            "保存为小应用" to {
                MiniAppStore.save(name, html)
                tick++
                notice = "已保存 $name"
            },
            "存进工作区" to {
                CodeEngine.saveFile(name, Lang.HTML, html)
                notice = "已写入代码工作区，可在工作台/工作区里看到"
            },
            "导出到交换目录" to {
                notice = "已导出 ${MiniAppStore.export(name, html)}，对端可用 self_file_read 取走"
            },
            "复制代码" to {
                val cm = App.ctx.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newPlainText("天网小应用", html))
                notice = "代码已复制"
            },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("预览（本机 WebView 真在跑）")
        FeatureCard {
            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        setBackgroundColor(0xFFFAFAFA.toInt())
                    }
                },
                update = { wv ->
                    // 只在预览内容变化时重载，避免输入过程中每帧刷新把页面状态冲掉。
                    if (wv.tag != previewHtml) {
                        wv.loadDataWithBaseURL(null, previewHtml, "text/html", "utf-8", null)
                        wv.tag = previewHtml
                    }
                },
                modifier = Modifier.fillMaxWidth().height(280.dp),
            )
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("已保存的小应用（${apps.size}）")
        if (apps.isEmpty()) {
            Text("还没保存过。点「保存为小应用」就会出现在这里。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            apps.forEach { a ->
                val n = a["name"].orEmpty()
                FeatureCard {
                    Text(n, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface)
                    Text("${MiniAppStore.htmlOf(n).length} 字", fontSize = 10.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    FeatureActions(
                        "载入" to {
                            name = n
                            html = MiniAppStore.htmlOf(n)
                            previewHtml = html
                            notice = "已载入 $n"
                        },
                        "删除" to {
                            MiniAppStore.delete(n)
                            tick++
                            notice = "已删除 $n"
                        },
                    )
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}
