package app.skynet.tianwang;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.Charset;

/**
 * 后端客户端。只依赖 HttpURLConnection，不引 OkHttp/Retrofit——
 * 三个接口、几百行就够的事，多一个依赖就多一份 64K 方法数的压力。
 *
 * 错误一律把服务端 {"ok":false,"err":"..."} 里的 err 抛出来，
 * 不许只抛 "HTTP 500"，那种异常在手机上根本没法排查。
 */
public final class Net {

    private static final Charset UTF8 = Charset.forName("UTF-8");
    private static final int CONNECT_MS = 8000;

    public interface SseCallback {
        void onLine(String text);
        void onError(String message);
        void onDone();
    }

    private Net() { }

    // ---------------- 三趟：识别 / 对话 / 合成 ----------------

    /** 裸 wav 进 → {"ok":true,"text":"..."} 出 */
    public static String recognize(String base, String token, byte[] wav) throws IOException {
        HttpURLConnection c = open(base + "/api/voice/recognize", "POST", token, "audio/wav");
        c.setReadTimeout(180000);
        try {
            writeBody(c, wav);
            String body = readText(c);
            JSONObject o = parse(body);
            if (!o.optBoolean("ok", false)) {
                throw new IOException(o.optString("err", "识别失败（服务端没给原因）"));
            }
            return o.optString("text", "");
        } finally {
            c.disconnect();
        }
    }

    /** {prompt, backends} 进 → SSE 流出，边收边回调
     *
     * backends：逗号分隔，空串 = 后端自己的默认（全部）。手机语音一次只念一句，
     * 传一个具体的 id 才不会几个后端同屏串台。
     */
    public static void chat(String base, String token, String prompt, String backends,
                            SseCallback cb) {
        HttpURLConnection c = null;
        try {
            JSONObject req = new JSONObject();
            req.put("prompt", prompt);
            req.put("backends", backends == null ? "" : backends);
            c = open(base + "/api/chat", "POST", token, "application/json");
            c.setReadTimeout(300000);          // 后端自己 300s 超时，这边给同样的耐心
            writeBody(c, req.toString().getBytes(UTF8));

            int code = c.getResponseCode();
            if (code != 200) {
                String err = readText(c);
                cb.onError(friendly(code, err));
                return;
            }

            InputStream in = new BufferedInputStream(c.getInputStream());
            BufferedReader r = new BufferedReader(new InputStreamReader(in, UTF8));
            String line;
            String lastErr = null;
            boolean got = false;
            while ((line = r.readLine()) != null) {
                if (!line.startsWith("data:")) { continue; }
                String payload = line.substring(5).trim();
                if (payload.isEmpty()) { continue; }
                JSONObject ev;
                try {
                    ev = new JSONObject(payload);
                } catch (Exception e) {
                    continue;                   // 单帧坏了不整体失败
                }
                String type = ev.optString("type", "");
                if ("line".equals(type)) {
                    String t = ev.optString("text", "");
                    if (!t.isEmpty()) {
                        got = true;
                        cb.onLine(t);
                    }
                } else if ("error".equals(type)) {
                    lastErr = ev.optString("err", "后端报了错但没给原因");
                } else if ("done".equals(type)) {
                    break;
                }
            }
            r.close();
            if (lastErr != null && !got) {
                cb.onError(lastErr);
            } else if (lastErr != null) {
                cb.onError(lastErr);
            } else {
                cb.onDone();
            }
        } catch (IOException e) {
            cb.onError("连不上后端：" + e.getMessage());
        } catch (Exception e) {
            cb.onError(e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            if (c != null) { c.disconnect(); }
        }
    }

    /** 合成结果：字节 + 真实容器后缀。
     *
     * 为什么不假设是 wav：电脑端有 ffmpeg 时 edge_tts 的 mp3 会被转成 wav；
     * 冻扁包里没有外部 ffmpeg，拿到的就是 mp3。以前这里硬校验 RIFF 开头，
     * 换个后端就整条链路哑掉——格式由服务端的 X-Voice-Format 说了算。
     */
    public static final class SayResult {
        public final byte[] data;
        public final String ext;

        SayResult(byte[] data, String ext) {
            this.data = data;
            this.ext = ext;
        }
    }

    /** {text} 进 → 音频出（wav 或 mp3，看服务端能力） */
    public static SayResult say(String base, String token, String text) throws IOException {
        byte[] payload;
        try {
            JSONObject req = new JSONObject();
            req.put("text", text);
            payload = req.toString().getBytes(UTF8);
        } catch (org.json.JSONException e) {
            throw new IOException("拼请求失败：" + e.getMessage());
        }
        HttpURLConnection c = open(base + "/api/voice/say", "POST", token, "application/json");
        c.setReadTimeout(180000);
        try {
            writeBody(c, payload);
            int code = c.getResponseCode();
            if (code != 200) {
                throw new IOException(friendly(code, readText(c)));
            }
            String ctype = c.getHeaderField("Content-Type");
            String fmt = c.getHeaderField("X-Voice-Format");
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bo = new ByteArrayOutputStream(1 << 16);
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) { bo.write(buf, 0, n); }
            in.close();
            byte[] data = bo.toByteArray();
            String ext = guessExt(fmt, ctype, data);
            if (data.length < 1024) {
                throw new IOException("合成返回的音频太小（" + data.length + " 字节，格式 " + ext + "）");
            }
            return new SayResult(data, ext);
        } finally {
            c.disconnect();
        }
    }

    /** 服务端头优先；头缺失就按字节头嗅探，最后才退回 wav。 */
    private static String guessExt(String fmt, String ctype, byte[] d) {
        if (fmt != null && fmt.length() > 0) {
            String f = fmt.trim().toLowerCase();
            if (f.equals("mpeg") || f.equals("mp3")) { return "mp3"; }
            if (f.equals("wav") || f.equals("x-wav")) { return "wav"; }
            if (f.equals("ogg")) { return "ogg"; }
            if (f.equals("mp4") || f.equals("m4a")) { return "m4a"; }
        }
        if (ctype != null) {
            String t = ctype.toLowerCase();
            if (t.contains("mpeg")) { return "mp3"; }
            if (t.contains("ogg")) { return "ogg"; }
            if (t.contains("mp4")) { return "m4a"; }
        }
        if (d != null && d.length > 4) {
            if (d[0] == 'R' && d[1] == 'I' && d[2] == 'F' && d[3] == 'F') { return "wav"; }
            if (d[0] == 'O' && d[1] == 'g' && d[2] == 'g') { return "ogg"; }
            if (d[0] == 'I' && d[1] == 'D' && d[2] == '3') { return "mp3"; }
            if ((d[0] & 0xFF) == 0xFF && (d[1] & 0xE0) == 0xE0) { return "mp3"; }
        }
        return "wav";
    }

    // ---------------- 控电脑：看清单 / 让它做事 ----------------

    /** GET /api/caps → 电脑那头能做的全部事。原样 JSON 交给界面自己排。 */
    public static JSONObject caps(String base, String token) throws IOException {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/caps").openConnection();
            c.setRequestMethod("GET");
            c.setConnectTimeout(CONNECT_MS);
            c.setReadTimeout(20000);
            c.setRequestProperty("X-Fleet-Token", token == null ? "" : token);
            int code = c.getResponseCode();
            String body = readText(c);
            if (code != 200) { throw new IOException(friendly(code, body)); }
            return parse(body);
        } finally {
            if (c != null) { c.disconnect(); }
        }
    }

    /**
     * POST /api/call → 让电脑做一件事。
     *
     * ⚠️ 要审批的能力会**一直挂着**，直到电脑上的弹窗被点确认（或服务端超时）。
     * 所以调用方要给足读超时，界面也要先说一句「等电脑上确认」——不然人以为卡死了。
     */
    public static JSONObject call(String base, String token, String cap, JSONObject args)
            throws IOException {
        HttpURLConnection c = null;
        try {
            JSONObject req = new JSONObject();
            req.put("cap", cap);
            req.put("args", args == null ? new JSONObject() : args);
            c = open(base + "/api/call", "POST", token, "application/json");
            c.setReadTimeout(180000);
            writeBody(c, req.toString().getBytes(UTF8));
            int code = c.getResponseCode();
            String body = readText(c);
            if (code != 200) { throw new IOException(friendly(code, body)); }
            return parse(body);
        } catch (org.json.JSONException e) {
            throw new IOException("请求体拼不出来：" + e.getMessage());
        } finally {
            if (c != null) { c.disconnect(); }
        }
    }

    /** 探活：/api/health 不带鉴权，用来判断"电脑那头起了没"。 */
    public static String health(String base) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/health").openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(2500);
            c.setRequestMethod("GET");
            int code = c.getResponseCode();
            if (code != 200) { return "HTTP " + code; }
            String body = readText(c);
            JSONObject o = parse(body);
            return "ok:" + o.optString("persona", o.optString("phase", ""));
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) { c.disconnect(); }
        }
    }

    // ---------------- 底层 ----------------

    private static HttpURLConnection open(String url, String method, String token, String ctype)
            throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(CONNECT_MS);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", ctype);
        c.setRequestProperty("X-Fleet-Token", token == null ? "" : token);
        c.setRequestProperty("Cache-Control", "no-store");
        return c;
    }

    private static void writeBody(HttpURLConnection c, byte[] body) throws IOException {
        c.setFixedLengthStreamingMode(body.length);
        OutputStream out = c.getOutputStream();
        out.write(body);
        out.flush();
        out.close();
    }

    private static String readText(HttpURLConnection c) {
        InputStream in = null;
        try {
            in = c.getInputStream();
        } catch (IOException e) {
            in = c.getErrorStream();
        }
        if (in == null) { return ""; }
        try {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) { bo.write(buf, 0, n); }
            return bo.toString("UTF-8");
        } catch (IOException e) {
            return "";
        } finally {
            try { in.close(); } catch (IOException ignored) { }
        }
    }

    private static JSONObject parse(String body) throws IOException {
        try {
            return new JSONObject(body == null ? "{}" : body);
        } catch (Exception e) {
            throw new IOException("服务端返回的不是 JSON：" + snippet(body));
        }
    }

    /** 把 HTTP 状态码 + 服务端 err 拼成一句人话。 */
    private static String friendly(int code, String body) {
        String why = null;
        try {
            why = new JSONObject(body == null ? "{}" : body).optString("err", null);
        } catch (Exception ignored) { }
        if (why == null || why.isEmpty()) { why = snippet(body); }
        if (code == 401) {
            return "没带令牌（401）——去「设置」填 FLEET_GUI_TOKEN";
        }
        if (code == 403) {
            return "令牌不对（403）——电脑重启后令牌会变，重新抄一枚";
        }
        return "HTTP " + code + "：" + why;
    }

    private static String snippet(String s) {
        if (s == null) { return "无响应体"; }
        String t = s.trim();
        return t.length() > 160 ? t.substring(0, 160) + "…" : (t.isEmpty() ? "无响应体" : t);
    }
}
