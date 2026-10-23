package app.skynet.tianwang;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 天网 · 模型下载器（App 内本地下载，不靠 adb 推、不搭镜像）。
 *
 * 事实链（2026-10-23 推翻旧「不做下载器」误判后落地）：
 *   1. 主人手机自带代理、能直连 Hugging Face，所以点「其他」＝手机自己从 HF 直下。
 *   2. 移动网络易断 ⇒ 断点续传（Range 头）+ 失败自动重试（最多 3 次，从已下字节续）。
 *   3. 下完比对预期体积 / Content-Length，残缺不谎称成功。
 *   4. 落到 models 外部私有目录，写完 setReadable/setExecutable，不再依赖我 adb chmod。
 *
 * 网络：走系统默认代理（HttpURLConnection 自动读手机代理设置），HF 在国内经代理直连无碍。
 * 不引 OkHttp，沿用 Net 的「不堆依赖」原则。
 */
public final class ModelDownloader {

    private static final String TAG = "fleet-dl";
    private static final int CONNECT_MS = 15000;
    private static final int READ_MS = 60000;     // 单段读超时：超了当断线，进重试/续传
    private static final int MAX_ATTEMPTS = 3;

    private static final ExecutorService pool = Executors.newFixedThreadPool(2);

    public interface Progress {
        void onStart(long total);
        void onProgress(long done, long total, float speedBps);
        void onDone(File file);
        void onError(String msg);
    }

    private ModelDownloader() {}

    /** 按已知 Spec 下载（预期体积来自 spec.sizeBytes，0＝未知，只靠 Content-Length）。 */
    public static void enqueue(Context ctx, ModelManager.Spec spec, Progress cb) {
        enqueue(ctx, spec.url, spec.file, spec.kind, spec.sizeBytes, cb);
    }

    /** 自定义 Hugging Face 直链：从 URL 抽文件名，localName 可空（自动抽）。 */
    public static void enqueueCustom(Context ctx, String url, String localName,
                                     String kind, long expectedBytes, Progress cb) {
        if (localName == null || localName.trim().isEmpty()) {
            localName = filenameFromUrl(url);
        }
        enqueue(ctx, url, localName.trim(), kind, expectedBytes, cb);
    }

    /** 从 HF 直链抽文件名（剥查询串/片段，取最后一段路径）。 */
    public static String filenameFromUrl(String url) {
        String u = url;
        int q = u.indexOf('?'); if (q >= 0) u = u.substring(0, q);
        int h = u.indexOf('#'); if (h >= 0) u = u.substring(0, h);
        int s = u.lastIndexOf('/');
        String name = (s >= 0 && s + 1 < u.length()) ? u.substring(s + 1) : u;
        if (name.isEmpty()) name = "model-" + Math.abs(u.hashCode()) + ".gguf";
        return name;
    }

    private static void enqueue(final Context ctx, final String url, final String fileName,
                                final String kind, final long expectedBytes, final Progress cb) {
        final Handler ui = new Handler(Looper.getMainLooper());
        pool.execute(new Runnable() {
            @Override public void run() {
                File out = new File(ModelManager.modelsDir(ctx), fileName);
                try {
                    download(url, out, expectedBytes, cb, ui);
                } catch (Exception e) {
                    final String m = e.getMessage();
                    Log.w(TAG, "下载失败 " + fileName + " : " + m);
                    ui.post(new Runnable() { @Override public void run() {
                        if (cb != null) cb.onError(m == null ? "未知错误" : m);
                    }});
                }
            }
        });
    }

    /** 断点续传 + 失败重试的下载主流程。 */
    private static void download(String url, File out, long expectedBytes,
                                 Progress cb, Handler ui) throws IOException {
        // 已存在且达到预期体积 ⇒ 直接完成
        if (out.isFile() && expectedBytes > 0 && out.length() >= expectedBytes) {
            final File f = out;
            ui.post(new Runnable() { @Override public void run() { if (cb != null) cb.onDone(f); }});
            return;
        }

        long done = out.isFile() ? out.length() : 0;
        long total = 0;
        RandomAccessFile raf = null;

        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            HttpURLConnection c = null;
            InputStream in = null;
            try {
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setRequestMethod("GET");
                c.setConnectTimeout(CONNECT_MS);
                c.setReadTimeout(READ_MS);
                c.setInstanceFollowRedirects(true);
                if (done > 0) {
                    c.setRequestProperty("Range", "bytes=" + done + "-");
                }
                int code = c.getResponseCode();
                if (code != 200 && code != 206) {
                    throw new IOException("HTTP " + code + "：" + readErr(c));
                }
                long cl = headerLong(c, "Content-Length");
                if (code == 206) {
                    total = parseRangeTotal(c.getHeaderField("Content-Range"), cl);
                    if (total <= 0 && cl > 0) total = done + cl;
                } else {
                    total = cl;
                    if (done > 0) {
                        // 服务器不支持续传：删残缺，从头下
                        out.delete();
                        done = 0;
                        if (raf != null) { raf.close(); raf = null; }
                    }
                }
                if (expectedBytes > 0 && total > 0
                        && Math.abs(total - expectedBytes) > expectedBytes * 0.02) {
                    Log.w(TAG, "体积偏差：预期 " + expectedBytes + " 实 Content-Length " + total);
                }

                final long totalF = total;
                if (attempt == 1) {
                    ui.post(new Runnable() { @Override public void run() {
                        if (cb != null) cb.onStart(totalF); }});
                }

                if (raf == null) raf = new RandomAccessFile(out, "rw");
                if (done > 0) raf.seek(done);
                in = new BufferedInputStream(c.getInputStream(), 1 << 16);
                byte[] buf = new byte[1 << 16];
                long lastTick = System.currentTimeMillis();
                long lastDone = done;
                int n;
                while ((n = in.read(buf)) > 0) {
                    raf.write(buf, 0, n);
                    done += n;
                    long now = System.currentTimeMillis();
                    if (now - lastTick >= 500) {
                        final long d = done, t = totalF;
                        final float sp = (done - lastDone) * 1000f / (now - lastTick);
                        ui.post(new Runnable() { @Override public void run() {
                            if (cb != null) cb.onProgress(d, t, sp); }});
                        lastTick = now; lastDone = done;
                    }
                }
                raf.close(); raf = null;   // 正常读到流尾 ⇒ 完成
                c.disconnect();
                break;
            } catch (IOException e) {
                Log.w(TAG, "第 " + attempt + " 次尝试中断：" + e.getMessage()
                        + "（已下 " + done + " 字节，将续传）");
                if (raf != null) { try { raf.close(); } catch (IOException ignored) {} raf = null; }
                if (c != null) c.disconnect();
                if (in != null) { try { in.close(); } catch (IOException ignored) {} }
                if (attempt == MAX_ATTEMPTS) {
                    // 保留残缺文件，下次可续传
                    throw new IOException("下载多次中断（已保留 " + done
                            + " 字节，可重试续传）：" + e.getMessage());
                }
                done = out.isFile() ? out.length() : 0;   // 续传起点
            }
        }

        // 完整性校验：有预期体积用预期，否则用 Content-Length
        long checkTotal = (expectedBytes > 0) ? expectedBytes : total;
        if (checkTotal > 0 && out.length() < checkTotal) {
            throw new IOException("下载不完整：得到 " + out.length() + " / 应 "
                    + checkTotal + " 字节，已保留残缺文件可续传");
        }
        // 自己下的文件属 APP，setReadable 即可；顺手 setExecutable 兼容 sd.cpp 加载
        out.setReadable(true, false);
        out.setExecutable(true, false);
        final File f = out;
        ui.post(new Runnable() { @Override public void run() { if (cb != null) cb.onDone(f); }});
    }

    private static long headerLong(HttpURLConnection c, String name) {
        String v = c.getHeaderField(name);
        if (v == null) return 0;
        try { return Long.parseLong(v.trim()); } catch (Exception e) { return 0; }
    }

    private static long parseRangeTotal(String cr, long cl) {
        if (cr != null) {
            int slash = cr.indexOf('/');
            if (slash >= 0) {
                try { return Long.parseLong(cr.substring(slash + 1).trim()); }
                catch (Exception ignored) {}
            }
        }
        return -1;
    }

    private static String readErr(HttpURLConnection c) {
        InputStream e = c.getErrorStream();
        if (e == null) return "";
        try {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
            byte[] b = new byte[4096]; int k;
            while ((k = e.read(b)) > 0) bo.write(b, 0, k);
            return bo.toString("UTF-8");
        } catch (IOException ex) { return ""; }
    }
}
