package app.skynet.tianwang;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.util.Locale;

/**
 * 天网 · 出图基准（阻塞执行，调用方负责放到工作线程）。
 *
 * 抽出来是因为**触发方式受限**：小米的「后台弹出界面」默认关闭，
 * `am start` 一个非 LAUNCHER 的新 Activity 会被直接拦掉（实测 DeviceCheckActivity
 * 起不来，日志里只有 "top resumed state loss timeout"）。所以基准必须能从
 * **MainActivity**（LAUNCHER，实测 am start 能起）触发，也能从检测页触发，
 * 逻辑就得共用一份，不能抄两遍。
 *
 * 用法（PC 侧，无人值守）：
 *   adb shell am start -n app.skynet.tianwang/.MainActivity \
 *       --ez bench true --ei steps 2 --ei size 512
 * 结论全部进 logcat（tag fleet-dev），屏幕上不用看。
 */
public final class BenchRunner {

    private static final String TAG = "fleet-dev";

    public interface Sink {
        /** 在调用线程同步回调（可能是工作线程），别在这里碰 UI。 */
        void onLog(String line);
    }

    /**
     * @return 跑完的设备档案（已落盘）；失败返回 null，原因看 logcat 与 sink。
     */
    public static DeviceProfile run(Context ctx, ModelManager.Spec spec,
                                    int size, int steps, Sink sink) {
        if (spec == null) {
            Log.e(TAG, "BENCH_SKIP 没有指定模型");
            if (sink != null) sink.onLog("没有指定模型");
            return null;
        }

        DeviceProfile p = DeviceProfile.probe(ctx);
        log(sink, "PROBE " + p.toString());

        SdBridge sd = new SdBridge();
        long tLoad0 = System.currentTimeMillis();
        boolean ok = sd.load(ModelManager.fileOf(ctx, spec).getAbsolutePath(),
                Math.max(2, p.cpuCores / 2), p.backend, ModelManager.taesdPath(ctx));
        long loadMs = System.currentTimeMillis() - tLoad0;
        if (!ok) {
            Log.e(TAG, "BENCH_FAIL 模型加载失败：" + sd.lastError);
            log(sink, "模型加载失败：" + sd.lastError);
            sd.close();
            return null;
        }

        // 分段依据与 C 端一致：一次生成里进度回调会分几段（如 686→2→140），
        // step 值回退 = 换段，最后一次回退才是 VAE 起点。只记首/末回调会算出
        // "74s/步" 这种错数（实测踩过）。
        final long[] denoiseEnd = { 0 };
        final int[] lastStep = { -1 };
        long t0 = System.currentTimeMillis();
        byte[] png = sd.generate("a cat", "", size, size, steps, 42L, spec.cfg,
                false, spec.sampler, new SdBridge.Progress() {
                    @Override public void onProgress(int step, int steps) {
                        if (lastStep[0] >= 0 && step < lastStep[0]) {
                            denoiseEnd[0] = System.currentTimeMillis();
                        }
                        lastStep[0] = step;
                    }
                });
        long totalMs = System.currentTimeMillis() - t0;

        if (png == null || png.length == 0) {
            Log.e(TAG, "BENCH_FAIL 推理失败：" + sd.lastError);
            log(sink, "推理失败：" + sd.lastError);
            sd.close();
            return null;
        }

        // 分得出段就按段算；分不出（只有一段回调）就如实标"未分段"，
        // 不拿总时间除以步数假装那是 s/步。
        // 存盘：跑基准的唯一目的就是"看数 + 看图"，图不落盘就只能靠人肉盯手机屏幕，
        // 而画质恰恰是要拿回来比对的东西（SD1.5 vs Turbo、原 VAE vs TAESD）。
        File out = savePng(ctx, png, spec.file + "-" + size + "-" + steps);
        if (out != null) Log.i(TAG, "BENCH_PNG " + out.getAbsolutePath());

        float denoiseMs = denoiseEnd[0] > 0 ? (denoiseEnd[0] - t0) : 0f;
        float sPerStep = denoiseMs > 0 ? denoiseMs / 1000.0f / (float) steps
                                       : totalMs / 1000.0f / (float) steps;
        float vae = denoiseMs > 0 ? (totalMs - denoiseMs) / 1000.0f : 0f;

        p.benched = true;
        p.secPerStep = sPerStep;
        p.vaeSec = vae;
        p.benchSize = size;
        p.benchSteps = steps;
        p.benchModel = spec.file;
        p.save(ctx);
        sd.close();

        Log.i(TAG, String.format(Locale.US,
                "BENCH_OK model=%s backend=%s size=%d steps=%d 加载%ds 总%.1fs "
                        + "%.2fs/步 VAE≈%.0fs PNG=%d字节%s",
                spec.file, p.backend, size, steps, loadMs / 1000L,
                totalMs / 1000.0, sPerStep, vae, png.length,
                denoiseMs > 0 ? "" : " ⚠️未分段"));

        // 三档预估一次打全：PC 侧看这几行就能定档位
        for (DeviceProfile.Tier t : DeviceProfile.Tier.values()) {
            DeviceProfile.Plan pl = p.plan(spec, t);
            Log.i(TAG, String.format(Locale.US, "PLAN %s(%s): %dx%d %d步 预估 %.0fs | %s",
                    t, spec.file, pl.size, pl.size, pl.steps, pl.estSec, pl.reason));
        }
        log(sink, String.format(Locale.US,
                "基准完成：%.2fs/步 · VAE≈%.0fs · 总 %.1fs（加载 %ds）",
                sPerStep, vae, totalMs / 1000.0, loadMs / 1000L));
        return p;
    }

    private static void log(Sink sink, String s) {
        if (sink != null) sink.onLog(s);
    }

    /** 存到 APP 私有外部目录（Pictures/fleet），不需要存储权限，PC 侧 adb pull 拿得到。 */
    private static File savePng(Context ctx, byte[] png, String name) {
        try {
            File dir = new File(ctx.getExternalFilesDir(android.os.Environment.DIRECTORY_PICTURES),
                    "fleet");
            if (!dir.exists() && !dir.mkdirs()) return null;
            File f = new File(dir, "bench-" + name + "-" + System.currentTimeMillis() + ".png");
            java.io.FileOutputStream os = new java.io.FileOutputStream(f);
            os.write(png);
            os.close();
            return f;
        } catch (Exception e) {
            Log.w(TAG, "bench 存图失败 " + e);
            return null;
        }
    }
}
