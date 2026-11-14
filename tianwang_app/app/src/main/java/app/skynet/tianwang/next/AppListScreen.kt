package app.skynet.tianwang.next

import android.content.ClipboardManager
import android.content.ClipData
import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 应用列表：手机用 pm 列出已装应用（默认只看第三方），点一下复制包名。
 * 纯 Android shell 层，不依赖 proot；Android 11+ 只能列到自己可见的范围，如实显示。
 */
@Composable
fun AppListScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var pkgs by remember { mutableStateOf<List<Pair<String, String>>>(emptyList()) }
    var notice by remember { mutableStateOf("点「刷新应用列表」列出已装应用。") }
    var busy by remember { mutableStateOf(false) }

    fun refresh() {
        if (busy) return
        busy = true
        notice = "读取应用列表…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("pm list packages -3 -f 2>/dev/null || pm list packages -3",
                LinuxRuntime.workspace().absolutePath, 12_000)
            val list = parsePm(out)
            withContext(Dispatchers.Main) {
                busy = false
                pkgs = list
                notice = if (list.isEmpty()) "没有解析到应用（pm 输出可能为空或受限）：$out".take(400)
                else "共 ${list.size} 个第三方应用。"
            }
        }
    }

    fun copyPkg(pkg: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("pkg", pkg))
        notice = "已复制包名：$pkg"
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("应用列表", "看已装的第三方应用、点一下复制包名。纯 shell 层，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("刷新应用列表" to { refresh() })
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(8.dp))
        pkgs.forEach { (pkg, path) ->
            FeatureCard(onClick = { copyPkg(pkg) }) {
                Text(pkg, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                if (path.isNotBlank()) {
                    Spacer(Modifier.height(2.dp))
                    Text(path.take(64), fontSize = 9.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private fun parsePm(out: String): List<Pair<String, String>> {
    val res = mutableListOf<Pair<String, String>>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        if (!t.startsWith("package:")) return@forEach
        // package:/data/app/.../base.apk=com.pkg.name
        val eq = t.indexOf('=')
        if (eq < 0) return@forEach
        val pkg = t.substring(eq + 1).trim()
        val path = t.substring("package:".length, eq).trim()
        if (pkg.isNotBlank()) res.add(pkg to path)
    }
    return res
}
