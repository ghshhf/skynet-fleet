package app.skynet.tianwang.next

/**
 * 16-bit PCM 的 WAV 封装。
 *
 * 后端 whisper 不接受任意采样率；老工程只录 16kHz 整数倍，这里固定 16k 单声道，
 * 免得手机端和 PC 端各写一套头、各猜一个格式。纯字节逻辑，JVM 可测。
 */
object WavWriter {
    fun header(
        dataBytes: Int,
        sampleRate: Int = 16000,
        channels: Int = 1,
        bitsPerSample: Int = 16,
    ): ByteArray {
        val byteRate = sampleRate * channels * bitsPerSample / 8
        val blockAlign = channels * bitsPerSample / 8
        val out = ByteArray(44)
        fun putInt(offset: Int, v: Int) {
            out[offset] = (v and 0xff).toByte()
            out[offset + 1] = ((v shr 8) and 0xff).toByte()
            out[offset + 2] = ((v shr 16) and 0xff).toByte()
            out[offset + 3] = ((v shr 24) and 0xff).toByte()
        }
        fun putShort(offset: Int, v: Int) {
            out[offset] = (v and 0xff).toByte()
            out[offset + 1] = ((v shr 8) and 0xff).toByte()
        }
        // RIFF
        out[0] = 'R'.code.toByte(); out[1] = 'I'.code.toByte()
        out[2] = 'F'.code.toByte(); out[3] = 'F'.code.toByte()
        putInt(4, 36 + dataBytes)
        out[8] = 'W'.code.toByte(); out[9] = 'A'.code.toByte()
        out[10] = 'V'.code.toByte(); out[11] = 'E'.code.toByte()
        // fmt 
        out[12] = 'f'.code.toByte(); out[13] = 'm'.code.toByte()
        out[14] = 't'.code.toByte(); out[15] = ' '.code.toByte()
        putInt(16, 16)
        putShort(20, 1)                 // PCM
        putShort(22, channels)
        putInt(24, sampleRate)
        putInt(28, byteRate)
        putShort(32, blockAlign)
        putShort(34, bitsPerSample)
        // data
        out[36] = 'd'.code.toByte(); out[37] = 'a'.code.toByte()
        out[38] = 't'.code.toByte(); out[39] = 'a'.code.toByte()
        putInt(40, dataBytes)
        return out
    }
}
