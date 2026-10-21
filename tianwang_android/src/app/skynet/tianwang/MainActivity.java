package app.skynet.tianwang;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
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
    private EditText input;

    private final WavRecorder recorder = new WavRecorder();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private MediaPlayer player;
    private boolean recording;
    private volatile boolean busy;                 // 一轮对话进行中，不接受新输入
    private int micDownX, micDownY;
    private long micDownAt;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);

        orb = findViewById(R.id.orb);
        status = findViewById(R.id.status);
        subtitle = findViewById(R.id.subtitle);
        btnMic = findViewById(R.id.btnMic);
        btnSend = findViewById(R.id.btnSend);
        btnSettings = findViewById(R.id.btnSettings);
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
        final StringBuilder acc = new StringBuilder();

        worker.execute(new Runnable() {
            @Override
            public void run() {
                Net.chat(base, token, prompt, new Net.SseCallback() {
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
        final String base = Prefs.baseUrl(this), token = Prefs.token(this);
        ui.post(new Runnable() {
            @Override
            public void run() {
                orb.setState(OrbView.SPEAK);
            }
        });
        worker.execute(new Runnable() {
            @Override
            public void run() {
                byte[] wav = null;
                String err = null;
                try {
                    wav = Net.say(base, token, text);
                } catch (Exception e) {
                    err = e.getMessage();
                }
                final byte[] data = wav;
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
                        play(data, text);
                    }
                });
            }
        });
    }

    private void play(byte[] wav, final String text) {
        try {
            File f = new File(getCacheDir(), "tts.wav");
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
        eToken.setHint("令牌（电脑上 FLEET_GUI_TOKEN 那枚）");
        eToken.setText(Prefs.token(this));
        eToken.setTextSize(13);
        box.addView(eToken);

        TextView tip = new TextView(this);
        tip.setText("电脑上执行：\nadb reverse tcp:8000 tcp:8000\n"
                + "再设 FLEET_GUI_TOKEN=<任意串> 启动后端，这里填同一串。");
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
                                eBase.getText().toString(), eToken.getText().toString());
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

    @Override
    protected void onDestroy() {
        stopPlayback();
        worker.shutdownNow();
        super.onDestroy();
    }
}
