package app.skynet.tianwang.next

import android.annotation.SuppressLint
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import org.json.JSONTokener
import kotlin.coroutines.resume

/**
 * 无头渲染：把页面交给一个**不显示**的 WebView 跑一遍 JS，再取回渲染后的 DOM。
 *
 * 为什么需要第二通道：OkHttp 只拿得到服务器首包 HTML。知乎、微信、飞书文档、
 * 各类 SPA 的首包是个空壳，正文全靠 JS 填 —— 这类链接直连读出来只有几百字导航，
 * 表现成「打开成功但没内容」，比失败更难判断。
 *
 * 为什么不是「打开浏览器」：这里没有任何界面、没有焦点、不打扰用户，
 * 加载完立刻销毁。用户给的是链接，要的是结果，中间过程不该占屏幕。
 *
 * 🔴 WebView 只能在主线程创建与销毁；所有回调都在主线程。
 * 超时就**一定**返回失败并销毁 —— 不销毁会漏内存，一个漏几十 MB。
 */
object WebRender {

    private const val TIMEOUT = 20_000L

    /** onPageFinished 之后再等一会儿：很多站是「先骨架、后填内容」，立刻取会拿到空壳。 */
    private const val SETTLE = 700L

    private const val UA =
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

    @SuppressLint("SetJavaScriptEnabled")
    suspend fun html(url: String): Result<String> = withContext(Dispatchers.Main) {
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            return@withContext Result.failure(IllegalArgumentException("url 必须以 http(s) 开头"))
        }
        suspendCancellableCoroutine { cont ->
            val main = Handler(Looper.getMainLooper())

            val wv = try {
                WebView(App.ctx)
            } catch (t: Throwable) {
                cont.resume(Result.failure(t))
                return@suspendCancellableCoroutine
            }

            var closed = false

            fun close(result: Result<String>) {
                if (closed) return
                closed = true
                main.removeCallbacksAndMessages(null)
                runCatching {
                    wv.stopLoading()
                    wv.loadUrl("about:blank")
                    (wv.parent as? ViewGroup)?.removeView(wv)
                    wv.destroy()
                }
                cont.resume(result)
            }

            runCatching {
                wv.settings.apply {
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    // 不加载图片：正文抽取用不到，省流量也省内存
                    blockNetworkImage = true
                    userAgentString = UA
                    loadWithOverviewMode = true
                    useWideViewPort = true
                }
                // 🔴 WebView 的语言头与系统语言是两套：不 attach 到窗口时它取进程默认，
                //    实测 example.com 回来的是阿拉伯语版（系统明明是 zh-CN）。
                //    loadUrl 的 header 又会在重定向时丢掉，所以还要在 settings 上设。
                //    setAcceptLanguage 只在系统接口里，公开 SDK 编不过 —— 反射调，
                //    机型不允许就静默退回 loadUrl 的 header，不让渲染整条挂掉。
                runCatching {
                    wv.settings.javaClass
                        .getMethod("setAcceptLanguage", String::class.java)
                        .invoke(wv.settings, "zh-CN,zh;q=0.9,en;q=0.8")
                }
                // 不 attach 也要给一个尺寸，否则部分站点按 0×0 视口渲染，
                // 懒加载的正文永远不出现。
                wv.measure(
                    View.MeasureSpec.makeMeasureSpec(1080, View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec(1920, View.MeasureSpec.EXACTLY),
                )
                wv.layout(0, 0, 1080, 1920)

                wv.webViewClient = object : WebViewClient() {
                    override fun onPageFinished(view: WebView?, u: String?) {
                        // 只认主框架，子 iframe 的 finished 不算
                        if (view !== wv) return
                        main.postDelayed({
                            if (closed) return@postDelayed
                            runCatching {
                                wv.evaluateJavascript(
                                    "(function(){return document.documentElement?" +
                                        "document.documentElement.outerHTML:'';})()",
                                ) { value ->
                                    if (!closed) {
                                        val html = runCatching {
                                            JSONTokener(value ?: "").nextValue() as? String
                                        }.getOrNull().orEmpty()
                                        if (html.length < 200) {
                                            close(Result.failure(IllegalStateException(
                                                "渲染后仍拿不到页面（可能是登录墙或纯 Canvas）")))
                                        } else {
                                            close(Result.success(html))
                                        }
                                    }
                                }
                            }.onFailure { close(Result.failure(it)) }
                        }, SETTLE)
                    }
                }

                main.postDelayed({
                    close(Result.failure(java.io.IOException("渲染超时（${TIMEOUT / 1000} 秒）")))
                }, TIMEOUT)

                // 必须显式带语言头：WebView 不 attach 时拿不到系统语言，
                // 站点会按 IP 或默认值回退 —— 实测 example.com 回了阿拉伯语版，
                // 材料里混进一堆翻不了的非英文，看起来像解析失败。
                wv.loadUrl(url, mapOf("Accept-Language" to "zh-CN,zh;q=0.9,en;q=0.8"))
            }.onFailure { close(Result.failure(it)) }

            cont.invokeOnCancellation { close(Result.failure(IllegalStateException("已取消"))) }
        }
    }
}
