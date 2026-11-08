package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * 网页就地翻译 —— 跟浏览器翻译插件一个路子，不碰任何大模型、不吃任何密钥。
 *
 * 🔴 通道只探测一次：以前每段文本都要串行试三个接口，国内 gtx 不通时每段白等
 * 8~12 秒，30 段就是几分钟，用户点「中文」像死了一样。现在先用一句话挑出能通的
 * 通道，其余段落只用这条通道并发翻，全不通就立刻明确失败、允许重试。
 */
object ArticleTranslator {
    private const val TTL = 7 * 24 * 3600_000L
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(6, TimeUnit.SECONDS)
        .build()

    private fun interface Channel {
        fun translate(text: String): String?
    }

    private val channels: List<Channel> by lazy {
        // 按实测可达性排序（2026-11-07 本机：MyMemory 200/0.9s，
        // edge auth 404、gtx 连接层不通）。探测一次定下来，整页都走它。
        listOf(
            Channel { myMemory(it) },
            Channel { edge(it) },
            Channel { gtx(it) },
        )
    }

    @Volatile private var picked: Channel? = null
    @Volatile private var pickedAt: Long = 0L
    @Volatile private var failedAt: Long = 0L

    /** 用一句短语挑通道；挑中的缓存 10 分钟，全挂也只短暂缓存 15 秒便于重试。 */
    private fun pick(): Channel? {
        val now = System.currentTimeMillis()
        picked?.let { if (now - pickedAt < 10 * 60 * 1000L) return it }
        if (now - failedAt < 15 * 1000L) return null
        for (c in channels) {
            val v = runCatching { c.translate("Hello world") }.getOrNull()
            if (!v.isNullOrBlank() && !v.equals("Hello world", ignoreCase = true)) {
                picked = c
                pickedAt = now
                return c
            }
        }
        failedAt = now
        return null
    }

    /** 只看页面采样，英文为主才「自动」翻；用户手动点「中文」不走这个判断。 */
    fun isEnglish(samples: List<String>): Boolean {
        val s = samples.take(12).joinToString(" ").take(1200)
        if (s.length < 40) return false
        val latin = s.count { it.isLetter() && it.code < 128 }
        val han = s.count { it.code in 0x4e00..0x9fff }
        // 🔴 阿拉伯文 / 日文假名 / 韩文不是「英文页面」：通道是 en→zh，
        //    硬走只会把原文留在结果里（实测混进过整段阿拉伯语）。
        //    留一点余量（1/4）—— 英文文章里夹几个外来词不该被判成外文。
        val other = s.count {
            it.code in 0x0600..0x06FF || it.code in 0x3040..0x30FF || it.code in 0xAC00..0xD7AF
        }
        if (other * 4 > latin) return false
        return latin > 40 && han * 5 < latin
    }

    /** 浏览器插件式的那几行 JS：收集 / 回填 / 还原。 */
    fun injectScript(): String = """
(function(){
 if(window.__twReady) return;
 window.__twReady=true;
 window.__twNodes=[]; window.__twOrig=[];
 window.__twCollect=function(){
   window.__twNodes=[]; window.__twOrig=[];
   var out=[], skip=/^(SCRIPT|STYLE|NOSCRIPT|CODE|PRE|TEXTAREA|INPUT|SVG|CANVAS|NAV|FOOTER)$/;
   var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false), n;
   while((n=w.nextNode())){
     var p=n.parentNode;
     if(!p||skip.test(p.tagName)) continue;
     var t=(n.nodeValue||'').trim();
     if(t.length<2||t.length>500) continue;
     if(!/[A-Za-z]/.test(t)) continue;
     window.__twNodes.push(n); window.__twOrig.push(n.nodeValue); out.push(t);
     if(out.length>=30) break;
   }
   return JSON.stringify(out);
 };
 window.__twApply=function(json){
   try{
     var a=JSON.parse(json);
     for(var k in a){ var i=parseInt(k); if(window.__twNodes[i]&&a[k]) window.__twNodes[i].nodeValue=a[k]; }
     return 'ok';
   }catch(e){ return 'err'; }
 };
 window.__twRestore=function(){
   for(var i=0;i<window.__twNodes.length;i++){
     if(window.__twNodes[i]) window.__twNodes[i].nodeValue=window.__twOrig[i];
   }
   return 'ok';
 };
})();
""".trimIndent()

    /** 注入 + 收集合成一次调用，少一趟跨进程，也避免「注入还没跑完就收集」。 */
    fun collectScript(): String = injectScript() + "\nwindow.__twCollect();"

    /** 逐段并发翻，限 6 路；缓存命中不再发请求。 */
    suspend fun translate(url: String, segs: List<String>): Result<Map<Int, String>> =
        withContext(Dispatchers.IO) {
            if (segs.isEmpty()) return@withContext Result.success(emptyMap())
            val channel = pick()
                ?: return@withContext Result.failure(IllegalStateException("翻译服务不可达"))
            val cached = loadCache(url)
            val gate = Semaphore(6)
            val out = HashMap<Int, String>()
            try {
                coroutineScope {
                    val jobs = segs.take(30).mapIndexed { i, s ->
                        async {
                            gate.withPermit {
                                val hit = cached?.optString(i.toString(), "")
                                val v = if (!hit.isNullOrBlank()) hit
                                else runCatching { channel.translate(s) }.getOrNull()
                                if (!v.isNullOrBlank()) i to v else null
                            }
                        }
                    }
                    for (j in jobs) j.await()?.let { out[it.first] = it.second }
                }
            } catch (e: Exception) {
                return@withContext Result.failure(e)
            }
            if (out.isEmpty()) {
                // 整页一个都没翻成，多半是刚挑中的通道被限流/下线了：
                // 清掉选择，让用户点「中文」重试时能重新探测别的通道（不设失败冷却，
                // 否则重试会被冷却挡回去，又变成「点了没反应」）。
                picked = null
                Result.failure(IllegalStateException("翻译服务暂时不可达"))
            } else {
                saveCache(url, out)
                Result.success(out)
            }
        }

    /** 公开翻译端点之一：Google gtx，无需密钥。 */
    private fun gtx(text: String): String? {
        val u = "https://translate.googleapis.com/translate_a/single" +
            "?client=gtx&sl=auto&tl=zh-CN&dt=t&q=" +
            URLEncoder.encode(text.take(450), "UTF-8")
        val req = Request.Builder().url(u)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 13)")
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val body = r.body?.string() ?: return null
            val outer = runCatching { JSONArray(body).optJSONArray(0) }.getOrNull() ?: return null
            val sb = StringBuilder()
            for (i in 0 until outer.length()) {
                val seg = outer.optJSONArray(i) ?: continue
                seg.optJSONArray(0)?.optString(0)?.let { sb.append(it) }
            }
            return sb.toString().takeIf { it.isNotBlank() }
        }
    }

    @Volatile private var edgeToken: String = ""
    @Volatile private var edgeAt: Long = 0L

    /** 浏览器翻译插件常用的匿名通道：先拿临时令牌，再 POST 翻译，同样无需密钥。 */
    private fun edgeAuth(): String? {
        if (edgeToken.isNotBlank() && System.currentTimeMillis() - edgeAt < 8 * 60 * 1000L) {
            return edgeToken
        }
        val req = Request.Builder().url("https://edge.microsoft.com/translate/auth").build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val t = r.body?.string()?.trim().orEmpty()
            if (t.isBlank()) return null
            edgeToken = t
            edgeAt = System.currentTimeMillis()
            return t
        }
    }

    private fun edge(text: String): String? {
        val token = edgeAuth() ?: return null
        val body = JSONArray().put(JSONObject().put("Text", text.take(450))).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("https://api-edge.cognitive.microsofttranslator.com/translate" +
                "?from=en&to=zh-Hans&api-version=3.0")
            .header("Authorization", "Bearer $token")
            .header("Content-Type", "application/json")
            .post(body)
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val raw = r.body?.string() ?: return null
            return runCatching {
                JSONArray(raw).optJSONObject(0)
                    ?.optJSONArray("translations")?.optJSONObject(0)?.optString("text")
            }.getOrNull()?.takeIf { it.isNotBlank() }
        }
    }

    /** 备用端点：MyMemory，同样无需密钥。 */
    private fun myMemory(text: String): String? {
        val u = "https://api.mymemory.translated.net/get?q=" +
            URLEncoder.encode(text.take(450), "UTF-8") + "&langpair=en|zh-CN"
        val req = Request.Builder().url(u).build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val body = r.body?.string() ?: return null
            return runCatching {
                JSONObject(body).optJSONObject("responseData")?.optString("translatedText")
            }.getOrNull()?.takeIf { it.isNotBlank() && !it.startsWith("MYMEMORY WARNING", true) }
        }
    }

    private fun sha1(s: String): String =
        MessageDigest.getInstance("SHA-1").digest(s.toByteArray())
            .joinToString("") { "%02x".format(it) }

    private fun cacheFile(url: String): File =
        File(File(App.ctx.cacheDir, "trans").apply { mkdirs() }, sha1(url) + ".json")

    private fun loadCache(url: String): JSONObject? {
        val f = cacheFile(url)
        if (!f.exists() || System.currentTimeMillis() - f.lastModified() > TTL) return null
        return runCatching { JSONObject(f.readText()) }.getOrNull()
    }

    private fun saveCache(url: String, map: Map<Int, String>) {
        val o = JSONObject()
        for ((k, v) in map) o.put(k.toString(), v)
        runCatching { cacheFile(url).writeText(o.toString()) }
    }
}
