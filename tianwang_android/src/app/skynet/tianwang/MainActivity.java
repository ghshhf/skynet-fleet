package app.skynet.tianwang;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.view.MotionEvent;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 天网 · 手机原生端主界面。
 *
 * 一条链路只有一条：
 *   录音(AudioRecord→WAV) → /api/voice/recognize → /api/chat(SSE，边收边显示) → /api/voice/say → 播放
 * 任何一环失败都把原因原样贴到字幕上，不做"出错了请重试"这种没信息量的兜底。
 *
 * 线程纪律：网络和录音都在后台；UI 只用 post 改，绝不从后台线程碰 View。
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 41;

    private OrbView orb;
    private TextView status;
    private TextView subtitle;
    private TextView btnMic;
    private TextView btnSend;
    private TextView btnSettings;
    private TextView btnPc;
    private TextView btnImg;
    private EditText input;

    private final WavRecorder recorder = new WavRecorder();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private MediaPlayer player;
    // 系统语音（本地合成）。null = 还没起或起不来；起不来时 speakerWhy 里有原因。
    private Speaker speaker;
    private String speakerWhy = "";
    private boolean recording;
    private volatile boolean busy;                 // 一轮对话进行中，不接受新输入
    private int micDownX, micDownY;
    private long micDownAt;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);

        // 本地语音自检：只报状态，绝不拦启动。
        // 模型没装好或者 ABI 不对时，App 仍要走「录音上传给电脑识别」那条老路，
        // 不能因为一个附加能力把主流程拖死。
        try {
            String mp = Whisper.defaultModelPath(this);
            File mf = new File(mp);
            android.util.Log.i("fleet-whisper", "so 版本 " + Whisper.version()
                    + "｜模型 " + (mf.isFile()
                    ? mf.length() / 1048576 + "MB " + mp
                    : "未安装（" + mp + "）"));
        } catch (Throwable t) {
            android.util.Log.w("fleet-whisper", "本地语音不可用：" + t);
        }

        orb = findViewById(R.id.orb);
        status = findViewById(R.id.status);
        subtitle = findViewById(R.id.subtitle);
        btnMic = findViewById(R.id.btnMic);
        btnSend = findViewById(R.id.btnSend);
        btnSettings = findViewById(R.id.btnSettings);
        btnPc = findViewById(R.id.btnPc);
        btnImg = findViewById(R.id.btnImg);
        input = findViewById(R.id.input);

        recorder.setLevelListener(new WavRecorder.LevelListener() {
            @Override
            public void onLevel(int level) {
                orb.setLevel(level);
            }
        });

        btnMic.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        micDownX = (int) e.getRawX();
                        micDownY = (int) e.getRawY();
                        micDownAt = System.currentTimeMillis();
                        if (!recording && !busy) { startRecord(); }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!recording) { return true; }
                        // 抬起才结束；轻微抖动不算"滑动取消"
                        boolean far = Math.abs(e.getRawX() - micDownX) > 120
                                || Math.abs(e.getRawY() - micDownY) > 120;
                        stopRecordAndAsk(far);
                        return true;
                    case MotionEvent.ACTION_CANCEL:
                        stopRecordAndAsk(true);
                        return true;
                    default:
                        return false;
                }
            }
        });

        btnSend.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                sendTyped();
            }
        });

        input.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int actionId, android.view.KeyEvent ev) {
                if (actionId == EditorInfo.IME_ACTION_SEND
                        || (ev != null && ev.getKeyCode() == android.view.KeyEvent.KEYCODE_ENTER)) {
                    sendTyped();
                    return true;
                }
                return false;
            }
        });

        btnPc.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showPcPanel();
            }
        });

        btnImg.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, ImageActivity.class));
            }
        });

        btnSettings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showSettings();
            }
        });

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }
        probeHealth();
    }

    // ---------------- 录音 ----------------

    private void startRecord() {
        if (busy) {
            toast("还在处理上一条，等一下");
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        stopPlayback();
        try {
            recorder.start();
        } catch (Exception e) {
            say2("录音起不来：" + e.getMessage());
            return;
        }
        recording = true;
        orb.setState(OrbView.LISTEN);
        btnMic.setBackgroundResource(R.drawable.bg_mic_on);
        btnMic.setText(R.string.mic_active);
        say2("在听…");
    }

    private void stopRecordAndAsk(boolean cancelled) {
        recording = false;
        btnMic.setBackgroundResource(R.drawable.bg_mic);
        btnMic.setText(R.string.mic_idle);
        final byte[] wav = recorder.stop();
        orb.setState(OrbView.IDLE);
        if (cancelled) {
            say2("取消了");
            return;
        }
        if (wav == null) {
            say2("没听到声音（太短了），再说一次");
            return;
        }
        if (System.currentTimeMillis() - micDownAt < 300) {
            say2("太短了，按住多说两个字");
            return;
        }
        say2("识别中…");
        orb.setState(OrbView.THINK);
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        setBusy(true);
        worker.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    final String text = Net.recognize(base, token, wav);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            if (TextUtils.isEmpty(text)) {
                                setBusy(false);
                                orb.setState(OrbView.IDLE);
                                say2("没听清，再说一次？");
                                return;
                            }
                            say2("你说：" + text);
                            ask(text);
                        }
                    });
                } catch (Exception e) {
                    final String msg = e.getMessage();
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            setBusy(false);
                            orb.setState(OrbView.IDLE);
                            say2("识别失败：" + msg);
                        }
                    });
                }
            }
        });
    }

    // ---------------- 打字那条路 ----------------

    private void sendTyped() {
        String t = input.getText().toString().trim();
        if (t.isEmpty()) { return; }
        if (busy) {
            toast("还在处理上一条，等一下");
            return;
        }
        input.setText("");
        say2("你说：" + t);
        ask(t);
    }

    // ---------------- 对话 + 合成 + 播放 ----------------

    private void ask(final String prompt) {
        stopPlayback();
        setBusy(true);
        orb.setState(OrbView.THINK);
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        final String backend = Prefs.backend(this);
        final StringBuilder acc = new StringBuilder();

        worker.execute(new Runnable() {
            @Override
            public void run() {
                Net.chat(base, token, prompt, backend, new Net.SseCallback() {
                    @Override
                    public void onLine(final String text) {
                        acc.append(text);
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                say2("它说：" + acc);
                            }
                        });
                    }

                    @Override
                    public void onError(final String message) {
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                setBusy(false);
                                orb.setState(OrbView.IDLE);
                                say2("对话失败：" + message);
                            }
                        });
                    }

                    @Override
                    public void onDone() {
                        String reply = acc.toString().trim();
                        if (reply.isEmpty()) {
                            ui.post(new Runnable() {
                                @Override
                                public void run() {
                                    setBusy(false);
                                    orb.setState(OrbView.IDLE);
                                    say2("它没说话（空回复）");
                                }
                            });
                            return;
                        }
                        speak(reply);
                    }
                });
            }
        });
    }

    private void speak(final String text) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                orb.setState(OrbView.SPEAK);
            }
        });
        worker.execute(new Runnable() {
            @Override
            public void run() {
                // 先试系统语音：本机几毫秒起播、不走网络、断网也能念。
                Speaker sp = systemSpeaker();
                if (sp != null) {
                    sp.say(text, new Runnable() {
                        @Override
                        public void run() {
                            ui.post(new Runnable() {
                                @Override
                                public void run() {
                                    setBusy(false);
                                    orb.setState(OrbView.IDLE);
                                }
                            });
                        }
                    });
                    return;
                }
                // 系统语音起不来：不静默降级，把原因写在字幕上再走电脑合成。
                final String why = speakerWhy;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        say2("它说：" + text + "\n（系统语音不可用，改用电脑合成：" + why + "）");
                    }
                });
                speakOnPc(text);
            }
        });
    }

    /** 懒起系统语音引擎（只试一次；失败就把原因记下来，不要每句都重试）。 */
    private Speaker systemSpeaker() {
        if (speaker != null) { return speaker; }
        if (!speakerWhy.isEmpty()) { return null; }
        Speaker sp = new Speaker();
        Speaker.NotReady nr = sp.open(this, 4000);
        if (nr != null) {
            speakerWhy = nr.why;
            sp.close();
            return null;
        }
        speaker = sp;
        return speaker;
    }

    private void speakOnPc(final String text) {
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        worker.execute(new Runnable() {
            @Override
            public void run() {
                byte[] wav = null;
                String ext = "wav";
                String err = null;
                try {
                    Net.SayResult r = Net.say(base, token, text);
                    wav = r.data;
                    ext = r.ext;
                } catch (Exception e) {
                    err = e.getMessage();
                }
                final byte[] data = wav;
                final String fmt = ext;
                final String why = err;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (data == null) {
                            setBusy(false);
                            orb.setState(OrbView.IDLE);
                            say2("它说：" + text + "\n（合成失败，没出声：" + why + "）");
                            return;
                        }
                        play(data, fmt, text);
                    }
                });
            }
        });
    }

    private void play(byte[] wav, String ext, final String text) {
        try {
            // 后缀必须跟真实容器一致：MIUI 的 MediaPlayer 会看后缀决定解复用器，
            // 把 mp3 存成 tts.wav 在部分 ROM 上直接 error(1, -2147483648)。
            File f = new File(getCacheDir(), "tts." + (ext == null ? "wav" : ext));
            FileOutputStream fo = new FileOutputStream(f);
            fo.write(wav);
            fo.close();
            stopPlayback();
            player = new MediaPlayer();
            player.setDataSource(f.getAbsolutePath());
            player.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer mp) {
                    cleanupPlayer();
                    setBusy(false);
                    orb.setState(OrbView.IDLE);
                }
            });
            player.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer mp, int what, int extra) {
                    cleanupPlayer();
                    setBusy(false);
                    orb.setState(OrbView.IDLE);
                    say2("播放失败（what=" + what + " extra=" + extra + "）");
                    return true;
                }
            });
            player.prepare();
            player.start();
        } catch (Exception e) {
            setBusy(false);
            orb.setState(OrbView.IDLE);
            say2("播放失败：" + e.getMessage());
        }
    }

    private void stopPlayback() {
        if (speaker != null) { speaker.stop(); }
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) { }
            cleanupPlayer();
        }
    }

    private void cleanupPlayer() {
        if (player != null) {
            try { player.release(); } catch (Exception ignored) { }
            player = null;
        }
    }

    // ---------------- 状态与设置 ----------------

    private void setBusy(boolean v) {
        busy = v;
        btnMic.setAlpha(v ? 0.45f : 1f);
        btnSend.setAlpha(v ? 0.45f : 1f);
    }

    private void say2(String s) {
        subtitle.setText(s);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    private void probeHealth() {
        final String base = Prefs.baseUrl(this);
        status.setText("连接 " + base + " …");
        status.setTextColor(Color.parseColor("#8B949E"));
        worker.execute(new Runnable() {
            @Override
            public void run() {
                final String r = Net.health(base);
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (r == null) {
                            status.setText("连不上 " + base);
                            status.setTextColor(Color.parseColor("#DA3633"));
                        } else {
                            status.setText("已连上 " + base);
                            status.setTextColor(Color.parseColor("#3FB950"));
                        }
                    }
                });
            }
        });
    }

    private void showSettings() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (getResources().getDisplayMetrics().density * 20);
        box.setPadding(pad, pad / 2, pad, 0);

        final EditText eBase = new EditText(this);
        eBase.setHint("后端地址，例如 http://127.0.0.1:8000");
        eBase.setText(Prefs.baseUrl(this));
        eBase.setTextSize(13);
        box.addView(eBase);

        final EditText eToken = new EditText(this);
        eToken.setHint("令牌（电脑上 --link 打印的那枚）");
        eToken.setText(Prefs.token(this));
        eToken.setTextSize(13);
        box.addView(eToken);

        final EditText eBackend = new EditText(this);
        eBackend.setHint("AI 后端（默认 pollinations；留空=全部）");
        eBackend.setText(Prefs.backend(this));
        eBackend.setTextSize(13);
        box.addView(eBackend);

        TextView tip = new TextView(this);
        tip.setText("隧道模式（默认，推荐）\n"
                + "  电脑：python fleet_gui.py --link\n"
                + "  这里填 http://127.0.0.1:8000 + 电脑上打印的令牌\n\n"
                + "局域网模式（不想插线时才用）\n"
                + "  电脑：set FLEET_GUI_TOKEN=xxx 然后 python fleet_gui.py --lan\n"
                + "  这里填 http://<电脑IP>:8000 + 上面那串令牌");
        tip.setTextSize(11);
        tip.setTextColor(Color.parseColor("#8B949E"));
        box.addView(tip);

        new AlertDialog.Builder(this)
                .setTitle("连接设置")
                .setView(box)
                .setPositiveButton("保存", new android.content.DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(android.content.DialogInterface d, int w) {
                        Prefs.save(MainActivity.this,
                                eBase.getText().toString(),
                                eToken.getText().toString(),
                                eBackend.getText().toString());
                        probeHealth();
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == REQ_MIC && (res.length == 0 || res[0] != PackageManager.PERMISSION_GRANTED)) {
            say2("没给录音权限，只能打字了");
        }
    }

    @Override
    protected void onPause() {
        if (recording) { stopRecordAndAsk(true); }
        stopPlayback();
        setBusy(false);
        orb.setState(OrbView.IDLE);
        super.onPause();
    }

    // ---------------- 控电脑 ----------------

    /**
     * 拉电脑的全能力清单 → 选一项 → 就地调用。
     *
     * 为什么要“参数”输入框：87 项里很多项要参数（装哪个包、点哪里、写什么字）。
     * 不做 87 个专属表单，而是给一个 JSON 框——无参的填 {} 就行，要参的自己填。
     * 这比“只能点无参的”诚实，也比现在就堆一堆表单现实。
     */
    private void showPcPanel() {
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        say2("正在从电脑拉能力清单…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                org.json.JSONArray rows = null;
                String err = null;
                try {
                    org.json.JSONObject o = Net.caps(base, token);
                    rows = o.optJSONArray("caps");
                } catch (Exception e) {
                    err = e.getMessage();
                }
                final org.json.JSONArray r = rows;
                final String e2 = err;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (e2 != null) {
                            say2("拉清单失败：" + e2);
                            return;
                        }
                        if (r == null || r.length() == 0) {
                            say2("电脑那头没有注册任何能力");
                            return;
                        }
                        renderPcPanel(r);
                    }
                });
            }
        });
    }

    private void renderPcPanel(final org.json.JSONArray rows) {
        final String[] names = new String[rows.length()];
        final String[] labels = new String[rows.length()];
        for (int i = 0; i < rows.length(); i++) {
            org.json.JSONObject o = rows.optJSONObject(i);
            names[i] = o == null ? "" : o.optString("name");
            String title = o == null ? "" : o.optString("title");
            String domain = o == null ? "" : o.optString("domain");
            labels[i] = names[i] + (title.isEmpty() ? "" : "　" + title)
                    + (domain.isEmpty() ? "" : "　[" + domain + "]");
        }

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (getResources().getDisplayMetrics().density * 12);
        box.setPadding(pad, pad, pad, 0);

        TextView tip = new TextView(this);
        tip.setText("参数（JSON，无参就留 {}）；要审批的会在电脑上弹窗等确认");
        tip.setTextSize(10);
        tip.setTextColor(Color.parseColor("#8B949E"));
        box.addView(tip);

        final EditText eArgs = new EditText(this);
        eArgs.setText("{}");
        eArgs.setTextSize(12);
        eArgs.setSingleLine(true);
        box.addView(eArgs);

        android.widget.ListView list = new android.widget.ListView(this);
        list.setAdapter(new android.widget.ArrayAdapter<String>(
                this, android.R.layout.simple_list_item_1, labels));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f);
        box.addView(list, lp);

        final AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("控电脑 · " + rows.length() + " 项能力")
                .setView(box)
                .setNegativeButton("关掉", null)
                .create();

        list.setOnItemClickListener(new android.widget.AdapterView.OnItemClickListener() {
            @Override
            public void onItemClick(android.widget.AdapterView<?> p, View v, int i, long id) {
                org.json.JSONObject args;
                try {
                    String s = eArgs.getText().toString().trim();
                    args = s.isEmpty() ? new org.json.JSONObject() : new org.json.JSONObject(s);
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this,
                            "参数不是合法 JSON：" + e.getMessage(), Toast.LENGTH_LONG).show();
                    return;
                }
                dlg.dismiss();
                runOnPc(names[i], args);
            }
        });
        dlg.show();
    }

    private void runOnPc(final String cap, final org.json.JSONObject args) {
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        setBusy(true);
        say2("让电脑做：" + cap + "\n（要审批的话在电脑上点确认…）");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                String text;
                try {
                    org.json.JSONObject r = Net.call(base, token, cap, args);
                    boolean ok = r.optBoolean("ok");
                    String out = ok ? r.optString("err", "") : r.optString("err", "");
                    String data = r.isNull("data") ? "" : String.valueOf(r.opt("data"));
                    text = (ok ? "✓ " : "✗ ") + cap
                            + (out.isEmpty() ? "" : "\n" + out)
                            + (data.isEmpty() || "null".equals(data) ? "" : "\n" + data);
                    if (text.length() > 1200) { text = text.substring(0, 1200) + "…"; }
                } catch (Exception e) {
                    text = "✗ " + cap + "\n" + e.getMessage();
                }
                final String t = text;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        setBusy(false);
                        say2(t);
                    }
                });
            }
        });
    }

    @Override
    protected void onDestroy() {
        stopPlayback();
        if (speaker != null) { speaker.close(); speaker = null; }
        worker.shutdownNow();
        super.onDestroy();
    }
}
