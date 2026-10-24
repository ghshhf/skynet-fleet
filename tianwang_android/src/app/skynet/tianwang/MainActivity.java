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
import android.view.animation.DecelerateInterpolator;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ScrollView;
import android.widget.Switch;
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
    /** 顶栏副标题：模型名 · 连接状态。原来那行「已配对」合并进来了——
     *  参考页的副标题就是「当前在跟谁说话」，两行小字没有必要占两条。 */
    private TextView headerSub;
    private TextView subtitle;
    private ImageView btnMic;
    private ImageView btnSend;
    private ImageView btnAttach;
    private ImageView btnSpeak;
    private ImageView btnMenu;
    private ImageView btnNewChat;
    private ImageView btnMore;
    /** 思考开关：关掉能省掉整段内心戏，短问答快很多。 */
    private TextView chipThink;
    /** 联网开关（三态循环）。 */
    private TextView chipWeb;
    private View scrim;
    private View drawer;
    private TextView drawerHistory;
    private EditText input;
    private ScrollView chatScroll;
    private LinearLayout chatList;

    /** 顶栏两段文字。分开放是因为它们刷新时机完全不同：
     *  模型名只在换模型时变，连接状态每次探活都变。 */
    private String modelLabel = "";
    private String connLabel = "未连接";
    private int connColor = R.color.text_dim;
    /** 抽屉是否已滑出。用来做返回键与重复点击的判断。 */
    private boolean drawerOpen;
    /** 思考模式开关。默认关：这台是 0.6B，开着思考会把 token 全花在推理上。 */
    private boolean thinking;
    /** 联网模式：auto（按问题判断）/ on（强制搜）/ off（不联网）。
     *  默认 auto —— 用户要的是「能联网」，而不是「每条都联网」：
     *  每句闲聊都等十几秒搜一遍是灾难。 */
    private String webMode = "auto";
    /** 这一轮实际引用了哪些网页。搜完就存下来，等回答出来附在气泡末尾。
     *  为什么不只放状态行：状态行是过程提示，播报一开始就被「在说…」盖掉，
     *  等播报完又变回空闲提示——人回头想看「它参考了谁」已经没了。
     *  来源是判断可信度的唯一线索，必须留在一个不会被覆盖的地方。 */
    private volatile org.json.JSONArray webSrc = null;
    /** 语音播报开关。默认开，但只有出结果时才会用到。 */
    private boolean speakOn = true;
    /** 多轮上下文：只留最近的来回。放在内存里，不落盘——
     *  这段是「这次会话在聊什么」，不是「主人是谁」。后者属于人格记忆包。 */
    private final java.util.ArrayList<JSONObject> history = new java.util.ArrayList<>();
    private static final int HISTORY_MAX = 12;
    /** 正在流式更新的那个气泡；null = 当前没有在生成的回复。 */
    private TextView streaming;

    private final WavRecorder recorder = new WavRecorder();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    // 紧急通道：审批用。
    // 为什么不能跟 worker 混用—— worker 是**单线程**，而审批轮询是个
    // `while(...) { sleep(1500) ... }` 的长驻循环，一旦起跑就把那唯一一根
    // 线程永久占死。结果：弹窗弹得出来（轮询自己在跑），但点「允许」后
    // sendApproval 排在这个无限循环后面，**永远轮不到**，界面上就是
    // 「点了没反应」，最后 30 秒一到弹一句「这条审批已经结束了」。
    // 审批是 30 秒时效的请求，绝不能和出图/聊天这类长任务排同一条队。
    private final ExecutorService urgent = Executors.newFixedThreadPool(2);

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
        headerSub = findViewById(R.id.subtitle_model);
        subtitle = findViewById(R.id.subtitle);
        btnMic = findViewById(R.id.btnMic);
        btnSend = findViewById(R.id.btnSend);
        btnAttach = findViewById(R.id.btnAttach);
        btnSpeak = findViewById(R.id.btnSpeak);
        btnMenu = findViewById(R.id.btnMenu);
        btnNewChat = findViewById(R.id.btnNewChat);
        btnMore = findViewById(R.id.btnMore);
        chipThink = findViewById(R.id.chipThink);
        chipWeb = findViewById(R.id.chipWeb);
        scrim = findViewById(R.id.scrim);
        drawer = findViewById(R.id.drawer);
        drawerHistory = findViewById(R.id.drawerHistory);
        input = findViewById(R.id.input);
        chatScroll = findViewById(R.id.chatScroll);
        chatList = findViewById(R.id.chatList);
        addHint("在下面的框里打字，或点麦克风说话。\n"
                + "说的话会留在这儿，往上翻能看到前几轮。");

        // 抽屉初始藏在屏幕外。不用 DrawerLayout 是因为这个工程没接 AndroidX
        // （aapt2+javac+d8 手搓），自己滑反而更可控：位移量和时长都在手里。
        drawer.post(new Runnable() {
            @Override
            public void run() {
                drawer.setTranslationX(-drawer.getWidth());
            }
        });

        btnMenu.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { toggleDrawer(); }
        });
        scrim.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { closeDrawer(); }
        });
        btnNewChat.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { newChat(); }
        });
        btnMore.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { showMoreMenu(v); }
        });
        btnAttach.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { showAttachMenu(v); }
        });
        btnSpeak.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { toggleSpeak(); }
        });
        chipThink.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { toggleThink(); }
        });
        chipWeb.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { cycleWeb(); }
        });

        // ---- 抽屉里那六项。前两项是真的页面，后四项是原有的低频动作，
        //      从横着滑的胶囊条搬进来：它们本来就不该占顶栏位置。 ----
        navClick(R.id.navChat, new Runnable() {
            @Override public void run() { closeDrawer(); }
        });
        navClick(R.id.navFleet, new Runnable() {
            @Override public void run() { closeDrawer(); showPcPanel(); }
        });
        navClick(R.id.navModels, new Runnable() {
            @Override public void run() {
                closeDrawer();
                startActivity(new Intent(MainActivity.this, ModelActivity.class));
            }
        });
        navClick(R.id.navBench, new Runnable() {
            @Override public void run() {
                closeDrawer();
                startActivity(new Intent(MainActivity.this, DeviceCheckActivity.class));
            }
        });
        navClick(R.id.navSettings, new Runnable() {
            @Override public void run() { closeDrawer(); showSettings(); }
        });
        navClick(R.id.navAbout, new Runnable() {
            @Override public void run() { closeDrawer(); showAbout(); }
        });

        // 副标题的「跟谁说话」这一半：取当前后端名。它不是文件名，
        // 但比「未连接」有用——出问题时第一句要问的就是走的是哪条路。
        String bk = Prefs.backend(this);
        modelLabel = (bk == null || bk.trim().isEmpty()) ? "" : bk.trim();
        applyThinkUi();
        applyWebUi();
        refreshHeaderSub();
        refreshDrawerHistory();

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

        // 原来这里挂着八颗胶囊的点击绑定（控电脑/出图/检测/模型库/扫频/体温/
        // 配对/清屏）。现在它们分两处去：高频的进「+」和「更多」，导航的进抽屉。
        // 语义没变，只是不再全堆在顶栏横着滑。

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
        // 图标按钮没有文字可改，用「底色变淡绿 + 图标变绿」表示在录。
        // 双重变化是有意的：只改底色在浅色底上不够显眼，只改图标又看不出"是按钮"。
        btnMic.setBackgroundResource(R.drawable.bg_touch_on);
        btnMic.setColorFilter(getResources().getColor(R.color.listen, getTheme()));
        say2("在听…（松手结束）");
    }

    private void stopRecordAndAsk(boolean cancelled) {
        recording = false;
        btnMic.setBackgroundResource(R.drawable.bg_touch);
        btnMic.clearColorFilter();
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
                    // ① 先试手机本地 whisper：飞行模式 / 不在局域网也能出字（总纲 §四 第 6 条）。
                    // ② 本地不可用（没 .so / 没装模型 / 没听出内容）才回退传回 PC。
                    String text = localRecognize(wav);
                    if (TextUtils.isEmpty(text)) {
                        text = Net.recognize(base, auth, wav);
                    }
                    final String text0 = text;
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            if (TextUtils.isEmpty(text0)) {
                                setBusy(false);
                                orb.setState(OrbView.IDLE);
                                say2("没听清，再说一次？");
                                return;
                            }
                            ask(text0);
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

    /**
     * 手机本地 whisper 识别（总纲 §四 第 6 条）。不可用/失败返回 null。
     *
     * 本地优先是为了「飞行模式也能出字」；但没装模型时必须让调用方回退到 PC，
     * 不能因为本地这条新路把原来能用的传回 PC 弄坏。
     */
    private String localRecognize(byte[] wav) {
        try {
            if (wav == null || wav.length < 45) { return null; }
            java.io.File model = new java.io.File(Whisper.defaultModelPath(this));
            if (!Whisper.looksLikeGgml(model)) {
                android.util.Log.i("fleet-whisper", "本地模型未装（" + model + "），回退 PC 识别");
                return null;
            }
            // 定位 WAV 的 data 块：不假设固定 44 字节头（有些录音器会多写 LIST/fact 段）
            int off = 12, end = wav.length;
            while (off + 8 <= end) {
                String id = new String(wav, off, 4, "US-ASCII");
                int sz = (wav[off + 4] & 0xff) | ((wav[off + 5] & 0xff) << 8)
                        | ((wav[off + 6] & 0xff) << 16) | ((wav[off + 7] & 0xff) << 24);
                if ("data".equals(id)) { off += 8; end = Math.min(end, off + sz); break; }
                off += 8 + sz + (sz & 1);
            }
            if (off >= end) { return null; }
            int n = (end - off) / 2;
            short[] pcm = new short[n];
            for (int i = 0; i < n; i++) {
                int lo = wav[off + i * 2] & 0xff;
                int hi = wav[off + i * 2 + 1];
                pcm[i] = (short) ((hi << 8) | lo);
            }
            Whisper w = new Whisper();
            try {
                if (!w.load(model.getAbsolutePath())) {
                    android.util.Log.i("fleet-whisper", "本地模型加载失败：" + w.lastError + "，回退 PC 识别");
                    return null;
                }
                String t = w.transcribe(pcm, 16000, "zh");
                if (t == null || t.isEmpty()) {
                    android.util.Log.i("fleet-whisper", "本地没听出内容：" + w.lastError + "，回退 PC 识别");
                    return null;
                }
                android.util.Log.i("fleet-whisper", "本地识别成功 " + w.lastMs + "ms");
                return t;
            } finally {
                w.close();
            }
        } catch (Throwable t) {
            android.util.Log.w("fleet-whisper", "本地识别异常，回退 PC：" + t);
            return null;
        }
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
        webSrc = null;          // 新一轮：先把上一轮的来源清掉，免得挂错人

        // 思考开关是**软开关**：不改后端采样参数，只改给模型的指令。
        // 硬关得让服务端把 chat_template_kwargs.enable_thinking 透传下去，
        // 那条链要动十几个后端适配器，先不动；这句引导对 Qwen3 这类会吐
        // 内心戏的模型确实有效（能把「思考>」那一段压掉大半）。
        // 注意：引导只进这次的请求，不进 history——否则下一轮上下文里全是废话。
        final String asked = prompt + (thinking
                ? "\n\n（先在心里把推理理清楚，再给结论；推理过程可以写出来。）"
                : "\n\n（直接给答案，不要输出思考过程。）");

        // 先把「我说的」钉到屏幕上，再看它怎么答。
        // 顺序反了会出现「它答完才看见我问了啥」，很像它答错人。
        appendMsg(true, prompt);
        if (Prefs.localMode(this)) {
            say2("本地模型思考中…（不连电脑）");
        } else {
            say2("在想…");
        }
        final TextView bubble = appendMsg(false, "…");
        streaming = bubble;

        // 同一个回调两种后端都能用：本地模式没有 web 进度帧，onWeb 自然不触发，
        // onLine/onDone/onError 两路共用，不用写两份。
        final Net.SseCallback cb = new Net.SseCallback() {
                    @Override
                    public void onWeb(final org.json.JSONObject data) {
                        // 联网/工具进度。搜索和手机工具都是几秒到十几秒的活，
                        // 不亮状态的话界面上就是一句「在想…」卡着不动，
                        // 人只会以为死机。
                        if (data == null) { return; }
                        final String phase = data.optString("phase", "");
                        final boolean used = data.optBoolean("used", false);
                        final String reason = data.optString("reason", "");
                        final String mode = data.optString("mode", "");
                        final String tool = data.optString("tool", "");
                        final String label = data.optString("label", "");
                        org.json.JSONArray srcs = data.optJSONArray("sources");
                        final int n = srcs == null ? 0 : srcs.length();
                        if (used && srcs != null) {
                            webSrc = srcs;      // 留着给 onDone 拼进气泡
                        }
                        ui.post(new Runnable() {
                            @Override
                            public void run() {
                                if ("searching".equals(phase)) {
                                    if ("tool".equals(mode) && tool.length() > 0) {
                                        // 手机工具：让用户看见是真的在动设备
                                        say2("正在用手机工具 " + tool + "…");
                                    } else {
                                        say2("正在联网查…");
                                    }
                                } else if (used) {
                                    if ("tool".equals(mode)) {
                                        say2("已用手机工具 "
                                             + (label.length() > 0 ? label : tool));
                                    } else {
                                        say2("已联网 · 引用了 " + n + " 个来源");
                                    }
                                } else if ("error".equals(phase)) {
                                    say2(reason.startsWith("工具面")
                                         ? reason : ("联网没成功：" + reason));
                                } else {
                                    say2("没联网：" + reason);
                                }
                            }
                        });
                    }
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
                        // 显示版带来源，进上下文的是纯回答：来源不是「我说过的话」，
                        // 塞进 history 既撑上下文又会让模型下一轮开始模仿这个格式。
                        final String shown = withSources(reply);
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
                                bubble.setText(shown);
                                scrollToBottom();
                            }
                        });
                        speak(reply);
                    }
                };

        // 本地模式：直接打手机自带的 llama-server，不碰电脑、不联网。
        // 开着就彻底离线，断网也能聊——这是「手机下好模型却调不动」的修法。
        final boolean local = Prefs.localMode(MainActivity.this);
        worker.execute(new Runnable() {
            @Override
            public void run() {
                if (local) {
                    Net.chatLocal(Prefs.localBase(MainActivity.this), "local",
                            asked, snapshotHistory(), cb);
                } else {
                    Net.chat(base, auth, asked, backend, snapshotHistory(), cb, webMode);
                }
            }
        });
    }

    /** 在回答末尾附上这一轮参考过的网页。
     *
     *  只放「编号 + 标题 + 域名」：手机上贴完整 URL 会把气泡撑成两屏，
     *  而且这个 TextView 没开 autoLink，长链接只会碍事。
     *  标题截到 42 字——中文标题常常 60 多字，不截就变成三行。 */
    private String withSources(String reply) {
        org.json.JSONArray srcs = webSrc;
        if (srcs == null || srcs.length() == 0) {
            return reply;
        }
        StringBuilder sb = new StringBuilder(reply);
        sb.append("\n\n参考来源：");
        for (int i = 0; i < srcs.length(); i++) {
            org.json.JSONObject s = srcs.optJSONObject(i);
            if (s == null) { continue; }
            String t = s.optString("title", "").trim();
            String h = s.optString("host", "").trim();
            if (t.length() > 42) { t = t.substring(0, 42) + "…"; }
            sb.append("\n[").append(s.optInt("i", i + 1)).append("] ").append(t);
            if (!h.isEmpty()) { sb.append("（").append(h).append("）"); }
        }
        return sb.toString();
    }

    private void speak(final String text) {
        // 播报关了就别再走一遍合成：合成是有成本的（本地 TTS 要起引擎，
        // 远端要一次请求），静音状态下算完再丢掉纯属浪费。
        if (!speakOn) {
            say2("（播报已关，只显示文字）");
            return;
        }
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
        // 我说的气泡是淡绿底，正文必须跟着换成深绿——沿用通用正文色在浅绿上对比不够。
        t.setTextColor(getResources().getColor(mine ? R.color.mine_text : R.color.text, getTheme()));
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

    // ---------------- 抽屉 ----------------

    /** 给抽屉里的一项挂点击。id 不存在时静默跳过——布局改版期间不至于整页崩掉。 */
    private void navClick(int id, final Runnable r) {
        View v = findViewById(id);
        if (v == null) { return; }
        v.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View x) { r.run(); }
        });
    }

    /**
     * 自己滑抽屉，不用 DrawerLayout。
     *
     * 为什么手搓：这个工程是 aapt2+javac+d8 直编，没接 AndroidX，
     * 引 drawerlayout 要连带 appcompat 一起进来，包体翻几倍只为一条侧滑。
     * 手写就三件事：位移、遮罩、返回键，行为完全一致。
     */
    private void openDrawer() {
        if (drawerOpen) { return; }
        drawerOpen = true;
        drawer.setVisibility(View.VISIBLE);
        scrim.setVisibility(View.VISIBLE);
        drawer.setTranslationX(-drawer.getWidth());
        drawer.animate().translationX(0f)
                .setDuration(220)
                .setInterpolator(new DecelerateInterpolator())
                .start();
        refreshDrawerHistory();
    }

    private void closeDrawer() {
        if (!drawerOpen) { return; }
        drawerOpen = false;
        drawer.animate().translationX(-drawer.getWidth())
                .setDuration(180)
                .setInterpolator(new DecelerateInterpolator())
                .start();
        scrim.setVisibility(View.GONE);
    }

    private void toggleDrawer() {
        if (drawerOpen) { closeDrawer(); } else { openDrawer(); }
    }

    @Override
    public void onBackPressed() {
        // 抽屉开着时返回键先收抽屉，这是侧边栏的通用预期；
        // 不接管的话一按就退出 App，人会以为闪退。
        if (drawerOpen) {
            closeDrawer();
            return;
        }
        super.onBackPressed();
    }

    /** 顶栏副标题：「跟谁说话」+「连上没有」。两段分开存，刷新互不干扰。 */
    private void refreshHeaderSub() {
        if (headerSub == null) { return; }
        int dim = getResources().getColor(R.color.text_dim, getTheme());
        // 两段用不同颜色：模型名恒灰，状态只在异常时上色。
        // 全行一个颜色的话，"已配对"也会绿成一片，真正该被看见的报错就淹了。
        android.text.SpannableStringBuilder sb = new android.text.SpannableStringBuilder();
        if (!modelLabel.isEmpty()) {
            int s0 = sb.length();
            sb.append(modelLabel).append("  ·  ");
            sb.setSpan(new android.text.style.ForegroundColorSpan(dim), s0, sb.length(),
                    android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        int s1 = sb.length();
        sb.append(connLabel);
        int c = (connColor == R.color.text_dim || connColor == R.color.listen)
                ? dim : getResources().getColor(connColor, getTheme());
        sb.setSpan(new android.text.style.ForegroundColorSpan(c), s1, sb.length(),
                android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        headerSub.setText(sb);
    }

    /** 抽屉「今天」下面那一行。只报条数，不编造会话标题——
     *  真要历史列表得先有落盘存储，现在每轮只在内存里。 */
    private void refreshDrawerHistory() {
        if (drawerHistory == null) { return; }
        int turns = history.size() / 2;
        if (turns <= 0) {
            drawerHistory.setText(R.string.history_empty);
            return;
        }
        String first = "";
        synchronized (history) {
            for (JSONObject o : history) {
                if ("user".equals(o.optString("role"))) {
                    first = o.optString("content", "");
                    break;
                }
            }
        }
        if (first.length() > 18) { first = first.substring(0, 18) + "…"; }
        drawerHistory.setText(first.isEmpty() ? (turns + " 轮对话") : first);
    }

    /** 新对话：清屏 + 清上下文。和原来「清屏」是同一件事，但改了个更准确的名字——
     *  它清的是上下文，不只是屏幕。 */
    private void newChat() {
        chatList.removeAllViews();
        synchronized (history) { history.clear(); }
        streaming = null;
        addHint("新对话。上文已经清掉了。");
        say2("新对话");
        refreshDrawerHistory();
    }

    /**
     * 「更多」：原来那排胶囊里最不该占顶栏的几个。
     *
     * 用 PopupMenu 而不是自绘弹层：它有系统级的位置计算（贴边自动翻转）
     * 和返回键处理，自己画一遍只会多一堆边界 bug。
     */
    private void showMoreMenu(View anchor) {
        PopupMenu m = new PopupMenu(this, anchor);
        m.getMenu().add(0, 1, 0, "扫频（WiFi / 蜂窝频段）");
        m.getMenu().add(0, 2, 1, "体温（热状态与掉频）");
        m.getMenu().add(0, 3, 2, "设备自检");
        m.getMenu().add(0, 4, 3, "配对这台手机");
        m.getMenu().add(0, 5, 4, "设置");
        m.setOnMenuItemClickListener(new PopupMenu.OnMenuItemClickListener() {
            @Override
            public boolean onMenuItemClick(android.view.MenuItem it) {
                switch (it.getItemId()) {
                    case 1: scanRadio(); return true;
                    case 2: checkThermal(); return true;
                    case 3: startActivity(new Intent(MainActivity.this, DeviceCheckActivity.class)); return true;
                    case 4: showPairDialog(); return true;
                    case 5: showSettings(); return true;
                    default: return false;
                }
            }
        });
        m.show();
    }

    /**
     * 「+」：往里放东西的地方 —— 出图、出视频、换模型。
     *
     * 为什么不把 prompt 在这儿吃掉直接生成：出图有尺寸/步数/风格这些参数，
     * 塞进一个弹窗会挤成一团。跳进出图页，把当前输入框的文字带过去，
     * 人只用补参数。
     */
    private void showAttachMenu(View anchor) {
        PopupMenu m = new PopupMenu(this, anchor);
        m.getMenu().add(0, 1, 0, "画一张图（本机 SD）");
        m.getMenu().add(0, 2, 1, "生成一段视频（电脑出片）");
        m.getMenu().add(0, 3, 2, "换模型");
        m.setOnMenuItemClickListener(new PopupMenu.OnMenuItemClickListener() {
            @Override
            public boolean onMenuItemClick(android.view.MenuItem it) {
                switch (it.getItemId()) {
                    case 1: openImagePage(); return true;
                    case 2: makeVideo(); return true;
                    case 3: startActivity(new Intent(MainActivity.this, ModelActivity.class)); return true;
                    default: return false;
                }
            }
        });
        m.show();
    }

    /** 把输入框里已经打好字带进出图页 —— 人多半是先写了描述才想起要画图。 */
    private void openImagePage() {
        Intent i = new Intent(this, ImageActivity.class);
        String seed = input.getText().toString().trim();
        if (!seed.isEmpty()) {
            i.putExtra("prompt", seed);
            input.setText("");
        }
        startActivity(i);
    }

    /**
     * 出视频：手机不出片，交给电脑。
     *
     * 手机端做不了这个量级：编一段 8 秒 512² 已经是它的极限（4 段各 <1s 的
     * zoompan 推镜拼起来），而且那还是静帧推镜，不是真生成。
     * 真出片走服务端的 render.reel / video.gen，结果落盘后用系统播放器打开。
     */
    private void makeVideo() {
        final String seed = input.getText().toString().trim();
        if (seed.isEmpty()) {
            toast("先在输入框里写一句画面描述");
            return;
        }
        input.setText("");
        final TextView b = appendMsg(true, "出视频：" + seed);
        say2("交给电脑出片…");
        final TextView st = appendMsg(false, "…");
        worker.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    JSONObject r = Net.call(Prefs.baseUrl(MainActivity.this),
                            Net.auth(MainActivity.this), "video.gen",
                            new JSONObject().put("prompt", seed));
                    final String out = r.toString();
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            st.setText(summarizeCap(out));
                            scrollToBottom();
                            say2("出片请求已返回");
                        }
                    });
                } catch (final Exception e) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            st.setText("出视频失败：" + e.getMessage());
                            say2("出视频失败");
                        }
                    });
                }
            }
        });
    }

    /** 能力返回值的手机版摘要：JSON 原样贴上手机是没法读的。 */
    private static String summarizeCap(String json) {
        try {
            JSONObject o = new JSONObject(json);
            if (!o.optBoolean("ok", true)) {
                return "失败：" + o.optString("err", o.optString("error", "未知原因"));
            }
            JSONObject d = o.optJSONObject("data");
            if (d == null) { return json.length() > 400 ? json.substring(0, 400) + "…" : json; }
            StringBuilder sb = new StringBuilder();
            java.util.Iterator<String> it = d.keys();
            while (it.hasNext()) {
                String k = it.next();
                String v = String.valueOf(d.opt(k));
                if (v.length() > 200) { v = v.substring(0, 200) + "…"; }
                sb.append(k).append(": ").append(v).append('\n');
            }
            return sb.length() == 0 ? "完成（无返回内容）" : sb.toString().trim();
        } catch (Exception e) {
            return json.length() > 400 ? json.substring(0, 400) + "…" : json;
        }
    }

    private void toggleThink() {
        thinking = !thinking;
        applyThinkUi();
        say2(thinking ? "思考已开：答得细，但慢" : "思考已关：直接给答案");
    }

    private void applyThinkUi() {
        chipThink.setBackgroundResource(thinking ? R.drawable.bg_chip_on : R.drawable.bg_chip);
        chipThink.setTextColor(getResources().getColor(
                thinking ? R.color.accent : R.color.text_dim, getTheme()));
    }

    /** 联网三态循环：当 → 开 → 关 → 当。
     *  为什么是三态而不是一个开关：一个开关只能表达「联网/不联网」，但
     *  「每条都搜」和「需要时才搜」体验差一个数量级（前者每问多等十几秒）。
     *  默认「当」既满足「能联网」，又不会把闲聊拖慢。 */
    private void cycleWeb() {
        if ("auto".equals(webMode)) {
            webMode = "on";
        } else if ("on".equals(webMode)) {
            webMode = "off";
        } else {
            webMode = "auto";
        }
        applyWebUi();
        if ("auto".equals(webMode)) {
            say2("联网：自动（像「最新/今天/多少钱」这类才去搜）");
        } else if ("on".equals(webMode)) {
            say2("联网：常开（每句都先搜一遍，会慢十几秒）");
        } else {
            say2("联网：关（只用模型自己的记忆，问到新事会答旧）");
        }
    }

    private void applyWebUi() {
        int label = "auto".equals(webMode) ? R.string.web_auto
                : ("on".equals(webMode) ? R.string.web_on : R.string.web_off);
        chipWeb.setText(label);
        // 只有「常开」给高亮：默认态和关闭态都灰，靠文字区分 ——
        // 三态全用不同底色的话，一眼看不出哪个是默认。
        boolean hot = "on".equals(webMode);
        chipWeb.setBackgroundResource(hot ? R.drawable.bg_chip_on : R.drawable.bg_chip);
        chipWeb.setTextColor(getResources().getColor(
                hot ? R.color.accent : R.color.text_dim, getTheme()));
    }

    private void toggleSpeak() {
        speakOn = !speakOn;
        btnSpeak.setColorFilter(speakOn ? null
                : getResources().getColor(R.color.text_faint, getTheme()));
        say2(speakOn ? "播报已开" : "播报已关（只显示文字）");
    }

    /** 关于：把「这台是什么、连到哪」讲清楚。排查问题第一句问的就是这些。 */
    private void showAbout() {
        String ver = "";
        try {
            android.content.pm.PackageInfo pi =
                    getPackageManager().getPackageInfo(getPackageName(), 0);
            ver = pi.versionName + " (build " + pi.versionCode + ")";
        } catch (Exception ignored) { }
        StringBuilder sb = new StringBuilder();
        sb.append("天网 · 手机端 ").append(ver).append('\n').append('\n');
        sb.append("服务端：").append(Prefs.baseUrl(this)).append('\n');
        sb.append("后端：").append(Prefs.backend(this)).append('\n');
        sb.append("配对：").append(Prefs.paired(this) ? "已配对" : "未配对").append('\n');
        sb.append("设备：").append(Prefs.deviceName(this)).append('\n').append('\n');
        sb.append("这台手机同时是编队里的一个算力节点：").append('\n');
        sb.append("· 语言模型跑在本机，不走网络").append('\n');
        sb.append("· 出图用本机 SD（sd.cpp + NEON）").append('\n');
        sb.append("· 扫频 / 热状态只有手机能做").append('\n').append('\n');
        sb.append("对话、模型、参数都在抽屉里。");
        new AlertDialog.Builder(this)
                .setTitle("关于")
                .setMessage(sb.toString())
                .setPositiveButton("知道了", null)
                .show();
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
        connLabel = paired ? "核对凭据…" : "连接中…";
        connColor = R.color.text_dim;
        refreshHeaderSub();
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
                    } catch (java.net.ConnectException e) {
                        // 连不上 ≠ 凭据失效。以前这两种都写「凭据失效」，
                        // 于是隧道没通（电脑端没起服务 / adb reverse 掉了）
                        // 时，人被指挥去重新配对——配一百次也是同一个错。
                        // 处置完全不同，文案就必须分开。
                        line = "连不上 " + base + "（电脑端服务没起，或 adb 隧道断了）";
                        color = R.color.alert;
                    } catch (java.net.SocketTimeoutException e) {
                        line = "超时 " + base + "（电脑在但没应答）";
                        color = R.color.alert;
                    } catch (java.net.UnknownHostException e) {
                        line = "地址不对 " + base;
                        color = R.color.alert;
                    } catch (Exception e) {
                        // 走到这儿才是真的凭据问题（401/403 已被 Net 转成可读文案）
                        line = "凭据失效：" + e.getMessage();
                        color = R.color.alert;
                    }
                }
                final String fLine = line;
                final int fColor = color;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        connLabel = fLine;
                        connColor = fColor;
                        refreshHeaderSub();
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

        // ---- ②·b Sky MCP：把电脑那 70 项能力暴露成 MCP server ----
        // 手机上别的 agent（Operit 之类）填这个地址就能用本仓的能力。
        // 只能摸到**只读**能力：有副作用的默认不导出，注册表层还会再 fail-closed 一次。
        final TextView mMcp = new TextView(this);
        mMcp.setTextSize(12);
        mMcp.setPadding(0, dp(10), 0, dp(4));
        final String mcpUrl = Prefs.baseUrl(this) + "/api/mcp";
        mMcp.setText("Sky MCP：" + mcpUrl + "\n（HTTP JSON-RPC 2.0，默认只导出只读能力）");
        mMcp.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        box.addView(mMcp);

        LinearLayout rowMcp = new LinearLayout(this);
        rowMcp.setOrientation(LinearLayout.HORIZONTAL);
        TextView bMcp = chip("复制 MCP 地址", R.color.accent);
        bMcp.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                android.content.ClipboardManager cm =
                        (android.content.ClipboardManager)
                                getSystemService(CLIPBOARD_SERVICE);
                cm.setPrimaryClip(android.content.ClipData.newPlainText(
                        "fleet-mcp", mcpUrl));
                toast("已复制：" + mcpUrl);
            }
        });
        rowMcp.addView(bMcp);
        box.addView(rowMcp);

        // ---- ③ 后端 ----
        final EditText eBackend = new EditText(this);
        eBackend.setHint("AI 后端（默认 pollinations；留空=全部）");
        eBackend.setText(Prefs.backend(this));
        eBackend.setTextSize(13);
        box.addView(eBackend);

        // ---- ③·b 本地模型（MCB）：手机自己跑，不连电脑、不联网 ----
        // 开之前必须先在 Termux 里把 llama-server 起起来，否则对话会直接报错带「怎么救」。
        final Switch swLocal = new Switch(this);
        swLocal.setText("本地模型模式（不连电脑、不联网）");
        swLocal.setTextSize(13);
        swLocal.setChecked(Prefs.localMode(this));
        swLocal.setPadding(0, dp(10), 0, dp(2));
        box.addView(swLocal);

        final EditText eLocalBase = new EditText(this);
        eLocalBase.setHint("本地模型地址，默认 http://127.0.0.1:8080");
        eLocalBase.setText(Prefs.localBase(this));
        eLocalBase.setTextSize(13);
        box.addView(eLocalBase);

        final TextView localTip = new TextView(this);
        localTip.setTextSize(11);
        localTip.setPadding(0, dp(2), 0, 0);
        localTip.setTextColor(getResources().getColor(R.color.text_dim, getTheme()));
        localTip.setText("开之前：Termux 里先起 llama-server\n"
                + "  llama-server -m /sdcard/Download/本地模型/\n"
                + "    Qwen3VL-4B-Instruct-Q4_K_M.gguf \\\n"
                + "    --host 127.0.0.1 --port 8080 --alias local\n"
                + "然后打开上面开关即可。4B 模型纯 CPU 约 6~7 tok/s。");
        box.addView(localTip);

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
                        Prefs.setLocalMode(MainActivity.this, swLocal.isChecked());
                        Prefs.setLocalBase(MainActivity.this, eLocalBase.getText().toString());
                        toast(swLocal.isChecked()
                                ? "已切到本地模型（不连电脑）。开聊前确认 Termux 里 llama-server 已起。"
                                : "已切回电脑模式。");
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
                        Prefs.setLocalMode(MainActivity.this, swLocal.isChecked());
                        Prefs.setLocalBase(MainActivity.this, eLocalBase.getText().toString());
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
    protected void onResume() {
        super.onResume();
        startApprovalWatch();
    }

    @Override
    protected void onPause() {
        if (recording) { stopRecordAndAsk(true); }
        stopPlayback();
        setBusy(false);
        orb.setState(OrbView.IDLE);
        stopApprovalWatch();
        super.onPause();
    }

    // ---------------- 审批：手机能不能「动手」全靠这一块 ----------------

    /**
     * 电脑那边遇到 ask 档（打开应用、点屏幕、打字）会挂起等人点头，
     * 等 30 秒没人理就当拒绝。**手机不来看一眼，它就永远只能读、不能动。**
     *
     * 所以这里轮询待审批清单，有就弹一个打断式对话框：说清要做什么、
     * 在哪台设备上、参数是什么，然后让主人选允许/拒绝。
     */
    private volatile boolean approvalWatch;
    /** 轮询心跳计数：只为日志降噪用，不影响逻辑。 */
    private int _dbgN;
    /** 连续连不上电脑的次数（成功一次就清零），用来决定要不要出声提醒。 */
    private int approvalFailN;
    private android.app.AlertDialog approvalDlg;
    /** 已经弹过的 id，防止同一个请求反复弹。 */
    private final java.util.Set<String> approvalSeen =
            java.util.Collections.synchronizedSet(new java.util.HashSet<String>());

    private void startApprovalWatch() {
        if (approvalWatch) { return; }
        approvalWatch = true;
        urgent.execute(() -> {
            while (approvalWatch) {
                try { Thread.sleep(1500); } catch (InterruptedException e) { return; }
                if (!approvalWatch) { return; }
                try {
                    JSONObject r = Net.approvals(Prefs.baseUrl(MainActivity.this),
                                                 Net.auth(MainActivity.this));
                    approvalFailN = 0;   // 通了就别再念叨断线
                    if (r.optInt("count", 0) <= 0) {
                        if (approvalWatch && _dbgN++ % 8 == 0) {
                            // 每 12 秒留一条心跳。审批弹不出来时，先能回答
                            // 「轮询到底活着吗、它看到几条」——否则只能靠猜。
                            Log.d(TAG, "审批轮询在跑：count=0 base="
                                    + Prefs.baseUrl(MainActivity.this));
                        }
                        continue;
                    }
                    org.json.JSONArray arr = r.optJSONArray("pending");
                    if (arr == null || arr.length() == 0) { continue; }
                    JSONObject p = arr.optJSONObject(0);
                    if (p == null) { continue; }
                    String rid = p.optString("request_id", "");
                    if (rid.isEmpty() || !approvalSeen.add(rid)) { continue; }
                    if (approvalSeen.size() > 64) { approvalSeen.clear(); }
                    String cap = p.optString("cap", "");
                    String title = p.optString("title", cap);
                    String dev = p.optString("device", "");
                    String prev = p.optString("args_preview", "");
                    ui.post(() -> askApproval(rid, title, cap, dev, prev));
                } catch (Exception e) {
                    // 不能静默吞掉：审批弹不出来时，这是唯一的线索。
                    // 踩过两次：
                    //   ① 轮询一直 403，界面上什么都没有；
                    //   ② adb reverse 掉了，连不上 127.0.0.1:8000（**这才是
                    //      「点了没反应」的真凶**——弹窗根本没机会出现）。
                    // 两种都是「主人只看到点了没反应，日志里一个字都没有」。
                    Log.w(TAG, "审批轮询失败：" + e);
                    // 连续失败要说一次人话。静默重试 = 主人永远不知道
                    // 手机其实已经和电脑断开了，「已配对」那行旧字还在骗人。
                    // 只在「刚断」和「刚恢复」时报一次，不刷屏。
                    int n = ++approvalFailN;
                    if (n == 3 || n % 20 == 0) {
                        final String why = e.getMessage() == null ? String.valueOf(e) : e.getMessage();
                        ui.post(() -> say2("连不上电脑（" + Prefs.baseUrl(MainActivity.this)
                                + "），审批先不管了：" + why
                                + "\n电脑端开着、且 adb reverse tcp:8000 tcp:8000 还在，才会恢复。"));
                    }
                }
            }
        });
    }

    private void stopApprovalWatch() {
        approvalWatch = false;
        if (approvalDlg != null) {
            try { approvalDlg.dismiss(); } catch (Exception ignored) { }
            approvalDlg = null;
        }
    }

    private void askApproval(String rid, String title, String cap,
                             String device, String preview) {
        if (isFinishing()) { return; }
        StringBuilder sb = new StringBuilder();
        sb.append("要做的动作：").append(title.isEmpty() ? cap : title);
        if (!cap.isEmpty() && !cap.equals(title)) { sb.append("\n能力：").append(cap); }
        if (!device.isEmpty()) { sb.append("\n设备：").append(device); }
        if (!preview.isEmpty() && !preview.equals("{}")) {
            sb.append("\n参数：").append(preview);
        }
        sb.append("\n\n不同意就什么都不做。这条 30 秒没人理会自动取消。");
        android.app.AlertDialog d = new android.app.AlertDialog.Builder(this)
                .setTitle("要允许这个动作吗？")
                .setMessage(sb.toString())
                .setPositiveButton("允许", null)
                .setNegativeButton("拒绝", null)
                // 允许按返回键/点外面关掉：不点允许本来就等于不执行，
                // 而把人锁在框里只会让人以为 App 卡死了。
                .setCancelable(true)
                .create();
        // 先设成 null 再在 show 后换掉监听：默认监听一点就 dismiss，
        // 而回应失败（比如已经超时）时我希望弹窗留着，主人看见原因再关。
        d.setOnShowListener(dlg -> {
            d.getButton(android.app.AlertDialog.BUTTON_POSITIVE)
             .setOnClickListener(v -> sendApproval(rid, true, d));
            d.getButton(android.app.AlertDialog.BUTTON_NEGATIVE)
             .setOnClickListener(v -> sendApproval(rid, false, d));
        });
        approvalDlg = d;
        d.show();
        // 这条可能已经在别处被处理，或者 30 秒到了自动取消。
        // **不盯着它，旧弹窗会一直挂在屏幕上**（实测踩过：回一条超时，弹窗
        // 不走，下一条审批被它挡在后面，点「允许」点到的其实是早已作废的那条）。
        watchApprovalGone(rid, d);
    }

    /** 这条审批还在等吗？不在了就把弹窗自己收掉。 */
    private void watchApprovalGone(String rid, android.app.AlertDialog d) {
        ui.postDelayed(() -> {
            if (!d.isShowing()) { return; }
            urgent.execute(() -> {
                boolean still = true;
                try {
                    JSONObject r = Net.approvals(Prefs.baseUrl(MainActivity.this),
                                                 Net.auth(MainActivity.this));
                    org.json.JSONArray arr = r.optJSONArray("pending");
                    if (arr != null) {
                        still = false;
                        for (int i = 0; i < arr.length(); i++) {
                            JSONObject p = arr.optJSONObject(i);
                            if (p != null
                                    && rid.equals(p.optString("request_id", ""))) {
                                still = true;
                                break;
                            }
                        }
                    }
                } catch (Exception e) {
                    Log.w(TAG, "查审批是否还在失败：" + e);
                    // 探不到就当它还在——误关弹窗比让它多留一会儿糟
                }
                if (!still) {
                    ui.post(() -> {
                        if (d.isShowing()) {
                            d.dismiss();
                            if (approvalDlg == d) { approvalDlg = null; }
                            say2("这条审批已经结束了");
                        }
                    });
                } else {
                    watchApprovalGone(rid, d);
                }
            });
        }, 1500);
    }

    private void sendApproval(String rid, boolean allow, android.app.AlertDialog d) {
        final String base = Prefs.baseUrl(this);
        final Net.Auth auth = Net.auth(this);
        urgent.execute(() -> {
            String err = "";
            try {
                Net.respond(base, auth, rid, allow);
            } catch (Exception e) {
                err = e.getMessage() == null ? "回应没送到" : e.getMessage();
            }
            final String msg = err;
            ui.post(() -> {
                if (msg.isEmpty()) {
                    say2(allow ? "已允许" : "已拒绝");
                    d.dismiss();
                    if (approvalDlg == d) { approvalDlg = null; }
                } else {
                    // 多半是「这条已经超时结束了」——说清，别让主人以为没生效。
                    // 仍然要关掉弹窗：留着一条已经点不动的框在屏幕上没意义。
                    say2("回应没生效：" + msg);
                    d.dismiss();
                    if (approvalDlg == d) { approvalDlg = null; }
                }
            });
        });
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
        // 关键修复：原来走单线程 worker，一旦被别的任务占住就「点了没反应」。
        // 改成 urgent 池（2 线程，专给审批/拉清单这类不能等的活），不再被饿死。
        urgent.execute(new Runnable() {
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
                            // 连不上：弹常驻对话框说清原因+排查+重试，而不是一闪 Toast。
                            // 这正是「点编队没反应」的根因——之前只 say2 一下就没了。
                            showPcFail(base, e2);
                            return;
                        }
                        if (r == null || r.length() == 0) {
                            showPcFail(base, "电脑那头没有注册任何能力");
                            return;
                        }
                        renderPcPanel(r);
                    }
                });
            }
        });
    }

    /** 编队页连不上电脑时的常驻提示：说清原因 + 三步排查 + 重试。 */
    private void showPcFail(final String base, final String err) {
        final String e = (err == null) ? "" : err;
        final String why;
        if (e.contains("ConnectException") || e.contains("timed out")
                || e.contains("TimeoutException") || e.contains("UnknownHost")
                || e.contains("Failed to connect") || e.contains("ENETUNREACH")) {
            why = "电脑那头连不上（" + base + "）。\n\n先排查三件事：\n"
                    + "① 电脑端 fleet_gui 在跑吗？  python fleet_gui.py --link\n"
                    + "② 做过  adb reverse tcp:8000 tcp:8000  吗？（App 靠它回连电脑）\n"
                    + "③ 手机是不是开了全局 VPN？它会把局域网流量也劫走。";
        } else if (e.contains("401") || e.contains("403") || e.contains("凭据")) {
            why = "连上了，但凭据不对：" + e + "\n试试在设置里重新配对。";
        } else {
            why = "拉清单失败：" + e;
        }
        new AlertDialog.Builder(this)
                .setTitle("编队 · 连不上电脑")
                .setMessage(why)
                .setPositiveButton("重试", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        showPcPanel();
                    }
                })
                .setNegativeButton("知道了", null)
                .show();
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
        urgent.execute(new Runnable() {
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
