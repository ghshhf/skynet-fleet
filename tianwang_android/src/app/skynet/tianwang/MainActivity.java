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
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import org.json.JSONObject;

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

    private static final String TAG = "tianwang";
    private static final int REQ_MIC = 41;

    private OrbView orb;
    private TextView status;
    private TextView subtitle;
    private TextView btnMic;
    private TextView btnSend;
    private TextView btnSettings;
    private TextView btnPc;
    private TextView btnImg;
    private TextView btnDev;
    private TextView btnModels;
    private TextView btnRadio;
    private TextView btnThermal;
    private TextView btnPair;
    private TextView btnClear;
    private EditText input;
    private ScrollView chatScroll;
    private LinearLayout chatList;
    /** 多轮上下文：只留最近的来回。放在内存里，不落盘——
     *  这段是「这次会话在聊什么」，不是「主人是谁」。后者属于人格记忆包。 */
    private final java.util.ArrayList<JSONObject> history = new java.util.ArrayList<>();
    private static final int HISTORY_MAX = 12;
    /** 正在流式更新的那个气泡；null = 当前没有在生成的回复。 */
    private TextView streaming;

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
        btnDev = findViewById(R.id.btnDev);
        btnModels = findViewById(R.id.btnModels);
        btnRadio = findViewById(R.id.btnRadio);
        btnThermal = findViewById(R.id.btnThermal);
        btnPair = findViewById(R.id.btnPair);
        btnClear = findViewById(R.id.btnClear);
        input = findViewById(R.id.input);
        chatScroll = findViewById(R.id.chatScroll);
        chatList = findViewById(R.id.chatList);
        addHint("这上面是对话：在下面的框里打字，或按住大圆说话。\n"
                + "说的话会留在这儿，往上翻能看到前几轮。");

        // 版本号来自 APK 本身（aapt2 link --version-name/--version-code），
        // 不写死在代码里——写死就会出现「代码改了但包没换」还说不清的状况。
        TextView ver = findViewById(R.id.ver);
        try {
            android.content.pm.PackageInfo pi =
                    getPackageManager().getPackageInfo(getPackageName(), 0);
            final String v = pi.versionName + " (build " + pi.versionCode + ")";
            ver.setText(v);
            Log.i(TAG, "天网版本 " + v);
        } catch (Exception e) {
            ver.setText("");
        }

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

        btnDev.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, DeviceCheckActivity.class));
            }
        });

        btnSettings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showSettings();
            }
        });

        btnModels.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, ModelActivity.class));
            }
        });

        btnRadio.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                scanRadio();
            }
        });

        btnThermal.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                checkThermal();
            }
        });

        btnPair.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showPairDialog();
            }
        });

        btnClear.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                chatList.removeAllViews();
                history.clear();
                addHint("清屏了，上下文也一起清了。");
                say2("清屏了，上下文也一起清了");
            }
        });

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }
        probeHealth();

        // 无人值守基准：PC 侧 am start 本 Activity（LAUNCHER，实测不会被小米的
        // "后台弹出界面"拦掉）带 bench=true，这里直接开跑，结果进 logcat。
        // 存在的意义：性能排查要跑很多轮，不能每轮都让人在手机上点一次。
        if (getIntent().getBooleanExtra("bench", false)) {
            startBench(getIntent().getIntExtra("steps", 2),
                    getIntent().getIntExtra("size", 512),
                    getIntent().getStringExtra("model"));
        }
        // 队列无人值守测试：`--es prompts "a cat;;a dog"`（用 ;; 分隔，命令行塞换行太麻烦）
        if (getIntent().getBooleanExtra("queue", false)) {
            startQueue(getIntent().getStringExtra("prompts"),
                    getIntent().getIntExtra("steps", 4),
                    getIntent().getIntExtra("size", 512));
        }
    }

    private void startQueue(String prompts, final int steps, final int size) {
        if (prompts == null || prompts.trim().isEmpty()) {
            Log.e(TAG, "QUEUE_SKIP 没给 prompts");
            return;
        }
        final java.util.List<QueueStore.Item> queue = new java.util.ArrayList<QueueStore.Item>();
        String[] lines = prompts.split(";;");
        ModelManager.Spec spec = ModelManager.current(this);
        long base = System.currentTimeMillis();
        for (int i = 0; i < lines.length; i++) {
            String p = lines[i].trim();
            if (p.isEmpty()) continue;
            QueueStore.Item it = new QueueStore.Item();
            it.id = base + i;
            it.prompt = p;
            it.size = size;
            it.steps = steps;
            it.seed = base + i;
            it.cfg = spec.cfg;
            it.sampler = spec.sampler;
            it.model = spec.file;
            it.tier = "STD";
            it.status = QueueStore.PENDING;
            it.createdAt = System.currentTimeMillis();
            queue.add(it);
        }
        QueueStore.save(this, queue);
        Log.i(TAG, "QUEUE_START " + queue.size() + " 张：" + QueueStore.summary(queue));
        worker.execute(new Runnable() {
            @Override public void run() {
                boolean all = QueueRunner.run(MainActivity.this, queue, null, null,
                        new QueueRunner.Sink() {
                            @Override public void onProgress(String line) {
                                Log.i(TAG, line);
                            }
                        });
                Log.i(TAG, all ? "QUEUE_ALL_OK" : "QUEUE_HAD_FAILURE");
            }
        });
    }

    private void startBench(final int steps, final int size, final String model) {
        java.util.List<ModelManager.Spec> av = ModelManager.availableSd(this);
        if (av.isEmpty()) {
            Log.e(TAG, "BENCH_SKIP 没有可用模型");
            return;
        }
        // 能指定模型才有 A/B 可言：同一个 steps 下 SD1.5 和 Turbo 差一个数量级，
        // 不指定就只能测到"第一个"，比较不出东西。
        ModelManager.Spec pick = av.get(0);
        if (model != null) {
            for (ModelManager.Spec s : av) {
                if (s.file.equals(model)) { pick = s; break; }
            }
        }
        final ModelManager.Spec spec = pick;
        Log.i(TAG, "BENCH_START model=" + spec.file + " size=" + size + " steps=" + steps);
        worker.execute(new Runnable() {
            @Override public void run() {
                BenchRunner.run(MainActivity.this, spec, size, steps,
                        new BenchRunner.Sink() {
                            @Override public void onLog(String line) {
                                Log.i(TAG, line);
                            }
                        });
            }
        });
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
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        setBusy(true);
        worker.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    final String text = Net.recognize(base, auth, wav);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            if (TextUtils.isEmpty(text)) {
                                setBusy(false);
                                orb.setState(OrbView.IDLE);
                                say2("没听清，再说一次？");
                                return;
                            }
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
        ask(t);
    }

    // ---------------- 对话 + 合成 + 播放 ----------------

    private void ask(final String prompt) {
        stopPlayback();
        setBusy(true);
        orb.setState(OrbView.THINK);
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        final String backend = Prefs.backend(this);
        final StringBuilder acc = new StringBuilder();

        // 先把「我说的」钉到屏幕上，再看它怎么答。
        // 顺序反了会出现「它答完才看见我问了啥」，很像它答错人。
        appendMsg(true, prompt);
        say2("在想…");
        final TextView bubble = appendMsg(false, "…");
        streaming = bubble;

        worker.execute(new Runnable() {
            @Override
            public void run() {
                Net.chat(base, auth, prompt, backend, snapshotHistory(), new Net.SseCallback() {
                    @Override
                    public void onLine(final String text) {
                        acc.append(text);
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                // 直接改气泡文字，不走 say2：字幕只放过程，
                                // 正文进气泡——两者混用会让一屏信息全废。
                                bubble.setText(cleanTags(acc.toString()));
                                scrollToBottom();
                            }
                        });
                    }

                    @Override
                    public void onError(final String message) {
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                setBusy(false);
                                streaming = null;
                                orb.setState(OrbView.IDLE);
                                if ("…".contentEquals(bubble.getText())) {
                                    bubble.setText("（没答上来）");
                                }
                                say2("对话失败：" + message);
                            }
                        });
                    }

                    @Override
                    public void onDone() {
                        String reply = cleanTags(acc.toString().trim());
                        if (reply.isEmpty()) {
                            ui.post(new Runnable() {
                                @Override
                                public void run() {
                                    setBusy(false);
                                    streaming = null;
                                    orb.setState(OrbView.IDLE);
                                    bubble.setText("（空回复）");
                                    say2("它没说话（空回复）");
                                }
                            });
                            return;
                        }
                        // 只有真的答出来了才进上下文：空回复/报错不该污染下一轮
                        pushHistory(prompt, reply);
                        streaming = null;
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                bubble.setText(reply);
                                scrollToBottom();
                            }
                        });
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
                say2("在说…");
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
                                    // 念完把字幕清回空闲：留在"在想…"会让人以为还在跑
                                    say2(getString(R.string.idle_sub));
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
                        say2("系统语音不可用，改用电脑合成（" + why + "）");
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
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        worker.execute(new Runnable() {
            @Override
            public void run() {
                byte[] wav = null;
                String ext = "wav";
                String err = null;
                try {
                    Net.SayResult r = Net.say(base, auth, text);
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
                            say2("合成失败，没出声（" + why + "）");
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

    // ---------------- 对话流 ----------------

    /**
     * 往对话流里加一条。返回那个 TextView，调用方可以继续改文字（流式）。
     *
     * 为什么不用 RecyclerView：一屏最多十几条、每次会话清空。
     * 为这点数据引一个 Adapter + ViewHolder，代价远大于收益。
     */
    private TextView appendMsg(boolean mine, String text) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView t = new TextView(this);
        t.setText(text);
        t.setTextIsSelectable(true);       // 能选中复制，比"长按整条复制"更顺手
        t.setTextSize(14f);
        t.setTextColor(getResources().getColor(R.color.text, getTheme()));
        t.setLineSpacing(dp(3), 1f);
        t.setPadding(dp(13), dp(9), dp(13), dp(9));
        t.setBackgroundResource(mine ? R.drawable.bg_bubble_me
                                     : R.drawable.bg_bubble_ai);
        // 限宽用 maxWidth 而不是 LayoutParams.width：
        // 指定 width 会把气泡拉成一条满宽的色块，左右分边就看不出来了。
        t.setMaxWidth(dp(300));

        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        tp.gravity = mine ? android.view.Gravity.END : android.view.Gravity.START;
        tp.topMargin = dp(6);
        row.addView(t, tp);
        chatList.addView(row);
        scrollToBottom();
        return t;
    }

    /**
     * 去掉流里的后端标签 `[pollinations]`。
     *
     * 只在手机端做：电脑上多后端同屏对照需要这个标签，手机上只念一路，
     * 留着就变成"每句前面挂个方括号"，读起来像乱码。
     */
    private static String cleanTags(String s) {
        if (s == null) { return ""; }
        return s.replaceAll("\\[[a-z0-9][a-z0-9_.\\-]{0,24}\\]\\s*", "");
    }

    private int dp(int v) {
        return (int) (getResources().getDisplayMetrics().density * v);
    }

    /**
     * 扫频：把手机当频谱仪。
     *
     * 为什么这件事非手机不可：电脑的无线网卡被它自己开的移动热点占满了，
     * 系统就不再扫周边 AP —— 电脑侧永远只看得见「已经连上的那个网」。
     * 手机连着热点还能同时扫，周边有多少热点、都在哪个频段，只有它看得见。
     *
     * 为什么不用服务端那张等宽表格：手机是比例字体，等宽表一贴就散架，
     * 看着像乱码。这里重新排成「一行一个热点」，宁可信息少一点也要能读。
     */
    private void scanRadio() {
        final String base = Prefs.baseUrl(this);
        if (!Prefs.paired(this)) {
            addHint("还没配对。先点「配对」换一张长期凭据，再扫。");
            return;
        }
        final Net.Auth auth = Net.auth(this);
        say2("扫频中，约 8 秒…");
        final TextView bubble = appendMsg(false, "正在让手机扫周边频段…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                String text;
                try {
                    JSONObject r = Net.call(base, auth, "radio.wifi.survey",
                            new JSONObject().put("rounds", 1));
                    text = renderSurvey(r.optJSONObject("data"));
                } catch (Exception e) {
                    text = "扫频失败：" + e.getMessage();
                }
                final String out = text;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        bubble.setText(out);
                        scrollToBottom();
                        say2("扫完了");
                    }
                });
            }
        });
    }

    /** 把 radio.wifi.survey 的 data 排成人能读的几行。 */
    private static String renderSurvey(JSONObject d) {
        if (d == null) {
            return "扫频没有返回数据。";
        }
        org.json.JSONArray aps = d.optJSONArray("aps");
        if (aps == null || aps.length() == 0) {
            return "没扫到热点（射频被占或扫描被系统限流，过几秒再试）。";
        }
        StringBuilder sb = new StringBuilder();
        sb.append("周边 ").append(d.optInt("count")).append(" 个热点");
        JSONObject bands = d.optJSONObject("bands");
        if (bands != null && bands.length() > 0) {
            sb.append("（");
            java.util.Iterator<String> it = bands.keys();
            boolean first = true;
            while (it.hasNext()) {
                String k = it.next();
                if (!first) { sb.append(" / "); }
                sb.append(k).append(" ").append(bands.optInt(k));
                first = false;
            }
            sb.append("）");
        }
        int n = Math.min(aps.length(), 10);
        for (int i = 0; i < n; i++) {
            JSONObject a = aps.optJSONObject(i);
            if (a == null) { continue; }
            String ssid = a.optString("ssid", "");
            if (ssid.length() == 0) { ssid = "(隐藏)"; }
            sb.append("\n").append(a.optString("band", "?"))
              .append(" ch").append(a.optInt("channel"))
              .append("  ").append(a.optInt("rssi")).append("dBm  ")
              .append(ssid);
        }
        if (aps.length() > n) {
            sb.append("\n…还有 ").append(aps.length() - n).append(" 个");
        }
        return sb.toString();
    }

    /** 体温：查热状态。手机被动散热，SOC 到 85℃ 就压频——
     *  「刚才还快，现在怎么慢了」八成就是它，不查就会去乱猜量化/线程数。 */
    private void checkThermal() {
        final String base = Prefs.baseUrl(this);
        if (!Prefs.paired(this)) {
            addHint("还没配对。先点「配对」换一张长期凭据，再查。");
            return;
        }
        final Net.Auth auth = Net.auth(this);
        final TextView bubble = appendMsg(false, "正在读热状态…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                String text;
                try {
                    JSONObject r = Net.call(base, auth, "thermal.read", new JSONObject());
                    text = renderThermal(r.optJSONObject("data"));
                } catch (Exception e) {
                    text = "读热状态失败：" + e.getMessage();
                }
                final String out = text;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        bubble.setText(out);
                        scrollToBottom();
                    }
                });
            }
        });
    }

    /** 把 thermal.read 的 data 排成几行。手机是比例字体，不用等宽表。 */
    private static String renderThermal(JSONObject d) {
        if (d == null) {
            return "热状态没有返回数据。";
        }
        StringBuilder sb = new StringBuilder();
        String st = d.optString("thermal_status_name", "?");
        sb.append("热状态 ").append(st)
          .append("（").append(d.optString("thermal_status_cn", "")).append("）");
        JSONObject temps = d.optJSONObject("temps");
        if (temps != null) {
            String cpu = temps.optString("CPU", "");
            String soc = temps.optString("SOC", "");
            if (cpu.length() > 0) { sb.append("   CPU ").append(cpu).append("℃"); }
            if (soc.length() > 0) { sb.append("  SOC ").append(soc).append("℃"); }
        }
        double head = d.optDouble("cpu_headroom_c", 999);
        if (head < 900) {
            sb.append("\n距首档节流线还有 ").append(Math.round(head * 10) / 10.0).append("℃");
        }
        JSONObject cpu = d.optJSONObject("cpu");
        if (cpu != null) {
            org.json.JSONArray cl = cpu.optJSONArray("clusters");
            if (cl != null && cl.length() > 0) {
                sb.append("\n频率 ");
                for (int i = 0; i < cl.length(); i++) {
                    JSONObject c = cl.optJSONObject(i);
                    if (c == null) { continue; }
                    if (i > 0) { sb.append("  "); }
                    sb.append("cpu").append(c.optInt("cpu")).append(" ")
                      .append(c.optInt("cur_mhz")).append("/")
                      .append(c.optInt("max_mhz")).append("MHz");
                }
            }
        }
        JSONObject b = d.optJSONObject("battery");
        if (b != null) {
            sb.append("\n电池 ").append(b.optInt("level")).append("%  ")
              .append(b.optDouble("temp_c", 0)).append("℃  ")
              .append(b.optBoolean("charging") ? "充电中" : "放电中");
        }
        JSONObject mem = d.optJSONObject("mem");
        if (mem != null) {
            sb.append("\n内存 可用 ").append(mem.optInt("avail_mb")).append("MB / ")
              .append(mem.optInt("total_mb")).append("MB");
        }
        int util = d.optInt("cpu_util_pct", -1);
        if (util >= 0) {
            sb.append("   CPU 利用率 ").append(util).append("%");
        }
        String verdict = d.optString("verdict", "");
        if (verdict.length() > 0) {
            sb.append("\n\n").append(verdict);
        }
        return sb.toString();
    }

    /** 空对话时的一句提示。用暗色居中，不伪装成气泡——
     *  「没话说」和「它回了这句」必须一眼能分清。 */
    private void addHint(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(12f);
        t.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        t.setGravity(android.view.Gravity.CENTER);
        t.setPadding(dp(18), dp(20), dp(18), 0);
        t.setLineSpacing(dp(3), 1f);
        chatList.addView(t);
    }

    private void scrollToBottom() {
        chatScroll.post(new Runnable() {
            @Override
            public void run() {
                chatScroll.fullScroll(View.FOCUS_DOWN);
            }
        });
    }

    /** 记一轮。超过上限从头部丢——上下文是给模型看的，不是档案。 */
    private void pushHistory(String q, String a) {
        try {
            JSONObject u = new JSONObject();
            u.put("role", "user");
            u.put("content", q);
            JSONObject m = new JSONObject();
            m.put("role", "assistant");
            m.put("content", a);
            synchronized (history) {
                history.add(u);
                history.add(m);
                while (history.size() > HISTORY_MAX) {
                    history.remove(0);
                }
            }
        } catch (org.json.JSONException ignored) { }
    }

    private org.json.JSONArray snapshotHistory() {
        org.json.JSONArray arr = new org.json.JSONArray();
        synchronized (history) {
            for (JSONObject o : history) { arr.put(o); }
        }
        return arr;
    }

    // ---------------- 配对 ----------------

    /**
     * 配对：输电脑屏幕上的 6 位码 → 换长期凭据 → 存本地。
     *
     * 这是「配一次、之后不再配」的入口。以前这里没有它，只能手抄令牌，
     * 而令牌每次电脑重启都变——于是天天在救火。
     */
    private void showPairDialog() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(20), dp(8), dp(20), 0);

        final TextView where = new TextView(this);
        where.setText("电脑：" + Prefs.baseUrl(this)
                + "\n在电脑网页「编队」页点「生成」，把 6 位码填在下面。");
        where.setTextSize(12f);
        where.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        box.addView(where);

        final EditText code = new EditText(this);
        code.setHint("6 位配对码");
        code.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        code.setTextSize(20f);
        code.setLetterSpacing(0.3f);
        box.addView(code);

        final TextView tip = new TextView(this);
        tip.setText("配对一次长期有效：电脑重启、换网都不用再配。\n"
                + "被撤销时才需要重配（电脑网页上能撤销）。");
        tip.setTextSize(11f);
        tip.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        box.addView(tip);

        new AlertDialog.Builder(this)
                .setTitle("配对这台手机")
                .setView(box)
                .setPositiveButton("配对", new android.content.DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(android.content.DialogInterface d, int w) {
                        doPair(code.getText().toString().trim());
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void doPair(final String code) {
        if (code.length() != 6) {
            toast("配对码是 6 位数字");
            return;
        }
        final String base = Prefs.baseUrl(this);
        final String name = Prefs.deviceName(this);
        say2("正在配对…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    final JSONObject r = Net.claim(base, code, name, "android");
                    Prefs.saveDevice(MainActivity.this, r.optString("device_id"),
                            r.optString("secret"), name);
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            say2("配对成功，以后不用再配了");
                            toast("配对成功：" + name);
                            // 配完不留旧令牌：留着反而以后分不清是哪把钥匙在生效
                            Prefs.save(MainActivity.this, Prefs.baseUrl(MainActivity.this),
                                    "", Prefs.backend(MainActivity.this));
                            probeHealth();
                        }
                    });
                } catch (final Exception e) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            say2("配对失败：" + e.getMessage());
                        }
                    });
                }
            }
        });
    }

    private void showDeviceToken() {
        if (!Prefs.paired(this)) {
            toast("还没配对，没有本机令牌");
            return;
        }
        final String token = Prefs.deviceId(this) + ":" + Prefs.deviceSecret(this);
        new AlertDialog.Builder(this)
                .setTitle("本机设备令牌")
                .setMessage(Prefs.deviceName(this) + "\n" + Prefs.deviceId(this)
                        + "\n\n这是这台手机在电脑上的身份证。别的端要连这台手机时用它；"
                        + "丢掉不要紧，重新配对一次就换新的。")
                .setPositiveButton("复制", new android.content.DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(android.content.DialogInterface d, int w) {
                        android.content.ClipboardManager cm =
                                (android.content.ClipboardManager)
                                        getSystemService(CLIPBOARD_SERVICE);
                        cm.setPrimaryClip(android.content.ClipData.newPlainText(
                                "fleet-device", token));
                        toast("已复制（dev-id:secret）");
                    }
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void probeHealth() {
        final String base = Prefs.baseUrl(this);
        final boolean paired = Prefs.paired(this);
        status.setText((paired ? "核对 " : "连接 ") + base + " …");
        status.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        worker.execute(new Runnable() {
            @Override
            public void run() {
                String line;
                int color;
                if (!paired) {
                    // 没配对时只探活（/api/health 不要鉴权），
                    // 能不能干活还得配对——所以这里不许写「已连上」了事。
                    String r = Net.health(base);
                    if (r == null) {
                        line = "连不上 " + base;
                        color = R.color.alert;
                    } else {
                        line = "电脑在，但这台没配对";
                        color = R.color.think;
                    }
                } else {
                    try {
                        JSONObject m = Net.me(base, Net.auth(MainActivity.this));
                        String host = m.optString("host", "");
                        line = "已配对 " + (host.isEmpty() ? "" : "· " + host);
                        color = R.color.listen;
                    } catch (Exception e) {
                        line = "凭据失效：" + e.getMessage();
                        color = R.color.alert;
                    }
                }
                final String fLine = line;
                final int fColor = color;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        status.setText(fLine);
                        status.setTextColor(getResources().getColor(fColor, getTheme()));
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

        // ---- ① 连哪台电脑 ----
        final EditText eBase = new EditText(this);
        eBase.setHint("后端地址，例如 http://127.0.0.1:8000");
        eBase.setText(Prefs.baseUrl(this));
        eBase.setTextSize(13);
        box.addView(eBase);

        // ---- ② 配对状态 ----
        final TextView st = new TextView(this);
        st.setTextSize(13);
        st.setPadding(0, dp(10), 0, dp(4));
        st.setText(Prefs.paired(this)
                ? "配对：✔ " + Prefs.deviceName(this) + "（" + Prefs.deviceId(this) + "）"
                : "配对：✘ 还没配对，现在发不了请求");
        st.setTextColor(getResources().getColor(
                Prefs.paired(this) ? R.color.listen : R.color.think, getTheme()));
        box.addView(st);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        TextView bPair = chip("配对 / 重配", R.color.accent);
        bPair.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { showPairDialog(); }
        });
        row.addView(bPair);
        TextView bTok = chip("本机令牌", R.color.text_dim);
        bTok.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { showDeviceToken(); }
        });
        row.addView(bTok);
        TextView bForget = chip("解除配对", R.color.alert);
        bForget.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                Prefs.clearDevice(MainActivity.this);
                st.setText("配对：✘ 已解除（电脑那边还留着记录，网页上可撤销）");
                st.setTextColor(getResources().getColor(R.color.think, getTheme()));
                probeHealth();
            }
        });
        row.addView(bForget);
        box.addView(row);

        // ---- ③ 后端 ----
        final EditText eBackend = new EditText(this);
        eBackend.setHint("AI 后端（默认 pollinations；留空=全部）");
        eBackend.setText(Prefs.backend(this));
        eBackend.setTextSize(13);
        box.addView(eBackend);

        // ---- ④ 老令牌：只当兜底 ----
        final EditText eToken = new EditText(this);
        eToken.setHint("老令牌（一般留空，配对后不需要）");
        eToken.setText(Prefs.token(this));
        eToken.setTextSize(13);
        box.addView(eToken);

        TextView tip = new TextView(this);
        tip.setText("隧道模式（默认，推荐）\n"
                + "  电脑：python fleet_gui.py --link\n"
                + "  这里填 http://127.0.0.1:8000，然后点「配对」输 6 位码\n\n"
                + "局域网模式\n"
                + "  电脑：python fleet_gui.py --lan（要先设 FLEET_GUI_TOKEN）\n"
                + "  这里填 http://<电脑IP>:8000，同样用 6 位码配对\n\n"
                + "令牌那一栏只在配不上对、或是很老的电脑端时才用。");
        tip.setTextSize(11);
        tip.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        tip.setPadding(0, dp(8), 0, 0);
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
                .setNeutralButton("配对", new android.content.DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(android.content.DialogInterface d, int w) {
                        Prefs.save(MainActivity.this, eBase.getText().toString(),
                                eToken.getText().toString(),
                                eBackend.getText().toString());
                        showPairDialog();
                    }
                })
                .show();
    }

    /** 设置弹窗里的小按钮。AlertDialog 里塞真 Button 会把布局撑开，用 TextView 更像胶囊。 */
    private TextView chip(String text, int colorRes) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(12f);
        t.setPadding(dp(12), dp(7), dp(12), dp(7));
        t.setBackgroundResource(R.drawable.bg_chip);
        t.setTextColor(getResources().getColor(colorRes, getTheme()));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = dp(8);
        t.setLayoutParams(lp);
        return t;
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
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        say2("正在从电脑拉能力清单…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                org.json.JSONArray rows = null;
                String err = null;
                try {
                    org.json.JSONObject o = Net.caps(base, auth);
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
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        setBusy(true);
        say2("让电脑做：" + cap + "\n（要审批的话在电脑上点确认…）");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                String text;
                try {
                    org.json.JSONObject r = Net.call(base, auth, cap, args);
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
