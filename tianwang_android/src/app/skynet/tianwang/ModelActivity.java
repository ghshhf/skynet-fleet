package app.skynet.tianwang;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.text.DecimalFormat;

/**
 * 天网 · 模型库（App 内本地下载界面）。
 *
 * 列出 ModelManager 全量档（出图 SD + 4B 级 LLM/VL），每行：标签 / 类别·体积 /
 * 状态 / 进度条 / 下载按钮。还能粘一条 Hugging Face 直链当自定义模型入队。
 *
 * 下载走 ModelDownloader：后台线程、断点续传、系统代理直连、完整性校验。
 * UI 纪律：回调只在主线程改 View（ModelDownloader 已用 Handler post 保证）。
 */
public class ModelActivity extends Activity {

    private static final String TAG = "fleet-models";
    private LinearLayout list;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_models);
        list = findViewById(R.id.listModels);
        final EditText url = findViewById(R.id.editUrl);
        Button add = findViewById(R.id.btnAdd);
        add.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                String u = url.getText().toString().trim();
                if (u.isEmpty()) { toast("先粘贴 Hugging Face 直链"); return; }
                enqueueCustom(u);
                url.setText("");
            }
        });
        render();
    }

    /** 依据已装/残缺/未下，给每行刷新状态与按钮文案。 */
    private void refreshRow(final ModelManager.Spec s, final Button btn, final TextView status) {
        File f = ModelManager.fileOf(this, s);
        boolean have = f.isFile() && f.length() >= ModelManager.MIN_BYTES;
        if (have) {
            status.setText("✔ 已装 " + human(f.length()));
            status.setTextColor(Color.parseColor("#3FB950"));
            btn.setText("重新下载");
        } else if (f.isFile()) {
            status.setText("⏳ 残缺 " + human(f.length()) + "（可续传）");
            status.setTextColor(Color.parseColor("#D29922"));
            btn.setText("继续下载");
        } else {
            status.setText("未下载");
            status.setTextColor(Color.parseColor("#8B949E"));
            btn.setText("下载");
        }
    }

    private void render() {
        list.removeAllViews();
        for (ModelManager.Spec s : ModelManager.allSpecs()) {
            addRow(s);
        }
    }

    /** 为单个 Spec 生成一行：标签行 + 元信息 + 进度条 + 按钮。 */
    private void addRow(final ModelManager.Spec s) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        row.setPadding(0, 0, 0, 16);

        LinearLayout line1 = new LinearLayout(this);
        line1.setOrientation(LinearLayout.HORIZONTAL);
        TextView label = new TextView(this);
        label.setText(s.label);
        label.setTextColor(Color.parseColor("#C9D1D9"));
        label.setTextSize(14);
        label.setLayoutParams(new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        final TextView status = new TextView(this);
        status.setTextSize(12);
        line1.addView(label);
        line1.addView(status);

        TextView meta = new TextView(this);
        meta.setTextSize(11);
        meta.setTextColor(Color.parseColor("#8B949E"));
        meta.setText(kindLabel(s.kind)
                + (s.sizeBytes > 0 ? " · " + human(s.sizeBytes) : " · 体积未知"));

        final ProgressBar pb = new ProgressBar(this, null,
                android.R.attr.progressBarStyleHorizontal);
        pb.setMax(100);
        pb.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 8));
        pb.setVisibility(View.GONE);

        final Button btn = new Button(this);
        btn.setTextSize(13);
        btn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                File f = ModelManager.fileOf(ModelActivity.this, s);
                if (f.isFile() && f.length() >= ModelManager.MIN_BYTES) {
                    toast(s.label + " 已就绪");
                } else {
                    download(s, btn, pb, status);
                }
            }
        });

        row.addView(line1);
        row.addView(meta);
        row.addView(pb);
        row.addView(btn);
        list.addView(row);
        refreshRow(s, btn, status);
    }

    private void download(final ModelManager.Spec s, final Button btn,
                          final ProgressBar pb, final TextView status) {
        btn.setEnabled(false);
        pb.setVisibility(View.VISIBLE);
        pb.setProgress(0);
        ModelDownloader.enqueue(this, s, new ModelDownloader.Progress() {
            @Override public void onStart(long total) {
                ui.post(new Runnable() { @Override public void run() {
                    status.setText("开始下载…"); status.setTextColor(Color.parseColor("#8B949E"));
                }});
            }
            @Override public void onProgress(long done, long total, float speedBps) {
                final int p = total > 0 ? (int) (done * 100 / total) : 0;
                ui.post(new Runnable() { @Override public void run() {
                    pb.setProgress(p);
                    String t = human(done) + (total > 0 ? " / " + human(total) : "")
                            + " · " + human((long) speedBps) + "/s";
                    status.setText(t);
                }});
            }
            @Override public void onDone(File file) {
                ui.post(new Runnable() { @Override public void run() {
                    btn.setEnabled(true); pb.setProgress(100);
                    refreshRow(s, btn, status);
                    toast(s.label + " 下载完成");
                }});
            }
            @Override public void onError(String msg) {
                ui.post(new Runnable() { @Override public void run() {
                    btn.setEnabled(true);
                    status.setText("✗ " + msg);
                    status.setTextColor(Color.parseColor("#DA3633"));
                }});
            }
        });
    }

    /** 自定义 Hugging Face 直链：抽文件名入队，下完刷新列表。 */
    private void enqueueCustom(final String url) {
        final String name = ModelDownloader.filenameFromUrl(url);
        ModelDownloader.enqueueCustom(this, url, name, ModelManager.KIND_CUSTOM, 0,
                new ModelDownloader.Progress() {
                    @Override public void onStart(long total) { }
                    @Override public void onProgress(long done, long total, float speedBps) { }
                    @Override public void onDone(File file) {
                        ui.post(new Runnable() { @Override public void run() {
                            toast("已下载：" + file.getName()); render();
                        }});
                    }
                    @Override public void onError(String msg) {
                        ui.post(new Runnable() { @Override public void run() {
                            toast("下载失败：" + msg);
                        }});
                    }
                });
    }

    private static String kindLabel(String k) {
        if (ModelManager.KIND_SD.equals(k)) return "出图(SD)";
        if (ModelManager.KIND_LLM.equals(k)) return "语言(LLM)";
        if (ModelManager.KIND_VL.equals(k)) return "视觉(VL)";
        return "自定义";
    }

    private static String human(long b) {
        if (b <= 0) return "0B";
        double v = b; String[] u = {"B", "KB", "MB", "GB", "TB"}; int i = 0;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return new DecimalFormat("#.##").format(v) + u[i];
    }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }
}
