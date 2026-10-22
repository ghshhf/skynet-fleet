package app.skynet.tianwang;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Build;
import android.os.Bundle;
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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 天网 · 手机出图界面（N+1c）。
 *
 * 与语音那条「走电脑」的路不同，出图是**手机本地**算力：
 *   输 prompt → 构造 Termux RUN_COMMAND → 调 ~/fleet/fleet-img.sh（本地 sd.cpp Vulkan 出图）
 *   → Termux 经 PendingIntent 回传一行 JSON → 本 Activity 读 Pictures/fleet 里的 PNG 展示。
 *
 * 不经过电脑、不新增网络出口。失败一律把原因原样贴出来，不静默。
 * 线程纪律同 MainActivity：网络/IO 在后台，UI 只用 post 改。
 */
public class ImageActivity extends Activity {

    private static final String TAG = "fleet-img";

    // ---- Termux RUN_COMMAND 契约（termux-app v0.53 源码核实，见动手范围文档 §4）----
    private static final String TERMUX_PKG = "com.termux";
    private static final String ACTION_RUN = "com.termux.RUN_COMMAND";
    private static final String SERVICE = "com.termux.app.RunCommandService";
    private static final String EXTRA_PATH = "com.termux.RUN_COMMAND_PATH";
    private static final String EXTRA_ARGS = "com.termux.RUN_COMMAND_ARGUMENTS";
    private static final String EXTRA_WORKDIR = "com.termux.RUN_COMMAND_WORKDIR";
    private static final String EXTRA_BG = "com.termux.RUN_COMMAND_BACKGROUND";
    private static final String EXTRA_PI = "com.termux.RUN_COMMAND_PENDING_INTENT";
    private static final String ACTION_RESULT = "app.skynet.tianwang.TERMUX_RESULT";

    private static final String WORKER = "/data/data/com.termux/files/home/fleet/fleet-img.sh";
    private static final String WORKDIR = "/data/data/com.termux/files/home/fleet";

    private EditText editPrompt, editSteps;
    private TextView btnSize512, btnSize768, btnSize1024, txtStatus, btnBack;
    private Button btnGen, btnRegen;
    private ImageView imgResult;
    private int size = 512;
    private volatile boolean running;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private BroadcastReceiver rx;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_image);

        editPrompt = findViewById(R.id.editPrompt);
        editSteps = findViewById(R.id.editSteps);
        btnSize512 = findViewById(R.id.btnSize512);
        btnSize768 = findViewById(R.id.btnSize768);
        btnSize1024 = findViewById(R.id.btnSize1024);
        txtStatus = findViewById(R.id.txtStatus);
        btnGen = findViewById(R.id.btnGen);
        btnRegen = findViewById(R.id.btnRegen);
        imgResult = findViewById(R.id.imgResult);
        btnBack = findViewById(R.id.btnBack);
        btnRegen.setEnabled(false);

        View.OnClickListener sizeClick = new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                size = (v == btnSize512) ? 512 : (v == btnSize768) ? 768 : 1024;
                setSizeButtons();
            }
        };
        btnSize512.setOnClickListener(sizeClick);
        btnSize768.setOnClickListener(sizeClick);
        btnSize1024.setOnClickListener(sizeClick);
        btnGen.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { generate(false); }
        });
        btnRegen.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { generate(true); }
        });
        btnBack.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { finish(); }
        });

        setSizeButtons();

        rx = new BroadcastReceiver() {
            @Override
            public void onReceive(Context ctx, Intent intent) {
                handleResult(intent);
            }
        };
        // 动态注册：只在 Activity 存活期间收结果。API33+ 用 NOT_EXPORTED。
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(rx, new IntentFilter(ACTION_RESULT), Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(rx, new IntentFilter(ACTION_RESULT));
        }

        if (checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) != PackageManager.PERMISSION_GRANTED
                && checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            if (Build.VERSION.SDK_INT >= 33) {
                requestPermissions(new String[]{Manifest.permission.READ_MEDIA_IMAGES}, 43);
            } else {
                requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE}, 43);
            }
        }
        checkTermux();
    }

    private void setSizeButtons() {
        int sel = 0xFF2F6BD8, dim = 0xFF21262D;
        btnSize512.setBackgroundColor(size == 512 ? sel : dim);
        btnSize768.setBackgroundColor(size == 768 ? sel : dim);
        btnSize1024.setBackgroundColor(size == 1024 ? sel : dim);
    }

    private void checkTermux() {
        try {
            getPackageManager().getPackageInfo(TERMUX_PKG, 0);
            txtStatus.setText("Termux 已安装；点「生成」调用本地出图");
            txtStatus.setTextColor(0xFF8B949E);
        } catch (PackageManager.NameNotFoundException e) {
            txtStatus.setText("未装 Termux（F-Droid 版）。先装 Termux 并跑 setup-termux-img.sh");
            txtStatus.setTextColor(0xFFDA3633);
        }
    }

    private void generate(boolean regen) {
        if (running) { toast("上一张还没画完"); return; }
        String prompt = editPrompt.getText().toString().trim();
        if (prompt.isEmpty()) { toast("先写 prompt"); return; }
        String steps = editSteps.getText().toString().trim();
        if (steps.isEmpty()) { steps = "10"; }

        running = true;
        btnGen.setEnabled(false);
        btnRegen.setEnabled(false);
        txtStatus.setText("已交给 Termux 出图（通知栏有实时日志）…");
        txtStatus.setTextColor(0xFF8B949E);

        Intent run = new Intent(ACTION_RUN);
        run.setClassName(TERMUX_PKG, SERVICE);
        run.putExtra(EXTRA_PATH, WORKER);
        String[] args;
        if (regen) {
            // 换种子：空 seed → worker 用随机
            args = new String[]{"--prompt", prompt, "--steps", steps, "--seed", "", "--size", String.valueOf(size)};
        } else {
            args = new String[]{"--prompt", prompt, "--steps", steps, "--size", String.valueOf(size)};
        }
        run.putExtra(EXTRA_ARGS, args);
        run.putExtra(EXTRA_WORKDIR, WORKDIR);
        run.putExtra(EXTRA_BG, true);

        Intent resultIntent = new Intent(ACTION_RESULT);
        int flags = android.app.PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 31) {
            flags |= android.app.PendingIntent.FLAG_MUTABLE;
        }
        android.app.PendingIntent pi = android.app.PendingIntent.getBroadcast(this, 0, resultIntent, flags);
        run.putExtra(EXTRA_PI, pi);

        try {
            startService(run);
        } catch (Exception e) {
            running = false;
            btnGen.setEnabled(true);
            txtStatus.setText("唤起 Termux 失败：" + e.getMessage());
            txtStatus.setTextColor(0xFFDA3633);
        }
    }

    private void handleResult(Intent intent) {
        running = false;
        btnGen.setEnabled(true);
        Bundle result = intent.getBundleExtra("result");
        if (result == null) {
            txtStatus.setText("Termux 没回传结果（授权/allow-external-apps 没开？）");
            txtStatus.setTextColor(0xFFDA3633);
            return;
        }
        String stdout = result.getString("stdout", "");
        int exitCode = result.getInt("exitCode", -1);
        if (stdout == null || stdout.trim().isEmpty()) {
            String errmsg = result.getString("errmsg", "");
            txtStatus.setText("出图失败（exit " + exitCode + "）：" + errmsg);
            txtStatus.setTextColor(0xFFDA3633);
            return;
        }
        parseAndShow(stdout.trim());
    }

    private void parseAndShow(final String line) {
        worker.execute(new Runnable() {
            @Override
            public void run() {
                org.json.JSONObject o;
                try {
                    o = new org.json.JSONObject(line);
                } catch (Exception e) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            txtStatus.setText("结果不是 JSON：" + snippet(line));
                            txtStatus.setTextColor(0xFFDA3633);
                        }
                    });
                    return;
                }
                if (!o.optBoolean("ok", false)) {
                    final String err = o.optString("err", "未知原因");
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            txtStatus.setText("出图失败：" + err);
                            txtStatus.setTextColor(0xFFDA3633);
                        }
                    });
                    return;
                }
                final String path = o.optString("path", "");
                final int ms = o.optInt("ms", 0);
                final int temp = o.optInt("temp", 0);
                final int steps = o.optInt("steps", 0);
                final int outSize = o.optInt("size", size);
                final File f = path.isEmpty() ? null : new File(path);
                final Bitmap bmp = (f != null && f.isFile()) ? safeDecode(f) : null;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (bmp == null) {
                            txtStatus.setText("出图成功但图读不出来：" + path);
                            txtStatus.setTextColor(0xFFDA3633);
                            return;
                        }
                        imgResult.setImageBitmap(bmp);
                        btnRegen.setEnabled(true);
                        txtStatus.setText(String.format(
                                "完成 · %dms · %d°C · %d步 · %dx%d（已存相册 Pictures/fleet）",
                                ms, temp, steps, outSize, outSize));
                        txtStatus.setTextColor(0xFF3FB950);
                    }
                });
            }
        });
    }

    private Bitmap safeDecode(File f) {
        try {
            BitmapFactory.Options opt = new BitmapFactory.Options();
            opt.inSampleSize = 2;   // 防 OOM：展示用，2 倍足够
            return BitmapFactory.decodeFile(f.getAbsolutePath(), opt);
        } catch (Exception e) {
            Log.w(TAG, "decode fail " + e);
            return null;
        }
    }

    private static String snippet(String s) {
        if (s == null) { return "无响应"; }
        String t = s.trim();
        return t.length() > 120 ? t.substring(0, 120) + "…" : t;
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }

    @Override
    protected void onDestroy() {
        if (rx != null) {
            try { unregisterReceiver(rx); } catch (Exception ignored) { }
        }
        worker.shutdownNow();
        super.onDestroy();
    }
}
