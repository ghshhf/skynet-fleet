package app.skynet.tianwang.next

import android.content.Context
import android.content.SharedPreferences
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.io.File
import java.util.Locale

/**
 * 出声。
 *
 * 到 2026-11-01 为止手机端**一个字都不会说**（总纲第 1 条空白）：
 * `TextToSpeech / MediaPlayer / AudioTrack` 一个都没有，她只能打字。
 *
 * ## 两条路，云在前
 *
 * **主路：云端合成 + 本地播放。** 走 `GET /api/tts` 从 PC 拿 wav，MediaPlayer 播。
 *
 * 为什么不是系统 TTS 打头 —— 真机实测（2026-11-01，Redmi K60 Ultra / HyperOS）：
 * `TextToSpeech` 初始化直接回 `-1`，`com.xiaomi.mibrain.speech` 状态正常
 * （stopped=false / hidden=false / enabled=0）、进程能拉起，但**连系统设置页
 * 自带的『播放』按钮都不产生任何音频轨道**（`dumpsys media.audio_flinger`
 * 的 active tracks 恒为 0，`dumpsys audio` 的 players 为空）。
 * 也就是说这台机器的系统语音引擎本身是废的，跟我们怎么写代码无关。
 * 所以嘴长在 PC 上（edge-tts 神经音色），手机只负责播。
 *
 * **兜底：系统 TTS。** 万一哪天换了台引擎正常的机器、又没连网关，
 * 还能出声。它挂了也不影响主路。
 *
 * ## 口径
 *   1. **默认关**。突然开口比不会说话更烦，得让人自己打开；
 *   2. **只念助手的最终回答**。不念用户输入、不念工具步骤、不念思维链 ——
 *      工具卡片是"给我看"的，不是"念给我听"的；
 *   3. **流式中不念**。等 onAnswer/onSuccess 拿到完整回答再念一次;
 *   4. **超长截断**。一口气念三分钟没人受得了。
 */
object Speech {
    private const val SP = "tianwang_agent"
    private const val K_ON = "speak_enabled"
    private const val TAG = "TianwangSpeech"

    /** 超过这么多字只念前面。约 20 秒语音，再长就该自己看。 */
    private const val MAX_CHARS = 120

    private var appCtx: Context? = null
    private var tts: TextToSpeech? = null
    private var ready = false
    private var langChecked = false
    private var zhOk = false
    private var sp: SharedPreferences? = null
    private var pending: String? = null
    private var lastErr = ""

    /** 云端合成要在 IO 上跑，播放要在主线程 —— 两条线程各管一段。 */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val main = Handler(Looper.getMainLooper())

    private var player: MediaPlayer? = null
    private var tmpWav: File? = null

    fun init(ctx: Context) {
        appCtx = ctx.applicationContext
        sp = ctx.applicationContext.getSharedPreferences(SP, Context.MODE_PRIVATE)
        if (tts != null) return
        // 系统 TTS 用 applicationContext 起得来就起，起不来不影响云端主路
        tts = TextToSpeech(ctx.applicationContext) { status ->
            ready = status == TextToSpeech.SUCCESS
            if (ready) ensureLang()
            else lastErr = "系统 TTS 初始化失败"
            Log.i(TAG, "init status=$status ready=$ready zhOk=$zhOk err=$lastErr")
            // 初始化是异步的，这期间攒下的那句在这里补念
            pending?.let { p ->
                pending = null
                if (ready && zhOk) systemSpeak(p)
            }
        }
    }

    /** 只有系统真的支持中文才算能用 —— 不支持时念出来是英文腔或者干脆没声。 */
    private fun ensureLang() {
        val t = tts ?: return
        val r = try {
            t.setLanguage(Locale.CHINA)
        } catch (e: Exception) {
            lastErr = "设置中文失败：${e.message}"
            return
        }
        zhOk = r != TextToSpeech.LANG_MISSING_DATA &&
            r != TextToSpeech.LANG_NOT_SUPPORTED
        langChecked = true
        if (!zhOk) lastErr = "系统没装中文语音引擎"
        Log.i(TAG, "lang=CHINA ret=$r zhOk=$zhOk")
    }

    fun enabled(): Boolean = sp?.getBoolean(K_ON, false) ?: false

    fun setEnabled(on: Boolean) {
        sp?.edit()?.putBoolean(K_ON, on)?.apply()
        if (!on) stop()
    }

    fun toggle(): Boolean {
        val next = !enabled()
        setEnabled(next)
        return next
    }

    /** 两条路至少有一条能走，才算"打开后真会响"。 */
    fun isUsable(): Boolean = Net.hasConfig() || (ready && zhOk)

    /** 给人看的一句话状态，别让 UI 自己猜。 */
    fun status(): String = when {
        !enabled() -> "关"
        Net.hasConfig() -> "开 · 云端合成"
        tts == null -> "未初始化"
        !ready -> lastErr.ifBlank { "系统引擎未就绪" }
        langChecked && !zhOk -> lastErr.ifBlank { "缺中文语音" }
        else -> "开 · 系统引擎"
    }

    /**
     * 念一段回答。开关关着、两条路都不通、文本为空 —— 全部静默跳过，绝不抛。
     * 念不出来最多是没声音，不能让整个对话因为 TTS 崩掉。
     */
    fun speak(text: String) {
        if (!enabled()) return
        val clean = clean(text)
        if (clean.isBlank()) return
        scope.launch {
            if (speakCloud(clean)) return@launch
            // 云端不通（没网关 / 断网 / PC 没开）：退回系统引擎
            main.post { systemSpeak(clean) }
        }
    }

    /**
     * 云端合成并播放。成功返回 true。
     *
     * 判据是"MediaPlayer 真的开始播"，不是"HTTP 回了 200" ——
     * 拿到 200 却播不出声（音频格式不对、焦点被抢）同样是失败。
     */
    private suspend fun speakCloud(text: String): Boolean {
        val bytes = Net.fetchSpeech(text) ?: return false
        if (bytes.size < 1000) {                       // 太短肯定不是有效 wav
            lastErr = "云端音频过短（${bytes.size}B）"
            Log.i(TAG, "cloud short bytes=${bytes.size}")
            return false
        }
        var ok = false
        // prepare() 会阻塞，不能在主线程；start() 反而无所谓线程
        ok = try {
            val ctx = appCtx ?: return false
            val f = File.createTempFile("fleet-tts", ".wav", ctx.cacheDir)
            f.writeBytes(bytes)
            stopPlayer()
            val mp = MediaPlayer()
            mp.setDataSource(f.absolutePath)
            mp.setOnCompletionListener {
                try { it.release() } catch (_: Exception) {}
                f.delete()
            }
            mp.prepare()
            mp.start()
            player = mp
            tmpWav = f
            true
        } catch (e: Exception) {
            lastErr = "播放失败：${e.javaClass.simpleName}: ${e.message}"
            Log.i(TAG, "cloud play FAIL ${e.javaClass.simpleName}: ${e.message}")
            false
        }
        Log.i(TAG, "cloud ok=$ok bytes=${bytes.size} chars=${text.length}")
        return ok
    }

    private fun stopPlayer() {
        try { player?.stop() } catch (_: Exception) {}
        try { player?.release() } catch (_: Exception) {}
        player = null
        tmpWav?.delete()
        tmpWav = null
    }

    /** 系统引擎那一路。要在主线程调。 */
    private fun systemSpeak(text: String) {
        if (!ready) { pending = text; return }
        if (!langChecked) ensureLang()
        if (!zhOk) return
        val t = tts ?: return
        t.stop()   // 新回答覆盖旧的，别排队念完上一条
        val rc = t.speak(text, TextToSpeech.QUEUE_FLUSH, null, "fleet-" + System.currentTimeMillis())
        // rc == SUCCESS 只说明"排进队列了"，不等于音箱真响过。
        Log.i(TAG, "system rc=$rc chars=${text.length}")
    }

    fun stop() {
        try { tts?.stop() } catch (_: Exception) {}
        stopPlayer()
    }

    fun shutdown() {
        stop()
        try { tts?.shutdown() } catch (_: Exception) {}
        tts = null
        ready = false
        langChecked = false
        zhOk = false
    }

    /**
     * 去掉 markdown 记号。不处理的话会把 `**` `#` `[文字](链接)` 原样念出来 ——
     * 那是给眼睛看的语法，不是给嘴念的。
     */
    private fun clean(raw: String): String {
        var s = raw
            .replace(Regex("```[\\s\\S]*?```"), "（代码略）")
            .replace(Regex("`([^`]*)`"), "\$1")
            .replace(Regex("!?\\[([^\\]]*)\\]\\([^)]*\\)"), "\$1")
            .replace(Regex("(?m)^\\s{0,3}#{1,6}\\s*"), "")
            .replace(Regex("[*_>|]+"), "")
            .replace(Regex("[ \\t]+"), " ")
            .replace(Regex("\\n{2,}"), "\n")
            .trim()
        if (s.length > MAX_CHARS) {
            s = s.take(MAX_CHARS).trimEnd() + "……"
        }
        return s
    }
}
