package app.skynet.tianwang;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 天网 · 设备检测界面。
 *
 * 存在的理由（主人 2026-10-23）：「你把它进入那个设置界面，再增加一个界面，
 * 可以专门用于检测手机。检测完手机后就是默认你这个手机。」
 * ⇒ 硬件不变，所以只测一次，结果写进 DeviceProfile 当默认配置；以后出图直接读，
 *   不再每次问、也不再每次让人点。
 *
 * 这个 Activity 是 **exported** 的，且支持无人值守跑完：
 *   adb shell am start -n app.skynet.tianwang/.DeviceCheckActivity \
 *       --ez auto true --ez bench true --ei steps 2 --ei size 512
 * 跑完把结论打进 logcat（tag: fleet-dev / fleet-sd），PC 侧直接读，不用盯屏幕。
 * auto=true 时在打完日志后自动 finish。
 *
 * 检测内容只有三样，全部实测、不猜机型：
 *   1. Vulkan 设备清单（sd.cpp 枚举）
 *   2. MemTotal（/proc/meminfo）
 *   3. 基准 s/步（真跑一张）
 */
public class DeviceCheckActivity extends Activity {

    private static final String TAG = "fleet-dev";

    private TextView txtInfo, txtResult;
    private Button btnRun, btnBench, btnClear, btnBack;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean running;
    private DeviceProfile profile;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_device);

        txtInfo = findViewById(R.id.txtDevInfo);
        txtResult = findViewById(R.id.txtDevResult);
        btnRun = findViewById(R.id.btnDevProbe);
        btnBench = findViewById(R.id.btnDevBench);
        btnClear = findViewById(R.id.btnDevClear);
        btnBack = findViewById(R.id.btnDevBack);

        btnRun.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { probeOnly(); }
        });
        btnBench.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { runBench(2, 512); }
        });
        btnClear.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                File f = new File(getFilesDir(), "device_profile.json");
                if (f.delete()) {
                    Log.i(TAG, "档案已删除");
                    toast("档案已清除，下次出图会重新检测");
                } else {
                    toast("档案本来就不存在");
                }
                showSaved();
            }
        });
        btnBack.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });

        // ---- 无人值守模式 ----
        final boolean auto = getIntent().getBooleanExtra("auto", false);
        final boolean bench = getIntent().getBooleanExtra("bench", false);
        final int steps = getIntent().getIntExtra("steps", 2);
        final int size = getIntent().getIntExtra("size", 512);
        if (auto) {
            Log.i(TAG, "auto 模式：bench=" + bench + " steps=" + steps + " size=" + size);
            if (bench) runBench(Math.max(1, steps), size, true);
            else { probeOnly(); finish(); }
        } else {
            showSaved();
        }
    }

    // ---------------------------------------------------------------- 只测硬件

    private void probeOnly() {
        profile = DeviceProfile.probe(this);
        profile.save(this);
        Log.i(TAG, "PROBE " + profile.toString());
        ui.post(new Runnable() {
            @Override public void run() { showProfile(profile); }
        });
    }

    // ---------------------------------------------------------------- 跑基准

    private void runBench(int steps, int size) { runBench(steps, size, false); }

    private void runBench(final int steps, final int size, final boolean autoFinish) {
        if (running) { toast("正在检测中"); return; }
        final List<ModelManager.Spec> av = ModelManager.availableSd(this);
        if (av.isEmpty()) {
            Log.e(TAG, "BENCH_SKIP 没有可用出图模型，无法跑基准");
            txtResult.setText("没有可用模型，先推模型进来再跑基准。");
            if (autoFinish) finish();
            return;
        }
        final ModelManager.Spec spec = av.get(0);
        running = true;
        setBusy(true);
        txtResult.setText("跑基准中…（" + spec.label + " " + size + "² " + steps + " 步，别退出）");

        worker.execute(new Runnable() {
            @Override public void run() {
                final DeviceProfile p = BenchRunner.run(DeviceCheckActivity.this, spec,
                        size, steps, new BenchRunner.Sink() {
                            @Override public void onLog(final String line) {
                                ui.post(new Runnable() {
                                    @Override public void run() { txtResult.setText(line); }
                                });
                            }
                        });
                running = false;
                final DeviceProfile done = p != null ? p : DeviceProfile.probe(DeviceCheckActivity.this);
                ui.post(new Runnable() {
                    @Override public void run() {
                        setBusy(false);
                        profile = done;
                        showProfile(done);
                        if (autoFinish) finish();
                    }
                });
            }
        });
    }

    // ---------------------------------------------------------------- 显示

    private void setBusy(boolean b) {
        btnRun.setEnabled(!b);
        btnBench.setEnabled(!b);
        btnClear.setEnabled(!b);
    }

    private void showSaved() {
        DeviceProfile p = DeviceProfile.load(this);
        if (p == null) {
            txtInfo.setText("还没有设备档案。点「检测硬件」或「跑基准」生成一份。");
            txtResult.setText("");
            return;
        }
        showProfile(p);
        txtResult.setText("（这是已存档案，"
                + new java.text.SimpleDateFormat("MM-dd HH:mm", Locale.CHINA)
                        .format(new java.util.Date(p.detectAtMs)) + " 测的，v" + p.appVersion + "）");
    }

    private void showProfile(DeviceProfile p) {
        StringBuilder sb = new StringBuilder();
        sb.append("GPU：").append(p.hasVulkan
                ? (p.vulkanDesc.isEmpty() ? p.vulkanDevice : p.vulkanDesc)
                : "没检测到 Vulkan（将退回 CPU，会非常慢）").append('\n');
        sb.append("内存：").append(p.memTotalMb).append(" MB\n");
        sb.append("CPU 核数：").append(p.cpuCores).append('\n');
        sb.append("后端：").append(p.backend).append('\n');
        sb.append("基准：").append(p.benched
                ? String.format(Locale.US, "%.2fs/步 · VAE %.0fs · %d² %d步 · %s",
                        p.secPerStep, p.vaeSec, p.benchSize, p.benchSteps, p.benchModel)
                : "还没跑").append('\n');

        List<ModelManager.Spec> av = ModelManager.availableSd(this);
        if (av.isEmpty()) {
            sb.append("\n模型：一个都没有（先推出图模型进来）");
        } else {
            ModelManager.Spec cur = ModelManager.current(this);
            sb.append("\n模型：").append(cur.label).append('\n');
            for (DeviceProfile.Tier t : DeviceProfile.Tier.values()) {
                DeviceProfile.Plan pl = p.plan(cur, t);
                sb.append(t == DeviceProfile.Tier.FAST ? "  快速 " :
                          t == DeviceProfile.Tier.STD ? "  标准 " : "  精细 ")
                  .append(pl.toString()).append('\n');
            }
        }
        txtInfo.setText(sb.toString());
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }

    @Override protected void onDestroy() {
        worker.shutdownNow();
        super.onDestroy();
    }
}
