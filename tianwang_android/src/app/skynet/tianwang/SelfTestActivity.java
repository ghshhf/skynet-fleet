package app.skynet.tianwang;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;

import java.io.DataInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * 本地语音自检页：拿一段 wav 喂给手机上的 whisper，把结果打到日志和文件。
 *
 * 为什么要有这个页：本地识别出问题时，「是 .so 没加载、模型没读到、还是音频格式不对」
 * 这三件事在 UI 上长得一模一样，全是"没出字"。这里把每一步都单独报出来，
 * 现场排查不用再改代码重装。
 *
 * 用法（wav 必须是 16k / 单声道 / 16bit）：
 *   adb push test.wav /sdcard/Android/data/app.skynet.tianwang/files/
 *   adb shell am start -n app.skynet.tianwang/.SelfTestActivity \
 *       --es wav /sdcard/Android/data/app.skynet.tianwang/files/test.wav
 *   adb logcat -s fleet-whisper
 */
public class SelfTestActivity extends Activity {

    private static final String TAG = "fleet-whisper";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        String wav = getIntent() == null ? null : getIntent().getStringExtra("wav");
        new Thread(() -> run(wav), "whisper-selftest").start();
    }

    private void run(String wav) {
        StringBuilder rep = new StringBuilder();
        try {
            rep.append("so 版本 ").append(Whisper.version()).append('\n');

            String model = Whisper.defaultModelPath(this);
            File mf = new File(model);
            rep.append("模型 ").append(model).append("  ")
               .append(mf.isFile() ? (mf.length() / 1048576 + "MB") : "缺失").append('\n');
            if (!Whisper.looksLikeGgml(mf)) {
                rep.append("模型不是 ggml 格式或没下完，停在这里\n");
                finishWith(rep);
                return;
            }

            Whisper w = new Whisper();
            if (!w.load(model)) {
                rep.append("加载失败：").append(w.lastError).append('\n');
                finishWith(rep);
                return;
            }
            rep.append("加载耗时 ").append(w.loadMs).append("ms\n");

            if (wav == null || wav.isEmpty()) {
                rep.append("没给 wav，只验了加载这一步\n");
                w.close();
                finishWith(rep);
                return;
            }

            short[] pcm = readWav16kMono(new File(wav));
            rep.append("音频 ").append(pcm.length).append(" 样本 = ")
               .append(String.format("%.2f", pcm.length / 16000.0)).append(" 秒\n");
            if (pcm.length == 0) {
                rep.append("wav 读不出样本（要求 16k/单声道/16bit PCM）\n");
                w.close();
                finishWith(rep);
                return;
            }

            String text = w.transcribe(pcm, 16000, "zh");
            rep.append("识别耗时 ").append(w.lastMs).append("ms\n");
            rep.append("结果：").append(text.isEmpty() ? "（空）" + w.lastError : text).append('\n');
            w.close();
        } catch (Throwable t) {
            rep.append("异常：").append(Log.getStackTraceString(t)).append('\n');
        }
        finishWith(rep);
    }

    /** 落一份到文件，方便 adb pull 回来慢慢看；同时打日志。 */
    private void finishWith(CharSequence report) {
        String s = report.toString();
        for (String line : s.split("\n")) Log.i(TAG, "自检 | " + line);
        try {
            File out = new File(getExternalFilesDir(null), "selftest.txt");
            try (java.io.FileOutputStream fo = new java.io.FileOutputStream(out)) {
                fo.write(s.getBytes("UTF-8"));
            }
            Log.i(TAG, "自检报告已写到 " + out);
        } catch (IOException e) {
            Log.w(TAG, "写报告失败：" + e);
        }
        runOnUiThread(this::finish);
    }

    /**
     * 只接受 16k / 单声道 / 16bit PCM。
     *
     * 不做重采样：一旦采样率不对，宁可报错也不要"凑合转一下"——
     * 变速变调的音频喂给 whisper 会得到通顺但完全不对的句子，那种错最难查。
     */
    static short[] readWav16kMono(File f) throws IOException {
        if (!f.isFile() || f.length() < 44) return new short[0];
        byte[] all = new byte[(int) f.length()];
        try (DataInputStream in = new DataInputStream(new FileInputStream(f))) {
            in.readFully(all);
        }
        ByteBuffer bb = ByteBuffer.wrap(all).order(ByteOrder.LITTLE_ENDIAN);
        if (bb.getInt(0) != 0x46464952) return new short[0];     // "RIFF"

        int rate = 0, channels = 0;
        int i = 12;
        while (i + 8 <= all.length) {
            int id = bb.getInt(i);
            int size = bb.getInt(i + 4);
            int body = i + 8;
            if (id == 0x20746d66) {                               // "fmt "
                if (size < 16) return new short[0];
                channels = bb.getShort(body + 2) & 0xffff;
                rate = bb.getInt(body + 4);
                int bits = bb.getShort(body + 14) & 0xffff;
                if (channels != 1 || rate != 16000 || bits != 16) {
                    Log.w(TAG, "wav 格式不接受: ch=" + channels + " rate=" + rate + " bits=" + bits);
                    return new short[0];
                }
            } else if (id == 0x61746164) {                        // "data"
                int n = Math.min(size, all.length - body) / 2;
                short[] out = new short[n];
                for (int k = 0; k < n; k++) out[k] = bb.getShort(body + k * 2);
                return out;
            }
            i = body + size + (size & 1);
        }
        return new short[0];
    }
}
