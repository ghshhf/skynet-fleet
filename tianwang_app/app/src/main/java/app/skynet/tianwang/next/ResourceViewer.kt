package app.skynet.tianwang.next

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener

/**
 * 点开卡片就在 App 里把网页加载出来，不再先跳浏览器。
 *
 * WebView 是 Android 系统自带的渲染能力，不是「别人得先装好、先开着」的软件；
 * 英文页面自动就地翻译成中文（浏览器插件那套路子，不调大模型），可一键切回原文。
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun ResourceViewer(card: ResourceCard, onClose: () -> Unit, onDetail: () -> Unit = {}) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var progress by remember { mutableStateOf(0) }
    var title by remember { mutableStateOf(card.title) }
    var failed by remember { mutableStateOf(false) }
    var web by remember { mutableStateOf<WebView?>(null) }
    var showChinese by remember { mutableStateOf(false) }
    var translated by remember { mutableStateOf<Map<Int, String>?>(null) }
    var trState by remember { mutableStateOf("") }
    // 只管「自动尝试一次」，绝不锁死手动按钮：以前自动识别判成中文后，
    // 用户再点「中文」会直接 return，看起来就是点了没反应。
    var autoTried by remember { mutableStateOf(false) }
    var translating by remember { mutableStateOf(false) }
    var trJob by remember { mutableStateOf<kotlinx.coroutines.Job?>(null) }

    fun applyChinese(v: WebView?, m: Map<Int, String>) {
        val o = JSONObject()
        for ((k, t) in m) o.put(k.toString(), t)
        runCatching {
            v?.evaluateJavascript("window.__twApply(${JSONObject.quote(o.toString())})") {}
        }
    }

    /** 插件式：注入 JS → 收集文本节点 → 手机自己翻 → 回填原节点。
     *  force = 用户主动点「中文」：不做语言判定、不受自动尝试结果影响，永远真的去做一次。 */
    fun translatePage(v: WebView?, force: Boolean) {
        if (v == null || translating) return
        if (!force && autoTried) return
        if (!force) autoTried = true
        translating = true
        trState = "正在收集页面文字…"
        runCatching {
            v.evaluateJavascript(ArticleTranslator.collectScript()) { raw ->
                val text = when {
                    raw.isNullOrBlank() || raw == "null" || raw == "\"null\"" -> ""
                    else -> runCatching {
                        JSONTokener(raw).nextValue().let { if (it is String) it else it.toString() }
                    }.getOrDefault(raw)
                }
                val arr = runCatching { JSONArray(text) }.getOrNull()
                val segs = arr?.let { (0 until it.length()).map { i -> it.optString(i) } }
                    ?.filter { it.isNotBlank() }.orEmpty()
                if (segs.isEmpty()) {
                    translating = false
                    trState = if (force) "没找到可翻译的正文，等页面加载完再点一次" else ""
                    return@evaluateJavascript
                }
                if (!force && !ArticleTranslator.isEnglish(segs)) {
                    translating = false
                    trState = ""
                    return@evaluateJavascript
                }
                trState = "正在就地翻译 ${segs.size} 段…"
                trJob = scope.launch {
                    ArticleTranslator.translate(card.url, segs)
                        .onSuccess { m ->
                            translated = m
                            applyChinese(v, m)
                            showChinese = true
                            trState = "已译成中文 · ${m.size} 段"
                        }
                        .onFailure { trState = "翻译服务不可达，点「中文」重试" }
                    translating = false
                }
            }
        }.onFailure {
            translating = false
            trState = "翻译脚本没注入成功，点「中文」重试"
        }
    }

    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            BackHandler {
                if (web?.canGoBack() == true) web?.goBack() else onClose()
            }
            DisposableEffect(Unit) {
                onDispose { trJob?.cancel(); web?.destroy(); web = null }
            }
            Column(
                Modifier.fillMaxSize()
                    .statusBarsPadding()
                    .navigationBarsPadding(),
            ) {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    IconButton(onClick = onClose, modifier = Modifier.size(36.dp)) {
                        Icon(Icons.Filled.Close, "关闭", Modifier.size(20.dp))
                    }
                    Column(Modifier.weight(1f).padding(horizontal = 4.dp)) {
                        Text(
                            title.ifBlank { card.title },
                            fontSize = 13.sp, maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text(
                            card.domain, fontSize = 10.sp, maxLines = 1,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    TextButton(
                        enabled = !translating,
                        onClick = {
                            val v = web ?: return@TextButton
                            if (showChinese) {
                                runCatching { v.evaluateJavascript("window.__twRestore()") {} }
                                showChinese = false
                                trState = ""
                            } else {
                                val m = translated
                                if (m != null) { applyChinese(v, m); showChinese = true }
                                else translatePage(v, force = true) // 手动点就一定有反应
                            }
                        },
                    ) {
                        Text(
                            if (translating) "翻译中" else if (showChinese) "原文" else "中文",
                            fontSize = 11.sp,
                        )
                    }
                    TextButton(onClick = onDetail) { Text("详情", fontSize = 11.sp) }
                    TextButton(onClick = {
                        runCatching {
                            ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(card.url)))
                        }
                    }) { Text("浏览器", fontSize = 11.sp) }
                }
                if (progress < 100) {
                    LinearProgressIndicator(Modifier.fillMaxWidth().height(2.dp))
                }
                if (trState.isNotBlank()) {
                    Text(trState, fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 2.dp))
                }
                if (failed) {
                    Text(
                        "页面没加载出来，可点右上「浏览器」打开，卡片内容仍在本机库里。",
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                    )
                }
                AndroidView(
                    modifier = Modifier.fillMaxWidth().weight(1f),
                    factory = { c ->
                        WebView(c).apply {
                            settings.apply {
                                javaScriptEnabled = true
                                domStorageEnabled = true
                                loadsImagesAutomatically = true
                                useWideViewPort = true
                                loadWithOverviewMode = true
                                setSupportZoom(true)
                                builtInZoomControls = true
                                displayZoomControls = false
                                allowFileAccess = false
                                mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
                                userAgentString = userAgentString + " TianwangNext/1.0"
                            }
                            webChromeClient = object : WebChromeClient() {
                                override fun onProgressChanged(view: WebView, newProgress: Int) {
                                    progress = newProgress
                                }

                                override fun onReceivedTitle(view: WebView, t: String?) {
                                    if (!t.isNullOrBlank()) title = t
                                }
                            }
                            webViewClient = object : WebViewClient() {
                                override fun shouldOverrideUrlLoading(
                                    view: WebView, req: WebResourceRequest,
                                ): Boolean {
                                    val scheme = req.url.scheme?.lowercase() ?: ""
                                    return if (scheme == "http" || scheme == "https") {
                                        false // 站内跳转也在 App 里翻，不甩给外部浏览器
                                    } else {
                                        runCatching {
                                            view.context.startActivity(
                                                Intent(Intent.ACTION_VIEW, req.url)
                                            )
                                        }
                                        true
                                    }
                                }

                                override fun onPageStarted(
                                    view: WebView, url: String, favicon: android.graphics.Bitmap?,
                                ) {
                                    // 站内跳转后重新来过，别把上一页的译文与闸门带过来
                                    trJob?.cancel()
                                    translated = null
                                    showChinese = false
                                    autoTried = false
                                    translating = false
                                    trState = ""
                                }

                                override fun onPageFinished(view: WebView, url: String) {
                                    progress = 100
                                    // 动态渲染页 onPageFinished 时正文还没进 DOM，稍等再收集
                                    view.postDelayed({ translatePage(view, force = false) }, 700)
                                }

                                override fun onReceivedError(
                                    view: WebView, req: WebResourceRequest, err: WebResourceError,
                                ) {
                                    if (req.isForMainFrame) failed = true
                                }
                            }
                            loadUrl(card.url)
                        }
                    },
                    update = { web = it },
                )
            }
        }
    }
}
