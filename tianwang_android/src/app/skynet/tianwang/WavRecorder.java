package app.skynet.tianwang;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

import java.io.ByteArrayOutputStream;
import java.io.IOException;

/**
 * 录音 → 16k 单声道 16bit WAV。
 *
 * 为什么要自己写 WAV 头：MediaRecorder 只会输出 m4a/webm，后端 whisper 只吃 wav，
 * 非 RIFF 还得让电脑起 ffmpeg 转一道。手机端直接把 PCM 拼成 wav，
 * 后端那条 `raw[:4] != b"RIFF"` 的分支就永远走不到——少一次转码就少一次失败点。
 *
 * 采样率只接受 16000 的整数倍（16k/48k/32k），这样降采样就是整数抽取，
 * 不用引入重采样滤波器；44.1k 这类非整数倍直接跳过，宁可报错也不给
 * whisper 喂变调的音频。
 */
public class WavRecorder {

    public static final int TARGET_RATE = 16000;

    /** 音量回调，0..100，给光球用。 */
    public interface LevelListener {
        void onLevel(int level);
    }

    private AudioRecord rec;
    private Thread worker;
    private volatile boolean running;
    private ByteArrayOutputStream pcm;
    private int srcRate = TARGET_RATE;
    private int decim = 1;
    private LevelListener listener;

    public void setLevelListener(LevelListener l) {
        listener = l;
    }

    public boolean isRecording() {
        return running;
    }

    /** 开录。失败直接抛，让调用方把原因显示出来——不静默降级。 */
    public synchronized void start() throws IOException {
        if (running) { return; }

        AudioRecord chosen = null;
        int pickedRate = 0;
        int[] candidates = {16000, 48000, 32000};
        StringBuilder why = new StringBuilder();
        for (int rate : candidates) {
            int min = AudioRecord.getMinBufferSize(
                    rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (min <= 0) {
                why.append(rate).append(":缓冲不可用; ");
                continue;
            }
            AudioRecord cand = null;
            try {
                cand = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        Math.max(min * 2, rate / 2));
                if (cand.getState() == AudioRecord.STATE_INITIALIZED) {
                    chosen = cand;
                    pickedRate = rate;
                    break;
                }
                why.append(rate).append(":初始化失败(state=").append(cand.getState()).append("); ");
            } catch (Exception e) {
                why.append(rate).append(':').append(e.getClass().getSimpleName()).append("; ");
            } finally {
                if (chosen == null && cand != null) {
                    try { cand.release(); } catch (Exception ignored) { }
                }
            }
        }
        if (chosen == null) {
            throw new IOException("没有可用录音配置（" + why + "）");
        }

        rec = chosen;
        srcRate = pickedRate;
        decim = Math.max(1, srcRate / TARGET_RATE);
        pcm = new ByteArrayOutputStream(srcRate * 2 * 30);
        running = true;
        try {
            rec.startRecording();
        } catch (Exception e) {
            running = false;
            throw new IOException("startRecording 失败：" + e.getMessage());
        }

        worker = new Thread(new Runnable() {
            @Override
            public void run() {
                loop();
            }
        }, "tianwang-rec");
        worker.setDaemon(true);
        worker.start();
    }

    private void loop() {
        // 一次读 40ms，既不至于太空转，也不会让光球跳得像心电图
        int frames = Math.max(320, srcRate / 25);
        short[] buf = new short[frames];
        short[] out = new short[Math.max(1, frames / decim)];
        while (running) {
            int n;
            try {
                n = rec.read(buf, 0, buf.length);
            } catch (Exception e) {
                break;
            }
            if (n <= 0) {
                if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE) {
                    break;
                }
                continue;
            }

            int peak = 0;
            int oi = 0;
            for (int i = 0; i < n; i += decim) {
                short s = buf[i];
                int a = s < 0 ? -s : s;
                if (a > peak) { peak = a; }
                out[oi++] = s;
            }
            byte[] bytes = new byte[oi * 2];
            for (int i = 0; i < oi; i++) {
                short s = out[i];
                bytes[i * 2] = (byte) (s & 0xFF);
                bytes[i * 2 + 1] = (byte) ((s >> 8) & 0xFF);
            }
            synchronized (this) {
                if (pcm != null) { pcm.write(bytes, 0, bytes.length); }
            }

            final int lv = Math.min(100, peak * 100 / 12000);
            if (listener != null) { listener.onLevel(lv); }
        }
    }

    /** 停录并返回完整 wav；没录到东西返回 null（调用方据此提示"没听见"）。 */
    public synchronized byte[] stop() {
        if (!running && pcm == null) { return null; }
        running = false;
        try {
            if (rec != null) {
                try { rec.stop(); } catch (Exception ignored) { }
                try { rec.release(); } catch (Exception ignored) { }
            }
        } finally {
            rec = null;
        }
        if (worker != null) {
            try { worker.join(600); } catch (InterruptedException ignored) { }
            worker = null;
        }
        byte[] raw;
        synchronized (this) {
            raw = pcm == null ? new byte[0] : pcm.toByteArray();
            pcm = null;
        }
        if (raw.length < TARGET_RATE / 2) {   // 不足 0.25 秒，当误触
            return null;
        }
        return wrapWav(raw);
    }

    /** 44 字节标准头 + PCM 数据。 */
    private static byte[] wrapWav(byte[] data) {
        int total = data.length + 44;
        byte[] w = new byte[total];
        writeAscii(w, 0, "RIFF");
        writeInt(w, 4, total - 8);
        writeAscii(w, 8, "WAVE");
        writeAscii(w, 12, "fmt ");
        writeInt(w, 16, 16);                       // PCM 子块大小
        writeShort(w, 20, 1);                      // PCM
        writeShort(w, 22, 1);                      // 单声道
        writeInt(w, 24, TARGET_RATE);
        writeInt(w, 28, TARGET_RATE * 2);          // 字节率 = rate * 声道 * 位宽/8
        writeShort(w, 32, 2);                      // 块对齐
        writeShort(w, 34, 16);                     // 位宽
        writeAscii(w, 36, "data");
        writeInt(w, 40, data.length);
        System.arraycopy(data, 0, w, 44, data.length);
        return w;
    }

    private static void writeAscii(byte[] b, int off, String s) {
        for (int i = 0; i < s.length(); i++) { b[off + i] = (byte) s.charAt(i); }
    }

    private static void writeInt(byte[] b, int off, int v) {
        b[off] = (byte) (v & 0xFF);
        b[off + 1] = (byte) ((v >> 8) & 0xFF);
        b[off + 2] = (byte) ((v >> 16) & 0xFF);
        b[off + 3] = (byte) ((v >> 24) & 0xFF);
    }

    private static void writeShort(byte[] b, int off, int v) {
        b[off] = (byte) (v & 0xFF);
        b[off + 1] = (byte) ((v >> 8) & 0xFF);
    }
}
