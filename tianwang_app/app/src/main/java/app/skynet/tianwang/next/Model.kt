package app.skynet.tianwang.next

import java.io.File
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * 双后端模型层。
 *
 * 🔴 为什么要写成「一个接口 + 两个实现」，而不是学上游各做一套：
 *   - pocketpal-ai 只跑本地 GGUF（llama.cpp），断网能用、但手机上 3B 以上就吃力；
 *   - Operit 的架子是给云端 API 铺的（OpenAI/Anthropic/Gemini），强、但没网就废，
 *     而且隐私数据全出手机。
 *   两个各有各的死角，所以真正的产品必须**一个界面、两个引擎、随时切**：
 *   闲聊/敏感内容走本地，重活/长上下文走云端，甚至同一轮里云端规划、本地收尾。
 *   ⇒ 上层 UI 只认 [ModelBackend]，永远不写 `if (是不是本地)`。
 */

enum class BackendKind { LOCAL, CLOUD }

/** 一个可选的模型（本地是一个 GGUF 文件，云端是一条端点+模型名）。 */
data class ModelProfile(
    val id: String = UUID.randomUUID().toString(),
    val label: String,
    val kind: BackendKind,
    /** 本地：GGUF 绝对路径；云端：baseUrl（如 https://api.deepseek.com/v1） */
    val target: String = "",
    /** 云端：模型名（本地留空，从文件读） */
    val model: String = "",
    /** 用户「想要」的上下文长度上限；Device 层会按可用内存和模型训练长度夹一次 */
    val ctx: Int = 0,
    val temperature: Float = 0.7f,
    val topP: Float = 0.9f,
    val maxTokens: Int = 0,
    /** 是否带工具调用（云端原生 function calling；本地靠提示词约束） */
    val tools: Boolean = true,
    val down: Boolean = false,          // 本地模型是否已下载完
    val sizeMb: Int = 0,
    val note: String = "",
) {
    /** 文件真实字节数（本地）；云端/不存在返回 0。 */
    val bytes: Long
        get() = if (kind == BackendKind.LOCAL) runCatching { File(target).length() }.getOrDefault(0L) else 0L

    val fileMb: Long get() = bytes / (1024 * 1024)
}

/** 统一的推理后端。两个实现见 [LocalBackend] / [CloudBackend]。 */
interface ModelBackend {
    val kind: BackendKind
    suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit = {},
    ): Result<String>
    suspend fun probe(profile: ModelProfile): Result<String>
}

/**
 * 本地（llama.cpp / GGUF）——真实现，JNI 静态链进 app 的单 .so（libfleet_llama.so）。
 *
 * 🔴 「一个 APP 解决所有」的本地那一半：模型文件在 app 私有外置目录里，
 * 加载/推理/模板全在 app 进程内完成，不借外部 app、不连外部 server。
 *
 * 🔴 **必须是单例**（2026-10-25 修）：旧版每次 chat 都 `LocalBackend()` new 一个，
 * 句柄缓存活在临时对象上，一轮结束就没了 —— 等于每说一句话都重新 mmap 一个
 * 1G~2.5G 的模型。表现为「第二句还是那么慢」，其实是缓存从来没命中过。
 *
 * 每轮不再清空 KV：native 侧按最长公共前缀复用，多轮对话只算增量。
 */
object LocalBackend : ModelBackend {
    override val kind = BackendKind.LOCAL

    /** profile.id → native 会话句柄 + 它当时用的加载参数。 */
    private class Loaded(val handle: Long, val ctx: Int, val gpuPercent: Int, val threads: Int)

    private val handles = HashMap<String, Loaded>()

    /** 当前正在用的句柄（供 UI 中止）。0 = 没在跑。 */
    @Volatile
    var activeHandle: Long = 0
        private set

    private fun sessionFor(profile: ModelProfile): Result<Loaded> {
        if (!profile.down) {
            return Result.failure(IllegalStateException("这个模型还没就绪（文件不在）"))
        }
        synchronized(handles) { handles[profile.id] }?.let { return Result.success(it) }
        if (!LlamaNative.ensureLoaded()) {
            return Result.failure(IllegalStateException("native 库没加载上（缺 libfleet_llama.so？）"))
        }

        // 🔴 参数由设备实测推出来，没有一个常量是「某台手机的值」
        val plan = Dev.plan(App.ctx, profile.fileMb, Prefs.mode, profile)
        val h = LlamaNative.nativeLoadModel(profile.target, plan.ctx, plan.threads, plan.gpuPercent)
        if (h == 0L) {
            return Result.failure(IllegalStateException(
                "模型加载失败：${profile.label}\n计划：${plan.reason}"))
        }
        val loaded = Loaded(h, plan.ctx, plan.gpuPercent, plan.threads)
        synchronized(handles) {
            handles.remove(profile.id)?.let { LlamaNative.nativeFreeModel(it.handle) }
            handles[profile.id] = loaded
        }
        return Result.success(loaded)
    }

    /** 这套模型当前会用什么参数（设置页/模型页展示用，不触发加载）。 */
    fun previewPlan(profile: ModelProfile): InferencePlan =
        Dev.plan(App.ctx, profile.fileMb, Prefs.mode, profile)

    override suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> = withContext(Dispatchers.IO) {
        val loaded = sessionFor(profile).getOrElse { return@withContext Result.failure(it) }

        val roles = ArrayList<String>(messages.size + 1)
        val contents = ArrayList<String>(messages.size + 1)

        // 🔴 语言约束必须先于对话落进去（2026-10-26）：
        //    0.6B 这类小模型没人管就会冒英文，而主人明确「英文看不懂」。
        //    放在 system 位而不是拼在用户问题后面 —— 后者会污染用户原话、
        //    也会被 KV 前缀复用当成每轮都变的内容，白算一遍。
        val lang = Prefs.replyLang
        if (lang.instruct.isNotEmpty()) {
            roles.add("system")
            contents.add(lang.instruct)
        }

        for (m in messages) {
            if (m.text.isBlank()) continue
            roles.add(
                when (m.role) {
                    ChatMsg.Role.USER -> "user"
                    ChatMsg.Role.ASSISTANT -> "assistant"
                    ChatMsg.Role.SYSTEM -> "system"
                }
            )
            // 本地模型没有真正的 tool calling，把工具结果折成上下文里的文字，
            // 这样它至少知道「刚才那个动作的结果是什么」，而不是当没发生过。
            contents.add(m.text)
        }
        if (roles.isEmpty()) return@withContext Result.failure(IllegalStateException("没有可发送的内容"))

        val maxTok = if (profile.maxTokens > 0) profile.maxTokens else Prefs.maxTokens
        activeHandle = loaded.handle
        try {
            val full = LlamaNative.nativeChatMessages(
                loaded.handle,
                roles.toTypedArray(),
                contents.toTypedArray(),
                maxTok,
                Prefs.noThink,
                LlamaNative.TokenCallback { onDelta(it) },
            )
            Result.success(full)
        } catch (e: Throwable) {
            Result.failure(e)
        } finally {
            activeHandle = 0
        }
    }

    override suspend fun probe(profile: ModelProfile): Result<String> = withContext(Dispatchers.IO) {
        val loaded = sessionFor(profile).getOrElse { return@withContext Result.failure(it) }
        val gpu = LlamaNative.gpuInfo()
        Result.success(
            "GGUF 就绪：${profile.label}\n" +
                "句柄 ${loaded.handle} · ctx ${loaded.ctx} · 线程 ${loaded.threads} · " +
                "GPU ${loaded.gpuPercent}%（本机 ${gpu.first} 个设备 ${gpu.second}）"
        )
    }

    /** 中止当前生成。手机没有 ESC，长生成要么能停、要么就只能杀 app。 */
    fun abort() {
        if (activeHandle != 0L) LlamaNative.nativeAbort()
    }

    /** 卸载某个模型的会话（切模型/清理时用）。 */
    fun release(profileId: String) {
        synchronized(handles) {
            handles.remove(profileId)?.let { LlamaNative.nativeFreeModel(it.handle) }
        }
    }
}

/**
 * 云端（OpenAI 兼容 /v1/chat/completions，SSE 流式）。
 *
 * 🔴 这个后端解决的是「模型页里选了云模型就能直接用」，
 * 与「云侧 agent loop」（走 Net.streamAgent，带工具）是两条不同的路：
 * 前者是纯对话、只连模型厂商；后者是让电脑侧的编队替手机干活。
 * 两者都要有 —— 一个保底能聊，一个能把事做成。
 */
object CloudBackend : ModelBackend {
    override val kind = BackendKind.CLOUD

    override suspend fun chat(
        profile: ModelProfile,
        messages: List<ChatMsg>,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> = withContext(Dispatchers.IO) {
        if (profile.target.isBlank() || profile.model.isBlank()) {
            return@withContext Result.failure(
                IllegalStateException("端点和模型名都得填（模型页里还没选具体模型）"))
        }
        val url = profile.target.trimEnd('/') + "/chat/completions"
        val key = Net.loadConfig().token

        val arr = JSONArray()
        // 直连网关时没有云侧的 system 提示可用，语言约束只能本机自己加
        val langInstruct = Prefs.replyLang.instruct
        if (langInstruct.isNotEmpty()) {
            arr.put(JSONObject().put("role", "system").put("content", langInstruct))
        }
        for (m in messages) {
            if (m.text.isBlank()) continue
            arr.put(JSONObject().put("role", when (m.role) {
                ChatMsg.Role.USER -> "user"
                ChatMsg.Role.ASSISTANT -> "assistant"
                ChatMsg.Role.SYSTEM -> "system"
            }).put("content", m.text))
        }
        val body = JSONObject()
            .put("model", profile.model)
            .put("messages", arr)
            .put("stream", true)
            .put("temperature", profile.temperature.toDouble())
            .put("max_tokens", if (profile.maxTokens > 0) profile.maxTokens else Prefs.maxTokens)
            .toString()

        var conn: HttpURLConnection? = null
        try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                setRequestProperty("Content-Type", "application/json")
                if (key.isNotBlank()) setRequestProperty("Authorization", "Bearer $key")
                connectTimeout = 12_000
                readTimeout = 180_000
                doOutput = true
            }
            conn.outputStream.write(body.toByteArray(Charsets.UTF_8))

            val code = conn.responseCode
            if (code !in 200..299) {
                val err = runCatching {
                    (conn.errorStream ?: conn.inputStream).bufferedReader().readText()
                }.getOrDefault("")
                return@withContext Result.failure(
                    IllegalStateException("HTTP $code ${err.take(160)}"))
            }

            val sb = StringBuilder()
            conn.inputStream.bufferedReader().forEachLine { line ->
                if (!line.startsWith("data:")) return@forEachLine
                val data = line.removePrefix("data:").trim()
                if (data == "[DONE]") return@forEachLine
                val j = runCatching { JSONObject(data) }.getOrNull() ?: return@forEachLine
                val delta = j.optJSONArray("choices")?.optJSONObject(0)
                    ?.optJSONObject("delta")?.optString("content") ?: ""
                if (delta.isNotEmpty()) { sb.append(delta); onDelta(delta) }
            }
            Result.success(sb.toString())
        } catch (e: Throwable) {
            Result.failure(e)
        } finally {
            conn?.disconnect()
        }
    }

    override suspend fun probe(profile: ModelProfile): Result<String> = withContext(Dispatchers.IO) {
        if (profile.target.isBlank()) return@withContext Result.failure(
            IllegalStateException("端点没填"))
        val url = profile.target.trimEnd('/') + "/models"
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                val k = Net.loadConfig().token
                if (k.isNotBlank()) setRequestProperty("Authorization", "Bearer $k")
                connectTimeout = 8000
                readTimeout = 12_000
            }
            val code = conn.responseCode
            if (code !in 200..299) return@withContext Result.failure(
                IllegalStateException("HTTP $code"))
            val txt = conn.inputStream.bufferedReader().readText()
            val n = runCatching { JSONObject(txt).optJSONArray("data")?.length() ?: 0 }.getOrDefault(0)
            Result.success("端点可达 · $n 个模型")
        } catch (e: Throwable) {
            Result.failure(e)
        } finally {
            conn?.disconnect()
        }
    }
}

/** 按 profile 取后端。唯一一处 if，别让这个判断漏到 UI 里去。 */
fun backendFor(profile: ModelProfile): ModelBackend =
    if (profile.kind == BackendKind.LOCAL) LocalBackend else CloudBackend
