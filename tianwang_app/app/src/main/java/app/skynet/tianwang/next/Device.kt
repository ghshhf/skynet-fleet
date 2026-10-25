package app.skynet.tianwang.next

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import android.util.Log
import java.io.File

/**
 * 设备档案 + 推理参数自适应。
 *
 * 🔴 为什么要有这一层（2026-10-25 主人定的一条硬原则）：
 *   「但凡写死就是完全的逻辑错误，因为任何一个项目不可能只有一个设备。」
 *
 *   这份代码迟早会装到别人的手机上。凡是被写死的数字，换一台机器就是错的：
 *     threads=6  → 八核机上浪费两核，四核机上直接超订；
 *     ctx=8192   → 8GB 机上可能 OOM，16GB 机上白白浪费；
 *     ngl=0      → 有 GPU 的机器永远用不上，等于把加速能力焊死。
 *   所以这里一个设备常量都不留：核数读 sysfs、内存问系统、
 *   GPU 能力问 native、上下文按剩余内存反推。换机只换探测结果，不换代码。
 */

/** 推理模式 —— 用户的意图，不是实现细节。键名进 SharedPreferences，别改。 */
enum class InferenceMode(val key: String, val label: String, val desc: String) {
    AUTO("auto", "自适应", "按这台机器的实测能力自己挑，换机不用改设置"),
    CPU("cpu", "纯 CPU", "最省电最兼容，任何机器都跑得动"),
    GPU("gpu", "纯 GPU", "全部层交给 GPU，最快，但吃驱动兼容性"),
    HYBRID("hybrid", "混合", "一部分层给 GPU、其余留 CPU，显存不够时的折中");

    companion object {
        fun of(key: String?): InferenceMode = entries.firstOrNull { it.key == key } ?: AUTO
    }
}

/**
 * 回复语言。第一项是**默认**（简体中文）。
 *
 * 🔴 为什么要有这一层（2026-10-26 主人定）：这台手机是生活场景用的。
 * 小模型（0.6B 这类）一旦没人约束，很容易顺着训练语料冒英文；
 * 而主人明确指出「英文看不懂，手机上还要多开个翻译软件」。
 * 所以语言是**默认值**而不是每次提问里的手工后缀 —— 设一次，之后每轮都生效。
 *
 * [instruct] 是真正塞进 system 提示的那句话；[AUTO] 为空表示不加任何约束。
 */
enum class ReplyLang(val key: String, val label: String, val instruct: String) {
    ZH_CN("zh-CN", "简体中文", "始终使用简体中文回答。即使用户用其他语言提问，也用简体中文回复。"),
    ZH_TW("zh-TW", "繁体中文", "始終使用繁體中文回答，用語以台灣習慣為準。"),
    EN("en", "English", "Always answer in English, even if the question is in another language."),
    AUTO("auto", "跟随提问", "");

    companion object {
        fun of(key: String?): ReplyLang = entries.firstOrNull { it.key == key } ?: ZH_CN
    }
}

/**
 * 设备档案。每个字段都来自运行时探测。
 *
 * [perfCores] 是「性能核数量」而不是核数：ARM 的大小核调度里，
 * 把线程数给成逻辑核总数会让小核也参与矩阵乘 —— 小核频率低、缓存小，
 * 反而拖慢整条流水线（llama.cpp 社区一致结论：按物理性能核给）。
 */
data class DeviceProfile(
    val model: String,
    val soc: String,
    val abi: String,
    val totalCores: Int,
    val perfCores: Int,
    val bigFreqMhz: Int,
    val totalRamMb: Long,
    val availRamMb: Long,
    val lowRamDevice: Boolean,
    /** native 侧探测到的 GPU offload 设备数（0 = 本构建没有 GPU 后端）。 */
    val gpuDevices: Int,
    val gpuName: String,
) {
    val ramGb: Double get() = totalRamMb / 1024.0
    val hasGpu: Boolean get() = gpuDevices > 0

    fun summary(): String = buildString {
        append("$model（$soc）· ${totalCores}核/性能核$perfCores · ")
        append(String.format("%.1f", ramGb)).append("G/可用${availRamMb}M")
        if (hasGpu) append(" · GPU:$gpuName") else append(" · 无GPU后端")
    }
}

/**
 * 一次加载的参数计划。带 [reason]，因为「为什么给我这个数」必须能被追问 ——
 * 换机出问题时，用户看到的是理由而不是一串黑箱数字。
 */
data class InferencePlan(
    val mode: InferenceMode,
    val threads: Int,
    val ctx: Int,
    /** 0 = 纯 CPU；100 = 全部层；1..99 = 按层数比例。层数本身从模型读，不写死。 */
    val gpuPercent: Int,
    val reason: String,
    /**
     * 这台机器的剩余内存够不够喂饱这个模型。
     * false 不代表加载失败 —— 而是「能加载，但会一直缺页读闪存，慢到没法用」。
     * 2026-10-25 真机实测：4B 在可用 3.4G 的机器上首字 20 秒，就是这种情况。
     */
    val fits: Boolean = true,
    val needMb: Long = 0,
) {
    /** 给用户看的一句话结论。 */
    val verdict: String
        get() = if (fits) "这台机器带得动"
                else "内存不够：约需 ${needMb}MB，可用的不够 —— 会慢到没法用，换小模型"
}

object Dev {
    private const val TAG = "DeviceProfile"

    @Volatile
    private var cached: DeviceProfile? = null    /** 探测一次就缓存：这些值在进程生命周期内不变，重复问系统只是白烧 IO。 */
    fun profile(ctx: Context): DeviceProfile {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            val p = probe(ctx.applicationContext)
            cached = p
            Log.i(TAG, p.summary())
            return p
        }
    }

    private fun probe(ctx: Context): DeviceProfile {
        val cores = Runtime.getRuntime().availableProcessors()
        val (perf, maxFreq) = perfCoreTopology(cores)

        val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mi = ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }

        // GPU 能力问 native：本构建有没有编进 GPU 后端、认得出几个设备，只有它知道。
        // 问不到就当没有 —— 不猜、不预设「手机一定有 GPU」。
        var gpuDevices = 0
        var gpuName = ""
        if (LlamaNative.ensureLoaded()) {
            runCatching { LlamaNative.gpuInfo() }
                .onSuccess { (n, name) -> gpuDevices = n; gpuName = name }
                .onFailure { Log.w(TAG, "gpuInfo 失败: ${it.message}") }
        }

        return DeviceProfile(
            model = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
            soc = socName(),
            abi = Build.SUPPORTED_ABIS.firstOrNull() ?: "?",
            totalCores = cores,
            perfCores = perf,
            bigFreqMhz = maxFreq,
            totalRamMb = mi.totalMem / (1024 * 1024),
            availRamMb = mi.availMem / (1024 * 1024),
            lowRamDevice = am.isLowRamDevice,
            gpuDevices = gpuDevices,
            gpuName = gpuName,
        )
    }

    private fun socName(): String = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !Build.SOC_MODEL.isNullOrBlank() ->
            Build.SOC_MODEL
        !Build.HARDWARE.isNullOrBlank() -> Build.HARDWARE
        else -> "unknown"
    }

    /**
     * 从小核/大核的频率分布里数出「性能核」。
     *
     * 不写死任何核心数量：直接读每个核能跑到的最高频率，取最高频那一档附近
     * （≥ 最大值 75%）的核数。天玑 8300 是 1+3+4、骁龙 8 Gen 是 1+5+2、
     * 老机器可能 4 核或 8 核同频 —— 这套算法对它们一视同仁。
     * sysfs 读不到（部分内核裁剪了 cpufreq）就退回「逻辑核 × 0.7」的保守估计。
     */
    private fun perfCoreTopology(cores: Int): Pair<Int, Int> {
        val freqs = (0 until cores).mapNotNull { i ->
            runCatching {
                File("/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq")
                    .readText().trim().toInt() / 1000        // kHz → MHz
            }.getOrNull()
        }
        if (freqs.isEmpty()) {
            // 读不到频率：既不敢给满核，也不敢只给两三核
            val est = (cores * 0.7).toInt().coerceAtLeast(1)
            return est to 0
        }
        val max = freqs.max()
        val perf = freqs.count { it >= max * 0.75 }.coerceAtLeast(1)
        return perf to max
    }

    /**
     * 算参数。三件事全部由「这台机器 + 这个模型」推出来：
     *
     * 1) threads —— 性能核数。ARM 没有超线程，性能核数就是并行度的上限。
     *    再夹一道 [1, cores] 的闸，防 sysfs 数据异常给出超核的数。
     *
     * 2) ctx —— 由剩余内存反推。KV cache 按 token 线性增长，每 token 的
     *    KV 开销与模型规模成正比（层数 × KV 头数 × 头维），用「模型文件大小」
     *    当代理量：Q4_K_M 下 1.7B 的 KV@8K≈0.9G、4B≈1.7G，比例都在 9%~11%。
     *    所以 kvPerTokenMb ≈ modelMb × 0.1 / 8192。留出模型本身 + 计算缓冲 +
     *    系统安全垫之后，剩下的才是能给上下文的。
     *
     * 3) gpuPercent —— 模式直接决定，但 AUTO 要看 GPU 后端存不存在：
     *    没有 GPU 后端的构建里把层推给 GPU 是无效操作，必须老实走 CPU。
     */
    fun plan(ctx: Context, modelFileMb: Long, mode: InferenceMode, model: ModelProfile): InferencePlan {
        val d = profile(ctx)

        val threads = d.perfCores.coerceIn(1, d.totalCores)

        // ---- ctx：剩余内存 → 能塞多少 token ----
        val modelMb = if (modelFileMb > 0) modelFileMb else 1024L
        val kvPerTokenMb = modelMb * 0.1 / 8192.0
        val reserve = maxOf((d.totalRamMb * 0.12).toLong(), 768L)   // 系统安全垫
        val computeBuf = (modelMb * 0.35).toLong()                   // 计算图/临时张量
        val forKv = d.availRamMb - modelMb - reserve - computeBuf
        val rawCtx = if (kvPerTokenMb > 0) (forKv / kvPerTokenMb).toInt() else 2048
        // 向下取 1024 的倍数；上界跟随模型自身训练长度（model.ctx 来自 GGUF/profile）
        val maxByModel = if (model.ctx > 0) model.ctx else 32768
        val ctxSize = (rawCtx.coerceIn(2048, maxByModel) / 1024) * 1024

        // ---- gpuPercent：模式 + 后端可用性 ----
        val (gpuPercent, gpuWhy) = when (mode) {
            InferenceMode.CPU -> 0 to "指定纯 CPU"
            InferenceMode.GPU ->
                if (d.hasGpu) 100 to "指定 GPU 全量"
                else 0 to "指定 GPU 但本构建没有 GPU 后端，回落 CPU"
            InferenceMode.HYBRID ->
                if (d.hasGpu) 60 to "指定混合，GPU 承担六成层"
                else 0 to "指定混合但无 GPU 后端，回落 CPU"
            InferenceMode.AUTO ->
                if (d.hasGpu && !d.lowRamDevice && d.availRamMb > modelMb * 2)
                    100 to "自适应：有 GPU 且内存宽裕 → 全量"
                else if (d.hasGpu && d.availRamMb > modelMb * 1.3)
                    60 to "自适应：有 GPU 但内存偏紧 → 混合"
                else if (d.hasGpu)
                    0 to "自适应：内存不足以给 GPU 留缓冲 → 纯 CPU"
                else
                    0 to "自适应：本构建无 GPU 后端 → 纯 CPU"
        }

        val reason = buildString {
            append(gpuWhy)
            append(" · 线程 $threads（性能核 ${d.perfCores}/共 ${d.totalCores}）")
            append(" · ctx $ctxSize（可用 ${d.availRamMb}M，模型 ${modelMb}M）")
        }

        // 「跑不动」的判据：模型 + KV + 计算缓冲 超过可用内存的九成，
        // 就必然频繁缺页 —— 表现为首字几十秒，而不是报错。宁可在 UI 上标红，
        // 也不要让用户对着一个转圈等了半分钟才知道。
        val kvMb = (ctxSize * kvPerTokenMb).toLong()
        val needMb = modelMb + kvMb + computeBuf
        val fits = needMb <= (d.availRamMb * 9 / 10)

        return InferencePlan(mode, threads, ctxSize, gpuPercent, reason, fits, needMb)
    }
}

/** 全局拿得到 applicationContext，省得每个 object 都要一个 init(ctx)。 */
object App {
    @Volatile
    private var app: Context? = null

    fun bind(c: Context) { app = c.applicationContext }

    val ctx: Context
        get() = app ?: error("App.ctx 未绑定：MainActivity.onCreate 里要先 App.bind(this)")
}

/**
 * 用户偏好（推理模式、思考开关等）。全部持久化 ——
 * 开关只在内存里活一次，等于每次开 app 都要重设，那不叫开关。
 */
object Prefs {
    private const val SP = "tianwang_prefs"
    private const val K_MODE = "infer_mode"
    private const val K_NOTHINK = "no_think"
    private const val K_MAXTOK = "max_tokens"
    private const val K_LAST_MODEL = "last_model_id"
    private const val K_LANG = "reply_lang"

    private val sp get() = App.ctx.getSharedPreferences(SP, Context.MODE_PRIVATE)

    var mode: InferenceMode
        get() = InferenceMode.of(sp.getString(K_MODE, InferenceMode.AUTO.key))
        set(v) { sp.edit().putString(K_MODE, v.key).apply() }

    /** 上次选中的模型。换机后这个 id 对不上就自然回落到「最小那个」。 */
    var lastModelId: String
        get() = sp.getString(K_LAST_MODEL, "") ?: ""
        set(v) { sp.edit().putString(K_LAST_MODEL, v).apply() }

    /** 抑制思考链（只对混合思考模型有意义）。默认开：手机上等 30 秒思考没意义。 */
    var noThink: Boolean
        get() = sp.getBoolean(K_NOTHINK, true)
        set(v) { sp.edit().putBoolean(K_NOTHINK, v).apply() }

    var maxTokens: Int
        get() = sp.getInt(K_MAXTOK, 512)
        set(v) { sp.edit().putInt(K_MAXTOK, v.coerceIn(64, 8192)).apply() }

    /**
     * 回复语言。默认**简体中文**（2026-10-26 主人定）：
     * 这手机是生活用的，不是英文练习机 —— 小模型一旦自由发挥就容易冒英文，
     * 与其每次在提问里手动写"用中文回答"，不如一次性钉死成默认还要能改。
     */
    var replyLang: ReplyLang
        get() = ReplyLang.of(sp.getString(K_LANG, ReplyLang.ZH_CN.key))
        set(v) { sp.edit().putString(K_LANG, v.key).apply() }
}
