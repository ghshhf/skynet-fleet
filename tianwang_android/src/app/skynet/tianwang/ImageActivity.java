package app.skynet.tianwang;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 天网 · 手机出图界面（本地 stable-diffusion.cpp + Vulkan）。
 *
 * 出图是**手机本地**算力：prompt → SdBridge → PNG → 展示 + 存盘。
 * 不经过电脑、不新增网络出口。模型不在 APK 里（1.6~2.3GB），在 APP 外部私有目录。
 *
 * 三条纪律：
 *   1. 推理以分钟计且阻塞 ⇒ 全在 worker 线程，UI 只用 post 改。
 *   2. **串行不并发**：实测单张 RSS 峰值 2.67GB，并发必 OOM。
 *   3. 参数由「档位」给，档位由 DeviceProfile 按实测算（GPU/内存/基准 s/步），
 *      依据显示在界面上，且步数/尺寸可以手动覆盖——不许让人盲信自动选择。
 *
 * 队列：一行 prompt 一张，落盘（QueueStore），进程被划掉后回来能看到每张的
 * 状态/耗时/失败原因（主人 2026-10-23 要求：事后要能裁定哪里错了）。
 */
public class ImageActivity extends Activity {

    private static final String TAG = "fleet-img";

    private EditText editPrompt, editSteps;
    private TextView btnSize512, btnSize768, btnSize1024;
    private TextView btnTierFast, btnTierStd, btnTierFine;
    private TextView txtStatus, txtQueue, txtReason, btnBack, btnModel;
    private Button btnGen, btnStop, btnRegen;
    private ImageView imgResult;

    private int size = 512;
    private DeviceProfile.Tier tier = DeviceProfile.Tier.STD;
    private volatile boolean running;
    private volatile boolean stopRequested;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private SdBridge sd;
    private File lastPng;
    private DeviceProfile profile;
    private final List<QueueStore.Item> queue = new ArrayList<QueueStore.Item>();

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_image);

        editPrompt = findViewById(R.id.editPrompt);
        editSteps = findViewById(R.id.editSteps);
        btnSize512 = findViewById(R.id.btnSize512);
        btnSize768 = findViewById(R.id.btnSize768);
        btnSize1024 = findViewById(R.id.btnSize1024);
        btnTierFast = findViewById(R.id.btnTierFast);
        btnTierStd = findViewById(R.id.btnTierStd);
        btnTierFine = findViewById(R.id.btnTierFine);
        txtStatus = findViewById(R.id.txtStatus);
        txtQueue = findViewById(R.id.txtQueue);
        txtReason = findViewById(R.id.txtReason);
        btnGen = findViewById(R.id.btnGen);
        btnStop = findViewById(R.id.btnStop);
        btnRegen = findViewById(R.id.btnRegen);
        imgResult = findViewById(R.id.imgResult);
        btnBack = findViewById(R.id.btnBack);
        btnModel = findViewById(R.id.btnModel);
        btnStop.setEnabled(false);
        btnRegen.setEnabled(false);

        // 从主界面「+」跳进来时会把已经打好的描述带过来：人多半是先写了
        // 描述才想起要画图，让他在两个页面各打一遍纯属浪费。
        String seed = getIntent().getStringExtra("prompt");
        if (seed != null && !seed.trim().isEmpty()) {
            editPrompt.setText(seed.trim());
            editPrompt.setSelection(editPrompt.getText().length());
        }

        View.OnClickListener sizeClick = new View.OnClickListener() {
            @Override public void onClick(View v) {
                size = (v == btnSize512) ? 512 : (v == btnSize768) ? 768 : 1024;
                refresh();
            }
        };
        btnSize512.setOnClickListener(sizeClick);
        btnSize768.setOnClickListener(sizeClick);
        btnSize1024.setOnClickListener(sizeClick);

        View.OnClickListener tierClick = new View.OnClickListener() {
            @Override public void onClick(View v) {
                tier = (v == btnTierFast) ? DeviceProfile.Tier.FAST
                     : (v == btnTierFine) ? DeviceProfile.Tier.FINE
                     : DeviceProfile.Tier.STD;
                applyTierToUi();
            }
        };
        btnTierFast.setOnClickListener(tierClick);
        btnTierStd.setOnClickListener(tierClick);
        btnTierFine.setOnClickListener(tierClick);

        btnGen.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { enqueueAndRun(false); }
        });
        btnRegen.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { enqueueAndRun(true); }
        });
        btnStop.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (!running) return;
                stopRequested = true;
                if (sd != null) sd.cancel();
                setStatus("停止中…（当前这张画完就停，队列留在盘上）", 0xFFD29922);
            }
        });
        btnBack.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        btnModel.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { showModelPicker(); }
        });

        // 设备档案：没有就当场测一次并存下来（只测这一次——硬件不会变）。
        profile = DeviceProfile.load(this);
        if (profile == null) {
            profile = DeviceProfile.probe(this);
            profile.save(this);
            Log.i(TAG, "首次建立设备档案：" + profile);
        } else {
            Log.i(TAG, "沿用设备档案：" + profile);
        }
        applyTierToUi();

        // 上次没跑完的队列：恢复回来，让人知道还有几张、为什么没跑完
        queue.clear();
        queue.addAll(QueueStore.load(this));
        if (!queue.isEmpty()) {
            txtQueue.setText(QueueStore.summary(queue));
            setStatus("有上次没跑完的队列，点「生成」继续", 0xFFD29922);
        }
        checkModel();
    }

    // ---------------------------------------------------------------- 档位

    /** 档位 → UI（尺寸/步数/依据）。步数可手动改，改了就不再被档位覆盖。 */
    private void applyTierToUi() {
        ModelManager.Spec spec = ModelManager.current(this);
        DeviceProfile.Plan pl = profile.plan(spec, tier);
        size = pl.size;
        editSteps.setText(String.valueOf(pl.steps));
        // 用没用 TAESD 必须让人看见：它决定了 VAE 那一段是秒级还是 134 秒级，
        // 而且是拿画质换的——不能悄悄换掉。
        txtReason.setText(pl.reason + (ModelManager.taesdPath(this) != null
                ? " · TAESD 解码（快，画质略软）" : " · 原 VAE（解码约 134s）"));
        refresh();
    }

    private void refresh() {
        int sel = 0xFF2F6BD8, dim = 0xFF21262D;
        btnSize512.setBackgroundColor(size == 512 ? sel : dim);
        btnSize768.setBackgroundColor(size == 768 ? sel : dim);
        btnSize1024.setBackgroundColor(size == 1024 ? sel : dim);
        btnTierFast.setBackgroundColor(tier == DeviceProfile.Tier.FAST ? sel : dim);
        btnTierStd.setBackgroundColor(tier == DeviceProfile.Tier.STD ? sel : dim);
        btnTierFine.setBackgroundColor(tier == DeviceProfile.Tier.FINE ? sel : dim);
        btnModel.setText(ModelManager.current(this).label);
    }

    private void setStatus(String s, int color) {
        txtStatus.setText(s);
        txtStatus.setTextColor(color);
    }

    private void checkModel() {
        if (ModelManager.isReady(this)) {
            setStatus("模型就绪，一行一句 prompt，点「生成」排队出", 0xFF8B949E);
        } else {
            setStatus("缺出图模型：" + ModelManager.modelPath(this), 0xFFDA3633);
            ModelManager.showMissingDialog(this);
        }
    }

    private void showModelPicker() {
        final List<ModelManager.Spec> av = ModelManager.availableSd(this);
        if (av.isEmpty()) { ModelManager.showMissingDialog(this); return; }
        String[] names = new String[av.size()];
        for (int i = 0; i < av.size(); i++) names[i] = av.get(i).label;
        new AlertDialog.Builder(this)
                .setTitle("选模型")
                .setItems(names, new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int which) {
                        ModelManager.choose(ImageActivity.this, av.get(which));
                        if (sd != null) { sd.close(); sd = null; }   // 换模型必须重新加载
                        applyTierToUi();
                        toast("已切到 " + av.get(which).label);
                        d.dismiss();
                    }
                })
                .show();
    }

    // ---------------------------------------------------------------- 入队

    private void enqueueAndRun(boolean regen) {
        if (running) { toast("队列正在跑"); return; }
        if (!ModelManager.isReady(this)) { ModelManager.showMissingDialog(this); return; }

        String[] lines = editPrompt.getText().toString().split("\n");
        List<String> prompts = new ArrayList<String>();
        for (String ln : lines) {
            String t = ln.trim();
            if (!t.isEmpty()) prompts.add(t);
        }
        // 队列里还有没跑完的，就接着跑它们，不加新的（避免"我明明点了一次却出了两批"）
        boolean resumed = false;
        for (QueueStore.Item it : queue) {
            if (QueueStore.PENDING.equals(it.status)) { resumed = true; break; }
        }
        if (!resumed) {
            if (prompts.isEmpty()) { toast("先写 prompt（一行一张）"); return; }
            final ModelManager.Spec spec = ModelManager.current(this);
            final DeviceProfile.Plan pl = profile.plan(spec, tier);
            int steps = parseIntSafe(editSteps.getText().toString(), pl.steps);
            if (steps < 1 || steps > 30) { toast("步数要在 1–30 之间"); return; }
            queue.clear();
            long base = regen ? System.currentTimeMillis() : 42L;
            for (int i = 0; i < prompts.size(); i++) {
                QueueStore.Item it = new QueueStore.Item();
                it.id = System.currentTimeMillis() + i;
                it.prompt = prompts.get(i);
                it.size = size;
                it.steps = steps;
                it.seed = base + i;          // 同一批每张种子不同，不然出一样的
                it.cfg = spec.cfg;
                it.sampler = spec.sampler;
                it.model = spec.file;
                it.tier = tier.name();
                it.status = QueueStore.PENDING;
                it.createdAt = System.currentTimeMillis();
                queue.add(it);
            }
            QueueStore.save(this, queue);
            Log.i(TAG, "入队 " + queue.size() + " 张：" + QueueStore.summary(queue));
        } else {
            Log.i(TAG, "续跑上次队列：" + QueueStore.summary(queue));
        }

        stopRequested = false;
        running = true;
        btnGen.setEnabled(false);
        btnStop.setEnabled(true);
        btnRegen.setEnabled(false);
        runQueue();
    }

    private static int parseIntSafe(String s, int def) {
        try { return Integer.parseInt(s.trim()); } catch (Exception e) { return def; }
    }

    // ---------------------------------------------------------------- 跑队列

    private void runQueue() {
        worker.execute(new Runnable() {
            @Override public void run() {
                // 队列执行逻辑在 QueueRunner 里：它需要能被 PC 侧无人值守触发
                // （小米"后台弹出界面"拦非 LAUNCHER Activity，所以不能只写在本页面里）。
                QueueRunner.run(ImageActivity.this, queue, profile,
                        new QueueRunner.Stop() {
                            @Override public boolean shouldStop() { return stopRequested; }
                        },
                        new QueueRunner.Sink() {
                            @Override public void onProgress(final String line) {
                                ui.post(new Runnable() {
                                    @Override public void run() { setStatus(line, 0xFF8B949E); }
                                });
                            }
                        });
                finishRun();
            }
        });
    }

    private void finishRun() {
        running = false;
        ui.post(new Runnable() {
            @Override public void run() {
                btnGen.setEnabled(true);
                btnStop.setEnabled(false);
                btnRegen.setEnabled(true);
                txtQueue.setText(QueueStore.summary(queue));
                int done = 0, fail = 0;
                for (QueueStore.Item it : queue) {
                    if (QueueStore.DONE.equals(it.status)) done++;
                    else if (QueueStore.FAILED.equals(it.status)) fail++;
                }
                if (fail > 0) setStatus("队列结束：完成 " + done + " 张，失败 " + fail
                        + " 张（原因见 logcat fleet-queue）", 0xFFDA3633);
                else if (done > 0) setStatus("队列结束：完成 " + done + " 张", 0xFF3FB950);
            }
        });
    }

    private void post(final String s, final int color) {
        ui.post(new Runnable() {
            @Override public void run() { setStatus(s, color); }
        });
    }

    private void showBitmap(final byte[] png) {
        final Bitmap bmp = BitmapFactory.decodeByteArray(png, 0, png.length);
        ui.post(new Runnable() {
            @Override public void run() {
                if (bmp != null) imgResult.setImageBitmap(bmp);
            }
        });
    }

    /** 存到 APP 私有外部目录（Pictures/fleet），不需要任何存储权限。 */
    private File savePng(byte[] png, long id) {
        try {
            File dir = new File(getExternalFilesDir(Environment.DIRECTORY_PICTURES), "fleet");
            if (!dir.exists() && !dir.mkdirs()) return null;
            File f = new File(dir, "fleet-" + id + ".png");
            FileOutputStream os = new FileOutputStream(f);
            os.write(png);
            os.close();
            return f;
        } catch (Exception e) {
            Log.w(TAG, "save fail " + e);
            return null;
        }
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }

    @Override
    protected void onDestroy() {
        if (running && sd != null) sd.cancel();
        QueueStore.save(this, queue);     // 被划掉也要留下现场
        if (sd != null) { sd.close(); sd = null; }
        worker.shutdownNow();
        super.onDestroy();
    }
}
