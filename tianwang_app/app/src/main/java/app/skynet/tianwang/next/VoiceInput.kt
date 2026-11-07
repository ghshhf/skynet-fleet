package app.skynet.tianwang.next

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.SystemClock
import java.io.ByteArrayOutputStream
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 16kHz 单声道 PCM16 录音，落成 WAV。
 *
 * 与老工程 `WavRecorder` 同一组参数（16k / 单声道 / 16bit）：
 * 后端 whisper 对采样率挑剔，格式错的表现是「识别出乱码」而不是报错。
 *
 * 录音线程只做读 PCM；停止时 join 再写文件，避免半截 WAV 被上传。
 */
object VoiceInput {
    private const val SAMPLE_RATE = 16000
    private const val MIN_MS = 500L

    @Volatile
    var recording: Boolean = false
        private set

    private var record: AudioRecord? = null
    private var thread: Thread? = null
    private var pcm: ByteArrayOutputStream? = null
    private var startedAt = 0L

    suspend fun start(): Result<Unit> = withContext(Dispatchers.IO) {
        if (recording) return@withContext Result.failure(IllegalStateException("已经在录音"))
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) {
            return@withContext Result.failure(IllegalStateException("设备不支持 16k 单声道录音"))
        }
        val r = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf * 2, 4096),
            )
        } catch (t: Throwable) {
            return@withContext Result.failure(t)
        }
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            r.release()
            return@withContext Result.failure(IllegalStateException("麦克风被占用或没有权限"))
        }
        try {
            r.startRecording()
        } catch (t: Throwable) {
            r.release()
            return@withContext Result.failure(t)
        }

        val out = ByteArrayOutputStream()
        pcm = out
        record = r
        startedAt = SystemClock.elapsedRealtime()
        recording = true
        thread = Thread {
            val buf = ByteArray(2048)
            try {
                while (recording) {
                    val n = r.read(buf, 0, buf.size)
                    if (n > 0) synchronized(out) { out.write(buf, 0, n) }
                }
            } catch (_: Throwable) {
                // 读失败（被抢占/设备拔出）时让 stop 拿到已录部分，不崩 UI。
            } finally {
                try { r.stop() } catch (_: Throwable) {}
                try { r.release() } catch (_: Throwable) {}
            }
        }.apply { name = "voice-input"; start() }
        Result.success(Unit)
    }

    /** 停止并写 WAV；太短返回 null（由界面给「再说一次」）。 */
    suspend fun stop(): File? = withContext(Dispatchers.IO) {
        if (!recording) return@withContext null
        recording = false
        val t = thread
        thread = null
        t?.join(2000)
        val buf = pcm
        val bytes = buf?.let { synchronized(it) { it.toByteArray() } }
        pcm = null
        record = null
        val ms = SystemClock.elapsedRealtime() - startedAt
        if (bytes == null || bytes.size < 1000 || ms < MIN_MS) return@withContext null

        val file = File(App.ctx.cacheDir, "voice-in.wav")
        try {
            file.outputStream().use { os ->
                os.write(WavWriter.header(bytes.size))
                os.write(bytes)
            }
            file
        } catch (t: Throwable) {
            file.delete()
            null
        }
    }

    suspend fun cancel() {
        if (!recording) return
        recording = false
        val t = thread
        thread = null
        withContext(Dispatchers.IO) { t?.join(1000) }
        pcm = null
        record = null
    }

    /** 离开页面时用：不等待、不落文件，只让录音线程尽快退出。 */
    fun abort() {
        recording = false
        pcm = null
        record = null
    }
}
