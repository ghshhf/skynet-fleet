package app.skynet.tianwang.next

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** 云侧连接 + 对话 SSE + 工具箱 + 进度。单例，配置存 SharedPreferences。 */
object Net {
    private const val SP = "tianwang_agent"
    private const val K_URL = "agent_url"
    private const val K_TOKEN = "agent_token"

    /** 长期设备凭据的头，值形如 `deviceId:secret`。 */
    private const val DEVICE_HEADER = "X-Fleet-Device"

    private lateinit var sp: SharedPreferences

    fun init(ctx: Context) {
        sp = ctx.applicationContext.getSharedPreferences(SP, Context.MODE_PRIVATE)
    }

    fun saveConfig(url: String, token: String) {
        sp.edit().putString(K_URL, url.trim()).putString(K_TOKEN, token.trim()).apply()
    }

    fun loadConfig(): AgentConfig {
        // 注意：getString 的第二个参数只在 key 不存在时生效。用户之前点过「保存」
        // 但地址框是空的，存下来的就是空串，光给默认值救不回来 —— 所以这里显式
        // 把空串也折回默认地址。没这个兜底，界面会一直显示「没填地址」。
        val savedUrl = sp.getString(K_URL, "") ?: ""
        val savedToken = sp.getString(K_TOKEN, "") ?: ""
        return AgentConfig(
            url = if (savedUrl.isBlank()) Api.DEFAULT_BASE else savedUrl,
            token = savedToken,
        )
    }

    /**
     * 能连云侧吗。两种钥匙任一即可：
     *   · token —— 老的一次性令牌（本机调试、老版本电脑端）；
     *   · deviceId/secret —— 配对换来的长期凭据（电脑重启也不变）。
     * 只认 token 会让「配对后清空 token」的流程永远显示未配置。
     */
    fun hasConfig(): Boolean {
        val cfg = loadConfig()
        return Api.normalizeBase(cfg.url).isNotEmpty() &&
            (cfg.token.isNotBlank() || Prefs.paired())
    }

    /**
     * 取一段合成好的语音（wav 字节）。失败返回 null —— 出声失败不该搞崩对话。
     *
     * 为什么合成放 PC 不放手机：这台真机的系统 TTS 是废的。
     * 2026-11-01 实测 `TextToSpeech` 初始化直接回 -1，连系统设置页自带的
     * 『播放』按钮都不产生任何音频轨道（`dumpsys media.audio_flinger` 恒为 0）。
     * 所以手机只负责播，嘴长在 PC 上。
     */
    suspend fun fetchSpeech(text: String, voice: String = "xiaoxiao"): ByteArray? =
        withContext(Dispatchers.IO) {
            if (text.isBlank() || !hasConfig()) return@withContext null
            val url = Api.endpoint(loadConfig().url, "/api/tts") +
                "?text=" + URLEncoder.encode(text, "UTF-8") +
                "&voice=" + URLEncoder.encode(voice, "UTF-8")
            try {
                val c = auth(url)
                c.readTimeout = 30_000
                try {
                    if (c.responseCode != 200) return@withContext null
                    c.inputStream.readBytes()
                } finally { c.disconnect() }
            } catch (e: Exception) {
                null
            }
        }

    private fun auth(url: String): HttpURLConnection {
        val c = URL(url).openConnection() as HttpURLConnection
        val cfg = loadConfig()
        // 两把钥匙都带：新电脑认长期凭据，老电脑只认一次性令牌。
        if (cfg.token.isNotBlank()) c.setRequestProperty("X-Fleet-Token", cfg.token)
        if (Prefs.paired()) c.setRequestProperty(DEVICE_HEADER, "${Prefs.deviceId}:${Prefs.deviceSecret}")
        c.connectTimeout = 8000
        c.readTimeout = if (url.contains("/api/agent")) 120_000 else 15_000
        return c
    }

    /**
     * 拿电脑屏幕上的 6 位配对码换长期凭据。POST /api/pair/claim。
     *
     * 配对请求本身没有凭据（凭据是这次换来的），所以不走 [auth] ——
     * 否则会把还没被电脑认下的旧钥匙带上去。
     * 调用方拿到 [DeviceClaim] 后自己落盘，并清空旧 token。
     */
    suspend fun claim(
        base: String,
        code: String,
        name: String,
        platform: String = "android",
    ): DeviceClaim = withContext(Dispatchers.IO) {
        val c = URL(Api.endpoint(base, "/api/pair/claim")).openConnection() as HttpURLConnection
        c.requestMethod = "POST"
        c.setRequestProperty("Content-Type", "application/json")
        c.connectTimeout = 8000
        c.readTimeout = 15_000
        c.doOutput = true
        try {
            c.outputStream.use {
                it.write(
                    JSONObject()
                        .put("code", code.trim())
                        .put("name", name.trim())
                        .put("platform", platform.trim())
                        .toString().toByteArray(Charsets.UTF_8)
                )
            }
            val http = c.responseCode
            val body = c.readBody()
            if (http != 200) throw IOException("HTTP $http：${body.take(120)}")
            val o = JSONObject(body)
            if (!o.optBoolean("ok", false)) {
                throw IOException(o.optString("err", "配对失败（电脑没给原因）"))
            }
            val id = o.optString("device_id", "").trim()
            val secret = o.optString("secret", "").trim()
            if (id.isEmpty() || secret.isEmpty()) {
                throw IOException("配对响应缺 device_id/secret")
            }
            DeviceClaim(id, secret)
        } finally {
            c.disconnect()
        }
    }

    /**
     * 裸 WAV 进 → 识别文字出。老工程同一个端点（`/api/voice/recognize`），
     * 服务端只收 `audio/wav`，不要改成 multipart —— 那边不解析表单。
     */
    suspend fun recognize(wav: ByteArray): String = withContext(Dispatchers.IO) {
        if (!hasConfig()) throw IOException("云侧未配置，没地方识别")
        val c = auth(Api.endpoint(loadConfig().url, "/api/voice/recognize"))
        c.requestMethod = "POST"
        c.setRequestProperty("Content-Type", "audio/wav")
        c.readTimeout = 180_000
        c.doOutput = true
        try {
            c.outputStream.use { it.write(wav) }
            val code = c.responseCode
            val body = c.readBody()
            if (code != 200) {
                throw IOException("HTTP $code：${body.take(120)}")
            }
            val o = JSONObject(body)
            if (!o.optBoolean("ok", false)) {
                throw IOException(o.optString("err", "识别失败（服务端没给原因）"))
            }
            o.optString("text", "")
        } finally {
            c.disconnect()
        }
    }

    suspend fun getProviders(): List<Gateway> = withContext(Dispatchers.IO) {
        val c = auth(Api.endpoint(loadConfig().url, "/api/providers"))
        try {
            if (c.responseCode != 200) return@withContext emptyList()
            val root = JSONObject(c.readBody())
            root.optJSONArray("providers")?.toGateways() ?: emptyList()
        } finally { c.disconnect() }
    }

    suspend fun getCaps(): Map<String, List<Capability>> = withContext(Dispatchers.IO) {
        val c = auth(Api.endpoint(loadConfig().url, "/api/caps"))
        try {
            if (c.responseCode != 200) return@withContext emptyMap()
            val root = JSONObject(c.readBody())
            val groups = mutableMapOf<String, List<Capability>>()
            root.optJSONObject("groups")?.let { g ->
                val caps = root.optJSONArray("caps")?.toCapabilities() ?: emptyList()
                for (k in g.keys()) {
                    val names = g.optJSONArray(k) ?: JSONArray()
                    val list = mutableListOf<Capability>()
                    for (i in 0 until names.length()) {
                        val n = names.optString(i)
                        caps.firstOrNull { it.name == n }?.let { list.add(it) }
                    }
                    groups[k] = list
                }
            }
            groups
        } finally { c.disconnect() }
    }

    suspend fun execTool(name: String, args: Map<String, Any>): ToolResult =
        withContext(Dispatchers.IO) {
            val c = auth(Api.endpoint(loadConfig().url, "/api/tool/exec"))
            c.requestMethod = "POST"
            c.setRequestProperty("Content-Type", "application/json")
            c.doOutput = true
            c.outputStream.write(JSONObject().put("name", name).put("args", JSONObject(args)).toString().encodeToByteArray())
            try {
                if (c.responseCode != 200) return@withContext ToolResult(false, err = "HTTP ${c.responseCode}")
                val r = JSONObject(c.readBody())
                ToolResult(r.optBoolean("ok", false), r.optString("data", "").ifEmpty { r.optString("text", "") }, r.optString("err", ""))
            } finally { c.disconnect() }
        }

    /**
     * 编队节点快照。数据全部来自云侧 /api/nodes —— 本机信息、注册表成员、
     * 控制面是否可用。拉不到就返回空并带上原因，绝不塞占位节点糊弄界面。
     */
    suspend fun getNodes(): FleetSnapshot = withContext(Dispatchers.IO) {
        val c = auth(Api.endpoint(loadConfig().url, "/api/nodes"))
        try {
            if (c.responseCode != 200) {
                return@withContext FleetSnapshot(err = "HTTP ${c.responseCode}")
            }
            val root = JSONObject(c.readBody())
            val selfJ = root.optJSONObject("self")
            val self = selfJ?.let {
                NodeInfo(
                    name = it.optString("name", "本机"),
                    host = run {
                        val a = it.optJSONArray("ips")
                        if (a == null || a.length() == 0) "127.0.0.1"
                        else (0 until a.length()).joinToString(", ") { i -> a.optString(i) }
                    },
                    online = true,
                    caps = it.optInt("caps", 0),
                    note = "${it.optString("platform", "")} · ${it.optString("role", "")}",
                )
            }
            val list = mutableListOf<NodeInfo>()
            root.optJSONArray("nodes")?.let { a ->
                for (i in 0 until a.length()) {
                    val o = a.optJSONObject(i) ?: continue
                    list.add(
                        NodeInfo(
                            name = o.optString("name"),
                            host = o.optString("host", "-"),
                            online = o.optBoolean("online", true),
                            caps = o.optInt("caps", 0),
                            note = o.optString("note", ""),
                        )
                    )
                }
            }
            val cp = root.optJSONObject("controlplane")
            FleetSnapshot(
                self = self,
                nodes = list,
                cpAvailable = cp?.optBoolean("available", false) ?: false,
                cpErr = cp?.optString("err", "") ?: "",
                err = "",
            )
        } catch (e: Exception) {
            FleetSnapshot(err = e.message ?: e.javaClass.simpleName)
        } finally {
            c.disconnect()
        }
    }

    suspend fun getTasks(): TaskBoard = withContext(Dispatchers.IO) {
        val c = auth(Api.endpoint(loadConfig().url, "/api/tasks"))
        try {
            if (c.responseCode != 200) return@withContext TaskBoard()
            val root = JSONObject(c.readBody())
            val phases = mutableListOf<Phase>()
            root.optJSONArray("phases")?.let { a ->
                for (i in 0 until a.length()) {
                    val p = a.optJSONObject(i) ?: continue
                    phases.add(Phase(p.optString("id"), p.optString("name"), p.optBoolean("done")))
                }
            }
            val commits = mutableListOf<String>()
            root.optJSONArray("recent_commits")?.let { a ->
                for (i in 0 until a.length()) commits.add(a.optString(i))
            }
            TaskBoard(phases, commits, root.optString("branch"))
        } finally { c.disconnect() }
    }

    /** 对话：POST /api/agent，逐事件回调。手机端因此接到真实工具卡 + 回答。 */
    suspend fun streamAgent(
        question: String,
        onStep: (ToolCall) -> Unit,
        onAnswer: (String, List<ToolCall>) -> Unit,
        onError: (String) -> Unit,
    ) = withContext(Dispatchers.IO) {
        val c = auth(Api.endpoint(loadConfig().url, "/api/agent"))
        c.requestMethod = "POST"
        c.setRequestProperty("Content-Type", "application/json")
        c.doOutput = true
        // 语言偏好跟着请求走：同一台云侧可能连好几台设备，各人语言不同
        c.outputStream.write(
            JSONObject().put("question", question)
                .put("lang", Prefs.replyLang.key)
                .toString().encodeToByteArray()
        )
        val steps = mutableListOf<ToolCall>()
        try {
            if (c.responseCode != 200) {
                onError("HTTP ${c.responseCode}")
                return@withContext
            }
            val parser = SseParser()
            c.inputStream.bufferedReader().useLines { lines ->
                for (line in lines) {
                    parser.feed(line)?.let { dispatch(it.event, it.data, steps, onStep, onAnswer) }
                }
            }
            parser.flush()?.let { dispatch(it.event, it.data, steps, onStep, onAnswer) }
        } catch (e: Exception) {
            onError(e.message ?: e.javaClass.simpleName)
        } finally { c.disconnect() }
    }

    private fun dispatch(event: String, data: String, steps: MutableList<ToolCall>,
                         onStep: (ToolCall) -> Unit, onAnswer: (String, List<ToolCall>) -> Unit) {
        val j = runCatching { JSONObject(data) }.getOrNull() ?: return
        when (event) {
            "step" -> {
                val cap = j.optString("cap")
                val phase = j.optString("phase")
                val ok = j.optBoolean("ok", true)
                val txt = j.optString("text", "")
                val ms = j.optLong("ms", 0)
                if (phase == "exec" || phase == "done" || j.has("cap")) {
                    val existing = steps.firstOrNull { it.name == cap && it.ms == 0L && it.state == ToolCall.State.RUNNING }
                    if (existing != null) {
                        val idx = steps.indexOf(existing)
                        steps[idx] = existing.copy(state = if (ok) ToolCall.State.OK else ToolCall.State.FAIL, result = txt, ms = ms)
                        onStep(steps[idx])
                    } else {
                        val tc = ToolCall(name = cap, args = j.optString("args", ""), state = ToolCall.State.RUNNING)
                        steps.add(tc); onStep(tc)
                    }
                }
            }
            "status" -> { /* 轮次/总结状态，UI 暂不需要逐条 */ }
            "answer" -> {
                onAnswer(j.optString("answer", ""), steps.toList())
            }
        }
    }

    private fun HttpURLConnection.readBody(): String =
        (if (responseCode in 200..299) inputStream else errorStream).bufferedReader().readText()
}

/** 配对换来的长期凭据。secret 只落 SharedPreferences，不进日志/界面。 */
data class DeviceClaim(val deviceId: String, val secret: String)

fun JSONArray.toGateways(): List<Gateway> {
    val out = mutableListOf<Gateway>()
    for (i in 0 until length()) {
        val p = optJSONObject(i) ?: continue
        out.add(Gateway(
            id = p.optString("id"),
            status = if (p.optBoolean("alive")) "活" else "死",
            endpoint = p.optString("base").replace("http://", "").replace("https://", ""),
            latencyMs = p.optInt("latency_ms", 0),
            modelCount = (p.optJSONArray("models")?.length() ?: 0),
            note = p.optString("note", ""),
        ))
    }
    return out
}

fun JSONArray.toCapabilities(): List<Capability> {
    val out = mutableListOf<Capability>()
    for (i in 0 until length()) {
        val c = optJSONObject(i) ?: continue
        val params = mutableListOf<CapParam>()
        c.optJSONArray("params")?.let { a ->
            for (k in 0 until a.length()) {
                val p = a.optJSONObject(k) ?: continue
                params.add(CapParam(p.optString("name"), p.optString("type", "str"), p.optString("desc", ""), p.optBoolean("required")))
            }
        }
        out.add(Capability(
            name = c.optString("name"), title = c.optString("title"),
            domain = c.optString("domain"), params = params,
            sideEffect = c.optBoolean("side_effect"), destructive = c.optBoolean("destructive"),
            approval = c.optString("approval", "auto"), note = c.optString("note", ""),
        ))
    }
    return out
}

fun String.urlEnc(): String = URLEncoder.encode(this, "UTF-8")
