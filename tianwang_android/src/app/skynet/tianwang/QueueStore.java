package app.skynet.tianwang;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 天网 · 出图队列（落盘，进程被杀不丢）。
 *
 * 为什么必须落盘（主人 2026-10-23）：「排队机制被杀后保存吧，最后咱们裁定，
 * 因为就能知道是哪里有问题，哪里错了。」
 * ⇒ 队列状态每次变化就写盘；进程被划掉/系统回收后，重启能看到每张的状态、
 *   耗时、失败原因，事后能裁定哪一环出问题，而不是"好像没跑完"。
 *
 * 两个硬约束：
 *   1. **串行，不并发。** 实测单张出图 RSS 峰值 2.67GB（512² SD1.5），
 *      并发两张必 OOM。快不了，但能跑完。
 *   2. 恢复时 running → pending：进程死了那张一定没跑完，不能当已完成。
 */
public final class QueueStore {

    private static final String TAG = "fleet-queue";
    private static final String FILE = "img_queue.json";

    public static final String PENDING = "pending";
    public static final String RUNNING = "running";
    public static final String DONE    = "done";
    public static final String FAILED  = "failed";
    public static final String ABORTED = "aborted";

    public static final class Item {
        public long id;
        public String prompt = "";
        public String neg = "";
        public int size = 512;
        public int steps = 20;
        public long seed = 42L;
        public float cfg = 7.0f;
        public int sampler = SdBridge.SAMPLE_EULER_A;
        public String model = "";       // 模型文件名
        public String tier = "STD";
        public String status = PENDING;
        public String out = "";         // 成功后的 PNG 文件名
        public long ms;                 // 本张耗时
        public String error = "";
        public long createdAt;
        public long startedAt;
        public long endedAt;

        JSONObject toJson() throws Exception {
            JSONObject o = new JSONObject();
            o.put("id", id);
            o.put("prompt", prompt);
            o.put("neg", neg);
            o.put("size", size);
            o.put("steps", steps);
            o.put("seed", seed);
            o.put("cfg", (double) cfg);
            o.put("sampler", sampler);
            o.put("model", model);
            o.put("tier", tier);
            o.put("status", status);
            o.put("out", out);
            o.put("ms", ms);
            o.put("error", error);
            o.put("createdAt", createdAt);
            o.put("startedAt", startedAt);
            o.put("endedAt", endedAt);
            return o;
        }

        static Item fromJson(JSONObject o) {
            Item it = new Item();
            it.id = o.optLong("id");
            it.prompt = o.optString("prompt", "");
            it.neg = o.optString("neg", "");
            it.size = o.optInt("size", 512);
            it.steps = o.optInt("steps", 20);
            it.seed = o.optLong("seed", 42L);
            it.cfg = (float) o.optDouble("cfg", 7.0);
            it.sampler = o.optInt("sampler", SdBridge.SAMPLE_EULER_A);
            it.model = o.optString("model", "");
            it.tier = o.optString("tier", "STD");
            it.status = o.optString("status", PENDING);
            it.out = o.optString("out", "");
            it.ms = o.optLong("ms", 0);
            it.error = o.optString("error", "");
            it.createdAt = o.optLong("createdAt", 0);
            it.startedAt = o.optLong("startedAt", 0);
            it.endedAt = o.optLong("endedAt", 0);
            return it;
        }
    }

    private static File file(Context ctx) {
        return new File(ctx.getFilesDir(), FILE);
    }

    /** 全量覆盖写。队列最多几十项、每次出图才动一次，没有性能问题。 */
    public static synchronized void save(Context ctx, List<Item> items) {
        try {
            JSONArray a = new JSONArray();
            for (Item it : items) a.put(it.toJson());
            FileOutputStream os = new FileOutputStream(file(ctx));
            os.write(a.toString(2).getBytes("UTF-8"));
            os.close();
        } catch (Exception e) {
            Log.e(TAG, "队列写盘失败：" + e);
        }
    }

    public static synchronized List<Item> load(Context ctx) {
        List<Item> out = new ArrayList<Item>();
        File f = file(ctx);
        if (!f.isFile()) return out;
        try {
            byte[] b = new byte[(int) f.length()];
            RandomAccessFile raf = new RandomAccessFile(f, "r");
            raf.readFully(b);
            raf.close();
            JSONArray a = new JSONArray(new String(b, "UTF-8"));
            int revived = 0;
            for (int i = 0; i < a.length(); i++) {
                Item it = Item.fromJson(a.getJSONObject(i));
                // 进程活着时不会走到这里：能读出来说明上次是被杀/退出的，
                // 那张 RUNNING 一定没跑完 ⇒ 退回 pending 重跑，不能算完成。
                if (RUNNING.equals(it.status)) {
                    it.status = PENDING;
                    it.error = "上次进程中断，恢复重跑";
                    revived++;
                }
                out.add(it);
            }
            if (revived > 0) {
                Log.i(TAG, "恢复 " + revived + " 张被打断的图（running→pending）");
                save(ctx, out);
            }
            return out;
        } catch (Exception e) {
            Log.e(TAG, "队列读盘失败：" + e);
            return out;
        }
    }

    public static synchronized void clear(Context ctx) {
        File f = file(ctx);
        if (f.isFile() && f.delete()) Log.i(TAG, "队列已清空");
    }

    /** 打一张的状态变化：事后裁定全靠这些行。 */
    public static void log(Item it, String note) {
        Log.i(TAG, String.format(Locale.US,
                "#%d [%s/%s] %s %.0fs %s | prompt=%.40s",
                it.id, it.status, it.tier, it.model, it.ms / 1000.0,
                note == null ? "" : note, it.prompt));
    }

    public static String summary(List<Item> items) {
        int p = 0, d = 0, f = 0, a = 0;
        for (Item it : items) {
            if (PENDING.equals(it.status) || RUNNING.equals(it.status)) p++;
            else if (DONE.equals(it.status)) d++;
            else if (FAILED.equals(it.status)) f++;
            else a++;
        }
        return String.format(Locale.US, "共%d张：待出%d 完成%d 失败%d 中止%d",
                items.size(), p, d, f, a);
    }
}
