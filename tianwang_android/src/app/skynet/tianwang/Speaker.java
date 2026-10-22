package app.skynet.tianwang;

import android.content.Context;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import java.util.Locale;

/**
 * 系统语音（本地合成），替代「每句话都让电脑合成一遍再下载音频」。
 *
 * 为什么走系统 TTS：一句话的音频没必要每次从网络搬一次。手机里本来就有语音引擎
 * （小米/ColorOS 等自带，中文离线包可在「设置 → 文字转语音」里下载），本机几毫秒
 * 就能起播，断网也能念。这也是唯一不花流量的那条路。
 *
 * ⚠️ 边界说清楚：
 *   · 这里**只管念**，不管识别；识别走 Whisper。
 *   · 引擎可能没有中文离线包（部分 ROM 默认只有在线音色）。这种情况**不静默降级**：
 *     ready() 会把原因交出去，由调用方决定是提示用户装语音包、还是改走电脑合成。
 *   · 引擎的在线/离线不归我们控制。onInit 里只确认「这个语言能不能念」，
 *     真没离线包时会拿到 LANG_MISSING_DATA —— 那个原因要如实告诉人。
 */
public final class Speaker {

    /** 系统语音起不来时给的原因（ready() 为 false 时看这里）。 */
    public static final class NotReady {
        public final String why;
        NotReady(String why) { this.why = why; }
    }

    private TextToSpeech tts;
    private volatile boolean inited;
    private volatile String initErr;
    private volatile Locale locale;
    private Runnable onDone;
    private String pendingText;

    /** 起引擎并等它 init 完（阻塞最多 timeoutMs）。返回 null = 可用。 */
    public NotReady open(Context c, long timeoutMs) {
        final Object lock = new Object();
        tts = new TextToSpeech(c.getApplicationContext(), new TextToSpeech.OnInitListener() {
            @Override
            public void onInit(int status) {
                synchronized (lock) {
                    inited = true;
                    if (status != TextToSpeech.SUCCESS) {
                        initErr = "系统语音引擎初始化失败（status=" + status + "）";
                    }
                    lock.notifyAll();
                }
            }
        });
        synchronized (lock) {
            if (!inited) {
                try { lock.wait(timeoutMs); } catch (InterruptedException ignored) { }
            }
        }
        if (!inited) {
            close();
            return new NotReady("系统语音引擎 " + (timeoutMs / 1000) + " 秒内没起来"
                    + "（可能没装任何 TTS 引擎）");
        }
        if (initErr != null) {
            close();
            return new NotReady(initErr);
        }

        // 依次试：中文（中国）→ 任意中文 → 系统默认。都不行就如实报。
        Locale[] tries = {Locale.SIMPLIFIED_CHINESE, Locale.CHINESE, Locale.getDefault()};
        String lastWhy = "系统语音没有可用的中文音色";
        for (Locale l : tries) {
            int r = tts.isLanguageAvailable(l);
            if (r == TextToSpeech.LANG_AVAILABLE
                    || r == TextToSpeech.LANG_COUNTRY_AVAILABLE
                    || r == TextToSpeech.LANG_COUNTRY_VAR_AVAILABLE) {
                tts.setLanguage(l);
                locale = l;
                return null;
            }
            if (r == TextToSpeech.LANG_MISSING_DATA) {
                lastWhy = "系统语音缺中文离线数据：去「设置 → 文字转语音(TTS) → 安装语音数据」"
                        + "装一个中文包，再回来试";
            } else if (r == TextToSpeech.LANG_NOT_SUPPORTED) {
                lastWhy = "当前语音引擎不支持中文（" + l + "）；换一个引擎或装中文包";
            }
        }
        close();
        return new NotReady(lastWhy);
    }

    public String localeName() {
        return locale == null ? "" : locale.toString();
    }

    public String engineName() {
        return tts == null ? "" : String.valueOf(tts.getDefaultEngine());
    }

    /**
     * 念一句。onEnd 在念完（或被下一句顶掉）时回调一次。
     * 不排队：新的直接顶掉旧的——语音对话里「最新一句」才是要听的。
     */
    public void say(String text, final Runnable onEnd) {
        if (tts == null || text == null || text.isEmpty()) {
            if (onEnd != null) { onEnd.run(); }
            return;
        }
        onDone = onEnd;
        pendingText = text;
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { }
            @Override public void onDone(String id) { fire(); }
            @Override public void onError(String id) { fire(); }
            @Override public void onStop(String id, boolean interrupted) { fire(); }
            private void fire() {
                Runnable r = onDone;
                onDone = null;
                if (r != null) { r.run(); }
            }
        });
        Bundle p = new Bundle();
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, p, "tw" + System.nanoTime());
    }

    public void stop() {
        if (tts != null) { tts.stop(); }
    }

    public void close() {
        if (tts != null) {
            try { tts.stop(); tts.shutdown(); } catch (Exception ignored) { }
            tts = null;
        }
    }
}
