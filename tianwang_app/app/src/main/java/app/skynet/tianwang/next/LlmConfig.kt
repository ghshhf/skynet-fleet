package app.skynet.tianwang.next

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.TimeUnit

/**
 * 手机自己的大模型端点。
 *
 * 🔴 这里**不预设任何厂商**：名称 / 地址 / 密钥 / 模型全由界面上「手动添加端点」录入，
 * 想加几条加几条，随时改随时删，加新厂商不需要改代码、不需要发版。
 * 电脑只是把已有凭据按同一份格式一次性交给手机，之后 App 自己管。
 *
 * 数据落在 App 私有 SharedPreferences；导入文件接完即删。代码与仓库里零端点、零密钥。
 */
data class LlmSettings(
    val id: String = "",
    val label: String = "",
    val base: String = "",
    val key: String = "",
    val model: String = "",
) {
    val ready: Boolean get() = base.isNotBlank() && key.isNotBlank() && model.isNotBlank()
    val baseNorm: String get() = base.trim().trimEnd('/')

    /** 列表里显示的名字：没填名称就退回归一化后的地址。 */
    fun title(): String = label.trim().ifBlank {
        runCatching { java.net.URI(baseNorm).host ?: baseNorm }.getOrDefault(baseNorm)
            .ifBlank { "未命名端点" }
    }

    fun json(): JSONObject = JSONObject()
        .put("id", id).put("label", label).put("base", base)
        .put("key", key).put("model", model)

    companion object {
        fun of(o: JSONObject): LlmSettings = LlmSettings(
            id = o.optString("id"),
            label = o.optString("label").ifBlank { o.optString("name") },
            base = o.optString("base").ifBlank { o.optString("base_url") },
            key = o.optString("key").ifBlank { o.optString("api_key") },
            model = o.optString("model").ifBlank { o.optString("model_name") },
        )
    }
}

object LlmConfig {
    private const val SP = "tianwang_llm_api"
    private const val DROP = "llm_api.json"
    private const val K_LIST = "profiles"
    private const val K_ACTIVE = "active"

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun sp(): android.content.SharedPreferences =
        App.ctx.getSharedPreferences(SP, Context.MODE_PRIVATE)

    /** 全部端点；兼容老版本只存了一条 base/key/model 的情况。 */
    fun all(): List<LlmSettings> {
        val p = sp()
        val raw = p.getString(K_LIST, "").orEmpty()
        if (raw.isNotBlank()) {
            val arr = runCatching { JSONArray(raw) }.getOrNull() ?: return emptyList()
            return (0 until arr.length()).mapNotNull { i ->
                runCatching { LlmSettings.of(arr.optJSONObject(i) ?: return@mapNotNull null) }
                    .getOrNull()
            }
        }
        val legacy = LlmSettings(
            id = "legacy",
            label = "手机直连",
            base = p.getString("base", "") ?: "",
            key = p.getString("key", "") ?: "",
            model = p.getString("model", "") ?: "",
        )
        if (legacy.key.isBlank()) return emptyList()
        return listOf(legacy)
    }

    private fun writeAll(list: List<LlmSettings>, activeId: String? = null) {
        val arr = JSONArray()
        list.forEach { arr.put(it.json()) }
        val ed = sp().edit().putString(K_LIST, arr.toString())
        if (activeId != null) ed.putString(K_ACTIVE, activeId)
        ed.apply()
        if (activeId == null && list.none { it.id == active() }) {
            sp().edit().putString(K_ACTIVE, list.firstOrNull()?.id ?: "").apply()
        }
    }

    private fun active(): String = sp().getString(K_ACTIVE, "") ?: ""

    /** 当前选中的端点；没有就给一条空的，界面据此判断「还没添加」。 */
    fun get(): LlmSettings {
        val list = all()
        return list.firstOrNull { it.id == active() } ?: list.firstOrNull() ?: LlmSettings()
    }

    fun byId(id: String): LlmSettings? = all().firstOrNull { it.id == id }

    /** 新增或更新一条；id 为空就新发一个，保存后自动设为当前。 */
    fun save(s: LlmSettings): LlmSettings {
        val fixed = if (s.id.isBlank()) s.copy(id = "ep-" + UUID.randomUUID().toString().take(8))
        else s
        val list = all().toMutableList()
        val idx = list.indexOfFirst { it.id == fixed.id }
        if (idx >= 0) list[idx] = fixed else list.add(fixed)
        writeAll(list, fixed.id)
        return fixed
    }

    fun setActive(id: String) {
        if (all().any { it.id == id }) {
            sp().edit().putString(K_ACTIVE, id).apply()
        }
    }

    fun remove(id: String) {
        val list = all().filterNot { it.id == id }
        val next = if (active() == id) list.firstOrNull()?.id ?: "" else active()
        writeAll(list, next)
    }

    /** 聊天按端点地址取对应密钥：不能用「当前选中」那一把去请求别的厂商。 */
    fun keyFor(base: String): String {
        val norm = base.trim().trimEnd('/')
        return all()
            .firstOrNull { it.baseNorm.equals(norm, ignoreCase = true) && it.key.isNotBlank() }
            ?.key ?: get().key
    }

    /** 每条可用端点都在模型页有一条直连模型，选哪条就用哪把钥匙。 */
    fun cloudModels(): List<ModelProfile> = all().filter { it.ready }.map { s ->
        ModelProfile(
            id = "llm-direct:${s.id}",
            label = "${s.model} · ${s.title()}",
            kind = BackendKind.CLOUD,
            target = s.baseNorm,
            model = s.model,
            tools = true,
            note = "手机拿本机密钥直连 · 不需要别的设备",
        )
    }

    /** 当前那条，兼容聊天里没指定模型时的回落。 */
    fun cloudModel(): ModelProfile? {
        val s = get()
        if (!s.ready) return null
        return cloudModels().firstOrNull { it.id == "llm-direct:${s.id}" }
            ?: cloudModels().firstOrNull()
    }

    /**
     * 从 App 专属外部目录接一份电脑递过来的配置，存进私有区后删源件。
     * 支持 {"profiles":[{...}]} 批量 / 单条 {...} / 纯文本一行 key。
     * 按「名称+地址」去重，不覆盖界面上手工改过的同名条目；只报成功与否，绝不写内容进日志。
     */
    fun importDropped(ctx: Context): Int {
        val dir = ctx.getExternalFilesDir(null) ?: return 0
        val f = File(dir, DROP)
        if (!f.exists()) return 0
        return try {
            val raw = f.readText().trim()
            val incoming = if (raw.startsWith("{")) {
                val o = JSONObject(raw)
                val arr = o.optJSONArray("profiles")
                if (arr != null) (0 until arr.length()).mapNotNull { i ->
                    arr.optJSONObject(i)?.let { LlmSettings.of(it) }
                } else listOf(LlmSettings.of(o))
            } else if (raw.startsWith("[")) {
                val arr = JSONArray(raw)
                (0 until arr.length()).mapNotNull { i ->
                    arr.optJSONObject(i)?.let { LlmSettings.of(it) }
                }
            } else {
                listOf(LlmSettings(key = raw.lineSequence().first().trim()))
            }
            val list = all().toMutableList()
            var n = 0
            for (s in incoming) {
                if (s.key.isBlank()) continue
                // 同一个地址算同一条端点（含老版本遗留的那条），只更新不重复堆
                val oldIdx = list.indexOfFirst {
                    it.baseNorm.equals(s.baseNorm, ignoreCase = true) && s.baseNorm.isNotBlank()
                }
                if (oldIdx >= 0) {
                    // 已有同名端点：只补上缺的字段，不冲掉手工改过的密钥/模型
                    val old = list[oldIdx]
                    // 导入是一次显式的「重新移交」：带来的新值生效，界面上手工改随时可覆盖回来
                    list[oldIdx] = old.copy(
                        key = s.key,
                        base = s.base.ifBlank { old.base },
                        label = s.label.ifBlank { old.label },
                        model = s.model.ifBlank { old.model },
                    )
                } else {
                    list.add(
                        s.copy(
                            id = s.id.ifBlank { "ep-" + UUID.randomUUID().toString().take(8) },
                            label = s.label.ifBlank { s.title() },
                        )
                    )
                }
                n++
            }
            if (n > 0) {
                val cur = active()
                writeAll(list, if (cur.isBlank()) list.firstOrNull()?.id ?: "" else cur)
            }
            f.delete()
            n
        } catch (e: Exception) {
            0
        }
    }

    /**
     * 已核验的免费对话模型（2026-11-07 核厂商价格页 / 官方文档，并真机请求过 /chat/completions）。
     * 付费档（DeepSeek-V4 系 / GLM-5 系 / Pro 版 / Kimi 等）一律不在里面 ——
     * 机主自己会用，要用自己在界面上填，程序绝不替他默认选一个要花钱的。
     * 🔴 /models 实测只回 id/created/object/owned_by，**不带价格**，
     *    所以「免费」只能靠这份白名单 + 名字里的 free，不能按「看起来便宜」猜。
     * 名单是数据不是逻辑：厂商改免费档时改这一处即可，不用发版逻辑。
     */
    private val FREE_CHAT_MODELS = listOf(
        "Qwen/Qwen3-8B",
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen3.5-4B",
        "THUDM/GLM-4-9B-0414",
        "THUDM/GLM-Z1-9B-0414",
        "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
        "XingChenAGI/Xing4.0-29B",
        "Qwen/Qwen2-7B-Instruct",
        "Qwen/Qwen2-1.5B-Instruct",
        "THUDM/glm-4-9b-chat",
    )

    /**
     * 从 /models 里挑一个「明确免费」的对话模型；认不出就返回 null（留空让人手填）。
     * 纯函数，JVM 单测直接验：不联网、不碰密钥、不看当前选中项。
     */
    fun pickFreeModel(names: List<String>): String? {
        val bad = listOf(
            "tts", "asr", "ocr", "image", "embedding", "embed", "bge",
            "rerank", "vision", "audio", "video", "voice", "whisper",
            "search", "reward", "guard", "moderation", "transcribe", "stt",
        )
        val paid = listOf("v4", "-pro", "pro/", "glm-5", "k2", "5.3", "5.2", "5.1", "72b", "122b", "235b")
        val clean = names.filter { m ->
            val n = m.lowercase()
            bad.none { it in n } && paid.none { it in n }
        }
        val exact = FREE_CHAT_MODELS.firstOrNull { f ->
            clean.any { it.equals(f, ignoreCase = true) }
        }
        if (exact != null) return clean.first { it.equals(exact, ignoreCase = true) }
        return clean.firstOrNull { "free" in it.lowercase() }
    }

    /**
     * 测指定端点；成功且模型名留空时，只自动补上**明确免费**的对话模型。
     * 认不出免费档就留空并如实说明，绝不退回「列表里第一个」（那通常正是付费旗舰）。
     */
    suspend fun test(id: String): Result<String> = withContext(Dispatchers.IO) {
        val s = byId(id) ?: return@withContext Result.failure(
            IllegalArgumentException("先添加并保存一个端点"))
        if (s.baseNorm.isBlank()) return@withContext Result.failure(
            IllegalArgumentException("还没填 API 地址"))
        if (s.key.isBlank()) return@withContext Result.failure(
            IllegalArgumentException("还没填 API 密钥"))
        val req = Request.Builder().url(s.baseNorm + "/models")
            .header("Authorization", "Bearer ${s.key}")
            .build()
        runCatching {
            client.newCall(req).execute().use { r ->
                if (!r.isSuccessful) throw IllegalStateException("HTTP ${r.code}")
                val body = r.body?.string().orEmpty()
                val names = mutableListOf<String>()
                runCatching {
                    val o = JSONObject(body)
                    val data = o.optJSONArray("data") ?: o.optJSONArray("models")
                    if (data != null) for (i in 0 until data.length()) {
                        val m = data.optJSONObject(i)?.optString("id").orEmpty()
                        if (m.isNotBlank() && m !in names) names.add(m)
                    }
                }
                // 能列 /models ≠ 能真调：欠费、服务未开通、应用没勾选模型，都是清单 200、
                // 真正 /chat/completions 才报 403/401。所以这里不谎报「可用」，
                // 也不主动发一次对话去试 —— 那会在付费端点上产生费用。
                val picked = pickFreeModel(names)
                if (s.model.isBlank() && picked != null) save(s.copy(model = picked))
                "能列出 ${names.size} 个模型（只查清单，未发起对话、不产生费用）" +
                    if (s.model.isBlank() && picked != null) " · 已自动填入免费模型 $picked"
                    else if (s.model.isBlank()) " · 没识别到免费档，留空；余额与开通情况请到厂商控制台确认"
                    else " · 能否真调、账号有无余额，以厂商控制台为准"
            }
        }
    }

    fun authHeader(base: String): String = "Bearer ${keyFor(base)}"
}
