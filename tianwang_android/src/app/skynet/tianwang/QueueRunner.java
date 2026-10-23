package app.skynet.tianwang;

import android.content.Context;
import android.os.Environment;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.util.List;
import java.util.Locale;

/**
 * 天网 · 队列执行器（阻塞，调用方放工作线程）。
 *
 * 抽出来的原因和 BenchRunner 一样：小米"后台弹出界面"默认关闭，非 LAUNCHER 的
 * Activity 起不来 ⇒ 队列要能被 PC 侧无人值守触发，逻辑就不能只写在 ImageActivity 里。
 *
 * 三条硬约束：
 *   1. **串行**：实测单张 RSS 峰值 2.67GB，并发必 OOM。
 *   2. **每张都落盘**：进程被划掉后要能看出每张的状态/耗时/失败原因（主人要求"事后裁定"）。
 *   3. 中断后剩余项保持 pending 留在盘上，不丢。
 */
public final class QueueRunner {

    private static final String TAG = "fleet-queue";
    private static final int MAX_RETRIES = 3;

    public interface Sink {
        void onProgress(String line);
    }

    /** 每轮开始前问一次：true = 停下。剩余项保持 pending 留在盘上。 */
    public interface Stop {
        boolean shouldStop();
    }

    /** 跑完队列里所有 pending。返回 true 表示全跑完（无中断、无失败）。 */
    public static boolean run(Context ctx, List<QueueStore.Item> queue,
                              DeviceProfile profile, Stop stop, Sink sink) {
        if (queue == null || queue.isEmpty()) {
            Log.w(TAG, "队列是空的");
            return true;
        }
        ModelManager.Spec spec = ModelManager.current(ctx);
        SdBridge sd = new SdBridge();
        DeviceProfile prof = profile != null ? profile : DeviceProfile.load(ctx);
        if (prof == null) prof = DeviceProfile.probe(ctx);

        emit(sink, "装模型中…");
        boolean okLoad = sd.load(ModelManager.fileOf(ctx, spec).getAbsolutePath(),
                Math.max(2, Math.min(6, prof.cpuCores / 2)), prof.backend,
                ModelManager.taesdPath(ctx));
        if (!okLoad) {
            emit(sink, "模型加载失败：" + sd.lastError);
            Log.e(TAG, "QUEUE_FAIL 模型加载失败：" + sd.lastError);
            sd.close();
            return false;
        }

        try {
            int total = 0;
            for (QueueStore.Item it : queue) {
                if (QueueStore.PENDING.equals(it.status)) total++;
            }
            int idx = 0;
            for (QueueStore.Item it : queue) {
                if (!QueueStore.PENDING.equals(it.status)) continue;
                idx++;
                if (stop != null && stop.shouldStop()) {
                    it.status = QueueStore.ABORTED;
                    it.error = "被用户中断";
                    QueueStore.save(ctx, queue);
                    QueueStore.log(it, "abort");
                    continue;
                }
                it.status = QueueStore.RUNNING;
                it.startedAt = System.currentTimeMillis();
                QueueStore.save(ctx, queue);

                final String tag = "第 " + idx + "/" + total + " 张";
                long t0 = System.currentTimeMillis();
                byte[] png = null;
                for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
                    png = sd.generate(it.prompt, it.neg, it.size, it.size,
                            it.steps, it.seed, it.cfg, false, it.sampler,
                            new SdBridge.Progress() {
                                @Override public void onProgress(int step, int steps) {
                                    emit(sink, tag + " 推理中… " + step + "/" + steps);
                                }
                            });
                    if (png != null && png.length > 0) break;
                    if (attempt < MAX_RETRIES) {
                        emit(sink, tag + " 失败重试 " + attempt + "/" + MAX_RETRIES
                                + "：" + sd.lastError);
                        try { Thread.sleep(2000); } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                            break;
                        }
                    }
                }
                it.ms = System.currentTimeMillis() - t0;

                if (png == null || png.length == 0) {
                    it.status = QueueStore.FAILED;
                    it.error = sd.lastError;
                } else {
                    File f = savePng(ctx, png, it.id);
                    it.status = QueueStore.DONE;
                    it.out = f == null ? "" : f.getName();
                }
                it.endedAt = System.currentTimeMillis();
                QueueStore.save(ctx, queue);
                QueueStore.log(it, null);
                emit(sink, tag + " 完成 · " + String.format(Locale.US, "%.1fs", it.ms / 1000.0)
                        + " · " + QueueStore.summary(queue));
            }
        } finally {
            sd.close();
        }

        // 收尾把每张最终状态打全：PC 侧拉 logcat 就能判定哪张错、为什么错
        for (QueueStore.Item it : queue) QueueStore.log(it, "final");
        Log.i(TAG, "QUEUE_END " + QueueStore.summary(queue));

        boolean allDone = true;
        for (QueueStore.Item it : queue) {
            if (!QueueStore.DONE.equals(it.status)) { allDone = false; break; }
        }
        return allDone;
    }

    private static void emit(Sink sink, String s) {
        Log.i(TAG, s);
        if (sink != null) sink.onProgress(s);
    }

    private static File savePng(Context ctx, byte[] png, long id) {
        try {
            File dir = new File(ctx.getExternalFilesDir(Environment.DIRECTORY_PICTURES), "fleet");
            if (!dir.exists() && !dir.mkdirs()) return null;
            File f = new File(dir, "fleet-" + id + ".png");
            FileOutputStream os = new FileOutputStream(f);
            os.write(png);
            os.close();
            return f;
        } catch (Exception e) {
            Log.w(TAG, "存图失败 " + e);
            return null;
        }
    }
}
