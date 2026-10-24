// 编队桥：最小无障碍服务（手机侧唯一常驻组件）。
//
// 存在的唯一理由：MIUI/HyperOS 把 input / monkey 的注入权限锁在「USB 调试(安全设置)」
// 后面，而那个开关要求登录小米账号 + 插 SIM 卡。无障碍的 dispatchGesture 走的是
// AccessibilityInputFilter，不经过 InputManager.injectInputEvent 的那道
// INJECT_EVENTS 检查，所以只要用户手动授权一次，就能点击/滑动/输入。
//
// 不做的事：不读通知、不传文件、不上传任何数据、不联网（除了本机 loopback 的
// 控制端口）。它只执行控制端发来的四条指令。
package app.fleet.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Path;
import android.hardware.ConsumerIrManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public class BridgeService extends AccessibilityService {

    private static final String TOKEN = "__TOKEN__";
    private static final int PORT = 8765;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private volatile boolean running = true;
    private ServerSocket server;

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        running = true;
        Thread t = new Thread(this::acceptLoop, "fleet-bridge-accept");
        t.setDaemon(true);
        t.start();
    }

    @Override
    public void onDestroy() {
        running = false;
        try {
            if (server != null) server.close();
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 不看事件，只借这个服务身份做手势注入
    }

    @Override
    public void onInterrupt() {
    }

    // ------------------------------------------------------------ 控制通道

    private void acceptLoop() {
        try {
            server = new ServerSocket(PORT, 8, java.net.InetAddress.getByName("127.0.0.1"));
            while (running) {
                Socket s = server.accept();
                pool.execute(() -> handle(s));
            }
        } catch (Exception e) {
            // 端口被占或服务被解绑：静默退出，不打扰用户
        }
    }

    private void handle(Socket sock) {
        try (Socket s = sock;
             BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
             PrintWriter out = new PrintWriter(new OutputStreamWriter(s.getOutputStream(), "UTF-8"), true)) {
            boolean authed = false;
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;
                if (!authed) {
                    if (line.startsWith("AUTH ") && TOKEN.equals(line.substring(5).trim())) {
                        authed = true;
                        out.println("OK auth");
                    } else {
                        out.println("ERR auth");
                    }
                    continue;
                }
                out.println(exec(line));
            }
        } catch (Exception ignored) {
        }
    }

    private String exec(String line) {
        String[] parts = line.split("\\s+", 2);
        String op = parts[0].toUpperCase();
        String rest = parts.length > 1 ? parts[1].trim() : "";
        try {
            switch (op) {
                case "PING":
                    return "PONG";
                case "TAP":
                    return tap(rest);
                case "SWIPE":
                    return swipe(rest);
                case "LONG":
                    return longPress(rest);
                case "TEXT":
                    return setText(rest);
                case "BACK":
                    return global(GLOBAL_ACTION_BACK);
                case "HOME":
                    return global(GLOBAL_ACTION_HOME);
                case "RECENTS":
                    return global(GLOBAL_ACTION_RECENTS);
                case "NOTIFY":
                    return global(GLOBAL_ACTION_NOTIFICATIONS);
                case "FOCUS":
                    return focusInfo();
                case "UI":
                    return uiTree(rest);
                case "TAPEL":
                    return tapEl(rest);
                case "SETTEXT":
                    return setTextEl(rest);
                case "SETSEEK":
                    return setSeek(rest);
                case "SCROLL":
                    return scroll(rest);
                case "IRINFO":
                    return irInfo();
                case "IR":
                    return ir(rest);
                case "BTSTATE":
                    return btState();
                case "BLE":
                    return ble(rest);
                case "PROBE":
                    return probeTcp(rest);
                case "PROBE_UDP":
                    return probeUdp(rest);
                default:
                    return "ERR unknown " + op;
            }
        } catch (Exception e) {
            return "ERR " + e.getClass().getSimpleName() + ": " + e.getMessage();
        }
    }

    // ------------------------------------------------------------ 具体动作

    private static int[] ints(String s, int need) {
        String[] a = s.trim().split("\\s+");
        int[] v = new int[need + 1];
        int n = Math.min(a.length, need + 1);
        for (int i = 0; i < n; i++) v[i] = Integer.parseInt(a[i]);
        return v;
    }

    private String tap(String rest) {
        int[] v = ints(rest, 3);
        if (v[0] <= 0 && v[1] <= 0) return "ERR coords";
        Path p = new Path();
        p.moveTo(v[0], v[1]);
        int dur = v[2] > 0 ? v[2] : 55;
        return gesture(p, dur) ? "OK tap " + v[0] + " " + v[1] : "ERR gesture";
    }

    private String swipe(String rest) {
        int[] v = ints(rest, 5);
        Path p = new Path();
        p.moveTo(v[0], v[1]);
        p.lineTo(v[2], v[3]);
        int dur = v[4] > 0 ? v[4] : 300;
        return gesture(p, dur) ? "OK swipe" : "ERR gesture";
    }

    private String longPress(String rest) {
        int[] v = ints(rest, 3);
        Path p = new Path();
        p.moveTo(v[0], v[1]);
        int dur = v[2] > 0 ? v[2] : 650;
        return gesture(p, dur) ? "OK long" : "ERR gesture";
    }

    private boolean gesture(Path path, int durationMs) {
        final CountDownLatch latch = new CountDownLatch(1);
        final boolean[] ok = {false};
        final GestureDescription g = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, Math.max(20, durationMs)))
                .build();
        final GestureResultCallback cb = new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                ok[0] = true;
                latch.countDown();
            }

            @Override
            public void onCancelled(GestureDescription d) {
                latch.countDown();
            }
        };
        main.post(() -> {
            if (!dispatchGesture(g, cb, null)) latch.countDown();
        });
        try {
            latch.await(3, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
        return ok[0];
    }

    private String setText(String text) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window";
        AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (focus == null) return "ERR no-focus";
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        boolean ok = focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        return ok ? "OK text" : "ERR text-rejected";
    }

    private String global(int action) {
        return performGlobalAction(action) ? "OK global " + action : "ERR global";
    }

    private String focusInfo() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window";
        AccessibilityNodeInfo f = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (f == null) return "OK focus none";
        CharSequence cls = f.getClassName();
        CharSequence txt = f.getText();
        return "OK focus " + cls + " | " + (txt == null ? "" : txt);
    }

    // ------------------------------------------------------------ 读屏
    //
    // 为什么放在桥里，而不是继续用 `uiautomator dump`：
    //   dump 要起外部 instrumentation 进程、等界面进入 idle，实测每次 1~3 秒，
    //   动画/视频页常常等不到 idle 直接超时；桥里 getRootInActiveWindow() 是同进程直读，
    //   毫秒级，且不受 idle 状态约束。
    //
    // 为什么输出要剪枝：一屏动辄上千节点、几十 KB，直接喂模型会撑爆上下文。
    // 剪枝规则取自 Operit（ToolResultDataClasses.kt shouldKeepNode）：
    //   关键类 ∪ 有文字/描述 ∪ 可点 ∪ 子孙里有值得留的。
    // ⚠️ 照抄会带入他们一个静默失效的分支：他们拿全限定类名（android.widget.Button）
    //    去比 "Button" 这类短名，`className in setOf("Button", ...)` 恒为 false，
    //    所以"关键类"这条其实一直没生效。这里取最后一段类名比较，让这条真的起作用。
    //
    // 输出格式（两空格缩进表父子，▶可点 ◢不可点）：
    //   ▶[Button]T:"发送"ID:btn_send⮞[100,200][300,400]
    // 不给节点编序号：序号只在一次快照内有效，跨快照即失效，容易诱发"拿旧序号点错位置"。
    // 寻址该用行里已有的 ID / 文字 / bounds。

    private static final java.util.Set<String> KEY_CLASSES =
            new java.util.HashSet<>(java.util.Arrays.asList(
                    "Button", "TextView", "EditText", "ScrollView", "Switch",
                    "ImageView", "CheckBox", "RadioButton", "SeekBar", "Spinner",
                    "CompoundButton", "TabWidget"));

    /** 一屏最多遍历多少节点：极端页面（长列表/网页）的硬闸，防止遍历本身卡住。 */
    private static final int MAX_NODES = 3000;

    private String uiTree(String rest) {
        // 取根本身可能很慢（目标 App 的 UI 线程忙时，accessibility 查询要排到它后面），
        // 它和遍历是两笔开销，必须分开计时，否则优化会对错方向。
        long tRoot = System.nanoTime();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        long rootMs = (System.nanoTime() - tRoot) / 1000000L;
        if (root == null) {
            return "ERR no-window 当前没有活动窗口（锁屏、下拉状态栏或系统弹窗遮住时会出现）";
        }
        int maxLines = 150;
        uiWithId = !rest.contains("noid");        // UI 400 noid = 不带 id 的裸读，用来量化 id 的代价
        try {
            if (!rest.trim().isEmpty()) {
                maxLines = Math.max(20, Math.min(1200,
                        Integer.parseInt(rest.trim().split("\\s+")[0])));
            }
        } catch (NumberFormatException ignored) {
        }

        long t0 = System.nanoTime();
        StringBuilder body = new StringBuilder(8192);
        int[] nodes = {0};
        render(root, "", body, nodes);
        long ms = (System.nanoTime() - t0) / 1000000L;

        String[] lines = body.toString().split("\n");
        int keep = body.length() == 0 ? 0 : Math.min(lines.length, maxLines);
        StringBuilder out = new StringBuilder(body.length() + 128);
        for (int i = 0; i < keep; i++) out.append(lines[i]).append('\n');
        if (keep < lines.length) {
            out.append("… 已截断 ").append(lines.length - keep)
               .append(" 行（本次遍历 ").append(nodes[0]).append(" 节点）\n");
        }

        CharSequence pkg = root.getPackageName();
        // 报字节不报字符：中文一个字 3 字节，按 length() 报会把体积低估成三分之一，
        // 而门禁判的就是字节数。
        // n= 是遍历过的节点数（剪枝前），root= 是取根耗时。
        // 这两个数决定了慢在哪：n 大说明树大要剪枝，root 大说明目标 App 的 UI 线程堵。
        String head = "OK ui " + keep + " " + utf8Len(out) + " " + ms + "ms pkg="
                + (pkg == null ? "?" : pkg) + " n=" + nodes[0] + " root=" + rootMs + "ms";
        return head + "\n" + out + "END";
    }

    private static int utf8Len(CharSequence s) {
        int n = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c < 0x80) n += 1;
            else if (c < 0x800) n += 2;
            else if (Character.isHighSurrogate(c)) { n += 4; i++; }
            else n += 3;
        }
        return n;
    }

    /**
     * 是否在树里带 resource-id。
     *
     * 这是实测出来的最大单项开销：第三方 App 的 id 字符串不在系统资源缓存里，
     * 每个节点的 getViewIdResourceName() 都可能走一次跨进程查询。
     * 抖音视频页 162 节点整体 2 秒，几乎全耗在这上面；系统设置同一指标是 1ms/节点。
     * 留成开关是为了随时能量化「带 id」到底值不值得（选元素时值，纯看内容时不必）。
     */
    private boolean uiWithId = true;

    private void render(AccessibilityNodeInfo n, String indent, StringBuilder sb, int[] nodes) {
        if (nodes[0] >= MAX_NODES) return;
        nodes[0]++;

        // 先渲染子孙（后序）：自己该不该留，要看子孙里有没有值得留的，
        // 而父行必须排在子行前面，所以子行先收进临时 buffer。
        StringBuilder kids = new StringBuilder();
        int childCount = n.getChildCount();
        for (int i = 0; i < childCount; i++) {
            AccessibilityNodeInfo c = n.getChild(i);
            if (c == null) continue;
            render(c, indent + "  ", kids, nodes);
        }

        String cls = simpleName(n.getClassName());
        String txt = clip(n.getText(), 30);
        String desc = clip(n.getContentDescription(), 40);
        String vid = uiWithId ? n.getViewIdResourceName() : null;
        boolean clickable = n.isClickable();

        // children.any { shouldKeepNode() } 的等价判断：kids 非空就说明子孙里有料
        if (!KEY_CLASSES.contains(cls) && txt.isEmpty() && desc.isEmpty()
                && !clickable && kids.length() == 0) {
            return;                       // 布局壳子：自己和子孙都没内容，整支丢掉
        }

        android.graphics.Rect r = new android.graphics.Rect();
        n.getBoundsInScreen(r);
        if (r.isEmpty()) {
            // 零尺寸 = 画不出来，写进树里只会让模型去点一个不存在的目标；
            // 但子孙可能自带有效区域，所以不连带丢弃。
            sb.append(kids);
            return;
        }

        sb.append(indent).append(clickable ? '▶' : '◢').append('[').append(cls).append(']');
        if (!txt.isEmpty()) sb.append("T:\"").append(txt).append('"');
        if (!desc.isEmpty()) sb.append("D:\"").append(desc).append('"');
        if (vid != null && !vid.isEmpty()) sb.append("ID:").append(vid);
        sb.append("⮞[").append(r.left).append(',').append(r.top).append("][")
          .append(r.right).append(',').append(r.bottom).append("]\n");
        sb.append(kids);
    }

    /** 取类名最后一段：android.widget.Button -> Button。 */
    private static String simpleName(CharSequence cls) {
        if (cls == null) return "?";
        String s = cls.toString();
        int i = s.lastIndexOf('.');
        return i >= 0 ? s.substring(i + 1) : s;
    }

    /**
     * 压成单行：换行/制表会破坏"一行一个节点"，双引号会破坏 T:"…" 的边界。
     * 超长按码点截断，避免把 emoji 这类代理对劈成半个字符。
     */
    private static String clip(CharSequence cs, int max) {
        if (cs == null) return "";
        String s = cs.toString().replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
                .replace('"', '\'').trim();
        if (s.isEmpty()) return "";
        if (s.length() <= max) return s;
        int cut = max;
        if (Character.isHighSurrogate(s.charAt(cut - 1))) cut--;
        return s.substring(0, cut) + "…";
    }

    // ------------------------------------------------------------ 元素寻址（P1）
    //
    // 坐标点击（TAP）要求调用方先自己读屏、自己算中心点。界面一变就得全部重算，
    // 而且模型根本算不准坐标 —— 它说得出「点发送」，说不出「点 1024,2388」。
    // 所以把寻址下沉到设备侧：调用方只说元素，坐标在这儿解析。
    //
    // 选择器语法：kind:value[#n]
    //   id:btn_send                / id:com.android.settings:id/btn_send（短名按 / 后缀匹配）
    //   text:发送
    //   desc:搜索
    //   bounds:[100,200][300,400]  （±4px 容差）
    //   #n  1-based，只在匹配到多个时消歧
    //
    // 纪律（主人的红线：不许静默）：找不到就报 not-found 并附界面上真实存在的候选，
    // 匹配到多个就报 ambiguous 并把候选列出来，绝不"点个差不多的地方"。
    // 也刻意不做「找不到就退化成点坐标」这类兜底 —— 那只会把错误藏起来。

    private static final int BOUNDS_TOL = 4;

    private static final class Sel {
        String kind;     // id / text / desc / bounds
        String value;
        int index;       // 0 = 未指定
    }

    private static final class Hit {
        final AccessibilityNodeInfo node;
        final int x, y, area;

        Hit(AccessibilityNodeInfo n, android.graphics.Rect r) {
            node = n;
            x = (r.left + r.right) / 2;
            y = (r.top + r.bottom) / 2;
            area = Math.max(1, r.width() * r.height());
        }
    }

    private Sel parseSel(String s) {
        if (s == null) return null;
        String body = s.trim();
        int bar = body.indexOf('|');
        if (bar >= 0) body = body.substring(0, bar).trim();     // SETTEXT 的文本部分由调用方另取
        int hash = body.lastIndexOf('#');
        int index = 0;
        if (hash > 0) {
            String tail = body.substring(hash + 1);
            if (tail.matches("\\d{1,3}")) {
                index = Integer.parseInt(tail);
                body = body.substring(0, hash).trim();
            }
        }
        int colon = body.indexOf(':');
        if (colon <= 0) return null;
        Sel sel = new Sel();
        sel.kind = body.substring(0, colon).trim().toLowerCase();
        sel.value = body.substring(colon + 1).trim();
        sel.index = index;
        if (!"id".equals(sel.kind) && !"text".equals(sel.kind)
                && !"desc".equals(sel.kind) && !"bounds".equals(sel.kind)) {
            return null;
        }
        if (sel.value.isEmpty()) return null;
        return sel;
    }

    /** 0 不匹配 / 1 模糊 / 2 精确。精确 = 整串相等，模糊 = 包含。 */
    private static int matchScore(AccessibilityNodeInfo n, Sel sel) {
        if ("id".equals(sel.kind)) {
            String vid = n.getViewIdResourceName();
            if (vid == null || vid.isEmpty()) return 0;
            if (vid.equals(sel.value) || vid.endsWith("/" + sel.value)) return 2;
            return 0;
        }
        if ("bounds".equals(sel.kind)) {
            android.graphics.Rect want = rectOf(sel.value);
            if (want == null) return 0;
            android.graphics.Rect r = new android.graphics.Rect();
            n.getBoundsInScreen(r);
            boolean same = Math.abs(r.left - want.left) <= BOUNDS_TOL
                    && Math.abs(r.top - want.top) <= BOUNDS_TOL
                    && Math.abs(r.right - want.right) <= BOUNDS_TOL
                    && Math.abs(r.bottom - want.bottom) <= BOUNDS_TOL;
            return same ? 2 : 0;
        }
        CharSequence cs = "text".equals(sel.kind) ? n.getText() : n.getContentDescription();
        if (cs == null) return 0;
        String v = cs.toString().trim();
        if (v.isEmpty()) return 0;
        if (v.equals(sel.value)) return 2;
        return v.contains(sel.value) ? 1 : 0;
    }

    private static android.graphics.Rect rectOf(String s) {
        String[] nums = s.replaceAll("[^0-9,+-]", " ").trim().split("[\\s,]+");
        if (nums.length < 4) return null;
        try {
            return new android.graphics.Rect(Integer.parseInt(nums[0]), Integer.parseInt(nums[1]),
                    Integer.parseInt(nums[2]), Integer.parseInt(nums[3]));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private void collect(AccessibilityNodeInfo n, Sel sel, int minScore,
                         Map<String, Hit> acc, int[] budget) {
        if (n == null || budget[0] >= MAX_NODES) return;
        budget[0]++;
        if (matchScore(n, sel) >= minScore) {
            android.graphics.Rect r = new android.graphics.Rect();
            n.getBoundsInScreen(r);
            if (!r.isEmpty()) {
                String key = r.left + "," + r.top + "," + r.right + "," + r.bottom;
                Hit old = acc.get(key);
                // 内外层容器常共用同一块 bounds：留着外层会把点击落到容器上，
                // 所以同一块区域优先保留可点的那个。
                if (old == null || (!old.node.isClickable() && n.isClickable())) {
                    acc.put(key, new Hit(n, r));
                }
            }
        }
        int cc = n.getChildCount();
        for (int i = 0; i < cc; i++) collect(n.getChild(i), sel, minScore, acc, budget);
    }

    private List<Hit> locate(AccessibilityNodeInfo root, Sel sel) {
        Map<String, Hit> acc = new LinkedHashMap<>();
        collect(root, sel, 2, acc, new int[]{0});          // 先只收精确匹配
        if (acc.isEmpty()) {
            collect(root, sel, 1, acc, new int[]{0});      // 精确为空才退到模糊
        }
        List<Hit> hits = new ArrayList<>();
        for (Hit h : acc.values()) {
            if (h.node.isVisibleToUser()) hits.add(h);
        }
        final List<Hit> hidden = new ArrayList<>();
        for (Hit h : acc.values()) {
            if (!h.node.isVisibleToUser()) hidden.add(h);
        }
        // 按视觉顺序排（上→下、左→右）：歧义时 #n 必须和屏幕上看到的次序一致，
        // 否则调用方数不清是第几个。同一位置再按面积小的优先（避开全屏容器）。
        hits.sort((a, b) -> {
            if (a.y != b.y) return Integer.compare(a.y, b.y);
            if (a.x != b.x) return Integer.compare(a.x, b.x);
            return Integer.compare(a.area, b.area);
        });
        hits.addAll(hidden);                               // 不可见的排在后面，只用于诊断
        return hits;
    }

    /** 返回错误串；能继续往下点就返回 null。 */
    private String checkHits(AccessibilityNodeInfo root, List<Hit> hits, Sel sel) {
        int visible = 0;
        for (Hit h : hits) if (h.node.isVisibleToUser()) visible++;
        List<Hit> vis = hits.subList(0, visible);
        if (vis.isEmpty()) {
            if (!hits.isEmpty()) {
                return "ERR not-visible " + hits.size() + " " + brief(hits.get(0).node)
                        + " 找到了但不在可视区（先 SCROLL 滚到它）";
            }
            return "ERR not-found " + sel.kind + ":" + sel.value + " 界面上没有这个元素；候选 "
                    + nearby(root);
        }
        if (sel.index > 0 && sel.index > visible) {
            return "ERR bad-index #" + sel.index + " 只匹配到 " + visible + " 个";
        }
        if (sel.index == 0 && visible > 1) {
            StringBuilder sb = new StringBuilder("ERR ambiguous " + visible + " 个匹配，请加 #序号：");
            for (int i = 0; i < visible && i < 4; i++) {
                if (i > 0) sb.append(" | ");
                sb.append('#').append(i + 1).append(' ').append(brief(vis.get(i).node));
            }
            return sb.toString();
        }
        return null;
    }

    /** 界面上真实存在的可点/有字元素，找不到时贴给调用方，省得它瞎猜。 */
    private String nearby(AccessibilityNodeInfo root) {
        List<String> got = new ArrayList<>();
        collectBrief(root, got, new int[]{0});
        if (got.isEmpty()) return "（无）";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < got.size() && i < 6; i++) {
            if (i > 0) sb.append(" | ");
            sb.append(got.get(i));
        }
        return sb.toString();
    }

    private void collectBrief(AccessibilityNodeInfo n, List<String> out, int[] budget) {
        if (n == null || budget[0] >= MAX_NODES || out.size() >= 6) return;
        budget[0]++;
        if (n.isVisibleToUser() && (n.isClickable()
                || n.getText() != null || n.getContentDescription() != null)) {
            String b = brief(n);
            if (!b.equals("[]")) out.add(b);
        }
        int cc = n.getChildCount();
        for (int i = 0; i < cc; i++) collectBrief(n.getChild(i), out, budget);
    }

    private static String brief(AccessibilityNodeInfo n) {
        StringBuilder sb = new StringBuilder();
        sb.append('[').append(simpleName(n.getClassName())).append(']');
        String t = clip(n.getText(), 16);
        if (!t.isEmpty()) sb.append("T:\"").append(t).append('"');
        String d = clip(n.getContentDescription(), 16);
        if (!d.isEmpty()) sb.append("D:\"").append(d).append('"');
        String vid = n.getViewIdResourceName();
        if (vid != null && !vid.isEmpty()) sb.append("ID:").append(vid);
        return sb.toString();
    }

    private int[] screenSize() {
        android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
        return new int[]{dm.widthPixels, dm.heightPixels};
    }

    private String tapEl(String rest) {
        Sel sel = parseSel(rest);
        if (sel == null) {
            return "ERR sel 用法：TAPEL id:xxx / text:xxx / desc:xxx / bounds:[l,t][r,b]";
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window 当前没有活动窗口";
        List<Hit> hits = locate(root, sel);
        String bad = checkHits(root, hits, sel);
        if (bad != null) return bad;
        Hit h = hits.get(sel.index > 0 ? sel.index - 1 : 0);
        int[] wh = screenSize();
        if (h.x < 0 || h.y < 0 || h.x >= wh[0] || h.y >= wh[1]) {
            return "ERR out-of-screen " + h.x + "," + h.y + " 屏幕 " + wh[0] + "x" + wh[1];
        }
        Path p = new Path();
        p.moveTo(h.x, h.y);
        if (!gesture(p, 55)) return "ERR gesture";
        return "OK tapel " + h.x + " " + h.y + " " + brief(h.node);
    }

    private String setTextEl(String rest) {
        int bar = rest.indexOf('|');
        if (bar < 0) return "ERR 用法：SETTEXT <sel>|<文本>";
        Sel sel = parseSel(rest.substring(0, bar));
        if (sel == null) return "ERR sel 无法解析：" + rest.substring(0, bar).trim();
        String text = unesc(rest.substring(bar + 1));
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window 当前没有活动窗口";
        List<Hit> hits = locate(root, sel);
        String bad = checkHits(root, hits, sel);
        if (bad != null) return bad;
        AccessibilityNodeInfo target = editable(hits.get(sel.index > 0 ? sel.index - 1 : 0).node, 0);
        if (target == null) return "ERR not-editable " + brief(hits.get(0).node);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        boolean ok = target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        return ok ? "OK settext " + brief(target) : "ERR text-rejected " + brief(target);
    }

    /** 命中的可能是输入框的容器，往下找真正可编辑的那个。 */
    private AccessibilityNodeInfo editable(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 8) return null;
        if (n.isEditable()) return n;
        int cc = n.getChildCount();
        for (int i = 0; i < cc; i++) {
            AccessibilityNodeInfo got = editable(n.getChild(i), depth + 1);
            if (got != null) return got;
        }
        return null;
    }

    /** 往下找带 RangeInfo 的那个（SeekBar 本体），容器自己通常没有区间信息。 */
    private AccessibilityNodeInfo rangeable(AccessibilityNodeInfo n, int depth) {
        if (n == null || depth > 8) return null;
        if (n.getRangeInfo() != null) return n;
        int cc = n.getChildCount();
        for (int i = 0; i < cc; i++) {
            AccessibilityNodeInfo got = rangeable(n.getChild(i), depth + 1);
            if (got != null) return got;
        }
        return null;
    }

    /**
     * SETSEEK &lt;选择器&gt;|&lt;百分比 0-100&gt;
     *
     * 为什么不拖：滑条的实际可拖区间受左右 padding、滑块半径影响，各 ROM 不同，
     * 按屏幕像素算「30%」必然偏；而 ACTION_SET_PROGRESS 交的是语义值，由控件自己换算。
     * 另外 SeekBar 的 clickable 常为 false（MIUI 亮度条就是），TAPEL 根本点不动。
     * 区间不假定 0~100，以控件自报的 RangeInfo 为准（有的滑条是 0~255 或 1~2047）。
     */
    private String setSeek(String rest) {
        int bar = rest.indexOf('|');
        if (bar < 0) return "ERR 用法：SETSEEK <sel>|<0-100>";
        Sel sel = parseSel(rest.substring(0, bar));
        if (sel == null) return "ERR sel 无法解析：" + rest.substring(0, bar).trim();
        int pct;
        try {
            pct = Math.max(0, Math.min(100, Integer.parseInt(rest.substring(bar + 1).trim())));
        } catch (NumberFormatException e) {
            return "ERR 百分比不是整数：" + rest.substring(bar + 1).trim();
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window 当前没有活动窗口";
        List<Hit> hits = locate(root, sel);
        String bad = checkHits(root, hits, sel);
        if (bad != null) return bad;
        AccessibilityNodeInfo n = hits.get(sel.index > 0 ? sel.index - 1 : 0).node;
        AccessibilityNodeInfo target = rangeable(n, 0);
        if (target == null) return "ERR not-seekbar " + brief(n) + " 该元素没有区间信息";

        AccessibilityNodeInfo.RangeInfo ri = target.getRangeInfo();
        float lo = ri == null ? 0f : ri.getMin();
        float hi = ri == null ? 100f : ri.getMax();
        float val = lo + (hi - lo) * pct / 100f;
        Bundle args = new Bundle();
        args.putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, val);
        // 编译用的 android-35 jar 里没有 ACTION_SET_PROGRESS 这个常量
        //（javac 报"找不到符号"），但它就是 R.id.accessibilityActionSetProgress = 0x0102003d。
        // 用数值而不是反射：动作 id 是平台固定值，不会随版本变。
        boolean ok = target.performAction(0x0102003d, args);
        if (!ok) {
            return "ERR seek-rejected " + brief(target)
                    + " 该控件不接受 setProgress（区间 " + lo + "~" + hi + "）";
        }
        return "OK setseek " + pct + "%=" + Math.round(val) + " " + brief(target);
    }

    // 为什么滚动不用 ACTION_SCROLL_FORWARD：
    //   实测（K80 / MIUI 设置首页）连续发 3 次 FORWARD，第 1 次 performAction 返回 true
    //   但界面纹丝不动，第 2、3 次才滚；BACKWARD 同样首发生效、次发空转。
    //   也就是说这个动作的返回值不能代表"真的滚了"，而"回读验证再重试"正是主人禁止的兜底。
    //   改走手势滑动：和 TAPEL 同一条 dispatchGesture 路径，行为对任何 App 一致。
    //   用较长的 duration 抑制 fling，避免"滑一下飞出去三屏"。
    private static final int SCROLL_MS = 550;
    private static final float SCROLL_SPAN = 0.35f;   // 相对容器高度/宽度的滑动比例

    private String scroll(String rest) {
        String dir = rest.trim().toLowerCase();
        boolean vertical, forward;
        if ("down".equals(dir)) { vertical = true; forward = true; }
        else if ("up".equals(dir)) { vertical = true; forward = false; }
        else if ("right".equals(dir)) { vertical = false; forward = true; }
        else if ("left".equals(dir)) { vertical = false; forward = false; }
        else return "ERR 用法：SCROLL up|down|left|right";

        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return "ERR no-window 当前没有活动窗口";

        // 页面上可能同时有横向 tab 和竖向列表，按面积挑最大的那个当主体容器
        final List<AccessibilityNodeInfo> cand = new ArrayList<>();
        final List<Integer> areas = new ArrayList<>();
        findScrollable(root, cand, areas, new int[]{0});
        if (cand.isEmpty()) return "ERR no-scrollable 当前界面没有可滚动容器";
        int best = 0;
        for (int i = 1; i < cand.size(); i++) if (areas.get(i) > areas.get(best)) best = i;
        AccessibilityNodeInfo n = cand.get(best);

        android.graphics.Rect r = new android.graphics.Rect();
        n.getBoundsInScreen(r);
        if (r.isEmpty()) return "ERR no-bounds " + brief(n);

        float sx, sy, ex, ey;
        if (vertical) {
            float h = r.height();
            float cx = (r.left + r.right) / 2f;
            if (forward) {                       // 内容往上走：手指从下往上划
                sy = r.top + h * 0.72f;
                ey = sy - h * SCROLL_SPAN;
            } else {
                sy = r.top + h * 0.28f;
                ey = sy + h * SCROLL_SPAN;
            }
            sx = cx;
            ex = cx;
        } else {
            float w = r.width();
            float cy = (r.top + r.bottom) / 2f;
            if (forward) {                       // 内容往左走：手指从右往左划
                sx = r.left + w * 0.75f;
                ex = sx - w * SCROLL_SPAN;
            } else {
                sx = r.left + w * 0.25f;
                ex = sx + w * SCROLL_SPAN;
            }
            sy = cy;
            ey = cy;
        }

        Path p = new Path();
        p.moveTo(sx, sy);
        p.lineTo(ex, ey);
        if (!gesture(p, SCROLL_MS)) return "ERR gesture";
        return "OK scroll " + dir + " " + brief(n);
    }

    private void findScrollable(AccessibilityNodeInfo n, List<AccessibilityNodeInfo> out,
                                List<Integer> areas, int[] budget) {
        if (n == null || budget[0] >= MAX_NODES) return;
        budget[0]++;
        if (n.isScrollable() && n.isVisibleToUser() && n.getChildCount() > 0) {
            android.graphics.Rect r = new android.graphics.Rect();
            n.getBoundsInScreen(r);
            if (!r.isEmpty()) {
                out.add(n);
                areas.add(r.width() * r.height());
            }
        }
        int cc = n.getChildCount();
        for (int i = 0; i < cc; i++) findScrollable(n.getChild(i), out, areas, budget);
    }

    /** SETTEXT 用 | 分隔选择器与文本，文本里的 | 与 % 由控制端转义。 */
    private static String unesc(String s) {
        return s.replace("%7C", "|").replace("%7c", "|").replace("%25", "%");
    }

    // ------------------------------------------------------------ 红外
    //
    // 走 Android 标准的 ConsumerIrManager，只用到 TRANSMIT_IR（普通权限，安装即得），
    // 不碰 USB 调试、不碰无障碍注入。手机在这里就是一只离线可用的万能遥控器。

    private ConsumerIrManager irManager() {
        return (ConsumerIrManager) getSystemService(Context.CONSUMER_IR_SERVICE);
    }

    private String irInfo() {
        ConsumerIrManager m = irManager();
        if (m == null) return "ERR no-service";
        if (!m.hasIrEmitter()) return "ERR no-emitter";
        StringBuilder sb = new StringBuilder("OK ir");
        for (ConsumerIrManager.CarrierFrequencyRange r : m.getCarrierFrequencies()) {
            sb.append(" ").append(r.getMinFrequency()).append("-").append(r.getMaxFrequency());
        }
        return sb.toString();
    }

    private String ir(String rest) {
        ConsumerIrManager m = irManager();
        if (m == null) return "ERR no-service";
        if (!m.hasIrEmitter()) return "ERR no-emitter";
        String[] p = rest.trim().split("\\s+");
        if (p.length < 3) return "ERR ir-need freq+2 durations";
        int freq = Integer.parseInt(p[0]);
        int[] pattern = new int[p.length - 1];
        for (int i = 1; i < p.length; i++) pattern[i - 1] = Integer.parseInt(p[i]);
        if ((freq < 25000) || (freq > 60000)) return "ERR ir-freq 25k-60k";
        m.transmit(freq, pattern);
        return "OK ir " + pattern.length;
    }

    // ------------------------------------------------------------ 网络探测（P3）
    //
    // 把「手机当前所在网络里某设备的暴露面」读回来，让探测跟着人走
    // （电脑侧只能扫电脑所在的网段，手机连到哪个网就能探哪个网）。
    //
    // 纪律（与电脑侧 tools/wall_probe.py 同一条红线，写在两边互相盯着）：
    //   · 只做任何合法播放器/扫描器零凭据第一步就会做的事——TCP 连上、
    //     发一个请求、读响应。RTSP DESCRIBE 返回 401/403 就是终点头：
    //     **不携带任何凭据、不发 SETUP/PLAY、不做任何口令尝试**。
    //     刑法 285/286 管未授权访问本身，与目标是否违法无关。
    //   · 单连接、单请求、限长限时，不做并发轰炸。
    //   · 回复 base64 单行/多行带回，解析全在电脑侧（fleet/wallprobe.py），
    //     这里只保证「连得上、发得出、读得回」。
    private static final int PROBE_CONNECT_MS = 2000;
    private static final int PROBE_MAX_BYTES = 65536;

    /** PROBE <ip> <port> <payload-b64> [read_ms] → "OK probe <字节数> <base64>"。 */
    private static String probeTcp(String rest) {
        String[] p = rest.trim().split("\\s+", 4);
        if (p.length < 3) {
            return "ERR probe-need ip+port+payload(b64)";
        }
        String ip = p[0];
        int port;
        try {
            port = Integer.parseInt(p[1]);
        } catch (NumberFormatException e) {
            return "ERR probe-port " + p[1];
        }
        int readMs = 2000;
        if (p.length >= 4) {
            try {
                readMs = Integer.parseInt(p[3]);
            } catch (NumberFormatException ignored) {
            }
        }
        readMs = Math.max(200, Math.min(10000, readMs));
        byte[] payload;
        try {
            payload = android.util.Base64.decode(p[2], android.util.Base64.DEFAULT);
        } catch (IllegalArgumentException e) {
            return "ERR probe-b64";
        }
        try (Socket s = new Socket()) {
            s.connect(new java.net.InetSocketAddress(ip, port), PROBE_CONNECT_MS);
            s.getOutputStream().write(payload);
            s.getOutputStream().flush();
            byte[] all = readResponse(s, readMs);
            return "OK probe " + all.length + " "
                    + android.util.Base64.encodeToString(all, android.util.Base64.NO_WRAP);
        } catch (Exception e) {
            return "ERR probe " + e.getClass().getSimpleName() + " "
                    + String.valueOf(e.getMessage());
        }
    }

    /**
     * 读到「响应完整」或超时为止。
     * 完整 = 头结束（\r\n\r\n）且 Content-Length 满足；没有 Content-Length 的
     * （HTTP 连接即关 / RTSP 例外）到头结束就停——**RTSP 是长连接，
     * 服务器发完不关，无脑死等会每次探测都白等一个满Timeout**。
     */
    private static byte[] readResponse(Socket s, int readMs) throws java.io.IOException {
        java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        byte[] tmp = new byte[4096];
        long deadline = System.currentTimeMillis() + readMs;
        while (buf.size() < PROBE_MAX_BYTES) {
            int left = (int) (deadline - System.currentTimeMillis());
            if (left <= 0) {
                break;
            }
            s.setSoTimeout(left);
            int n;
            try {
                n = s.getInputStream().read(tmp);
            } catch (java.net.SocketTimeoutException e) {
                break;
            }
            if (n < 0) {
                break;
            }
            buf.write(tmp, 0, n);
            byte[] all = buf.toByteArray();
            int headEnd = indexOf(all, "\r\n\r\n");
            if (headEnd >= 0) {
                String head = new String(all, 0, headEnd, "ISO-8859-1");
                java.util.regex.Matcher m = java.util.regex.Pattern
                        .compile("(?i)content-length:\\s*(\\d+)").matcher(head);
                if (!m.find()) {
                    break;                      // 无 Content-Length：头完即完整
                }
                if (all.length >= headEnd + 4 + Integer.parseInt(m.group(1))) {
                    break;                      // body 收全
                }
            }
        }
        return buf.toByteArray();
    }

    private static int indexOf(byte[] hay, String needle) {
        byte[] n = new byte[0];
        try {
            n = needle.getBytes("ISO-8859-1");
        } catch (java.io.UnsupportedEncodingException ignored) {
        }
        outer:
        for (int i = 0; i <= hay.length - n.length; i++) {
            for (int j = 0; j < n.length; j++) {
                if (hay[i + j] != n[j]) {
                    continue outer;
                }
            }
            return i;
        }
        return -1;
    }

    /**
     * PROBE_UDP <port> <payload-b64> [wait_ms] → 多行："OK mcount" / 每来源一行
     * "<ip> <base64>" / 末行 "END"（配 bridge_bulk 的多行读法）。
     * 用途：ONVIF WS-Discovery 组播 Probe——标准匿名设备发现机制，
     * 设计给匿名客户端，不携带任何凭据。
     */
    private static String probeUdp(String rest) {
        String[] p = rest.trim().split("\\s+", 3);
        if (p.length < 2) {
            return "ERR probe-udp-need port+payload(b64)";
        }
        int port;
        try {
            port = Integer.parseInt(p[0]);
        } catch (NumberFormatException e) {
            return "ERR probe-udp-port " + p[0];
        }
        int waitMs = 2000;
        if (p.length >= 3) {
            try {
                waitMs = Integer.parseInt(p[2]);
            } catch (NumberFormatException ignored) {
            }
        }
        waitMs = Math.max(500, Math.min(8000, waitMs));
        byte[] payload;
        try {
            payload = android.util.Base64.decode(p[1], android.util.Base64.DEFAULT);
        } catch (IllegalArgumentException e) {
            return "ERR probe-udp-b64";
        }
        StringBuilder sb = new StringBuilder("OK mcast");
        int n = 0;
        try (java.net.DatagramSocket ds = new java.net.DatagramSocket()) {
            ds.send(new java.net.DatagramPacket(payload, payload.length,
                    java.net.InetAddress.getByName("239.255.255.250"), port));
            long deadline = System.currentTimeMillis() + waitMs;
            while (n < 16 && System.currentTimeMillis() < deadline) {
                int left = (int) (deadline - System.currentTimeMillis());
                ds.setSoTimeout(Math.max(50, Math.min(left, 300)));
                byte[] buf = new byte[65535];
                java.net.DatagramPacket rp = new java.net.DatagramPacket(buf, buf.length);
                try {
                    ds.receive(rp);
                    byte[] data = java.util.Arrays.copyOf(rp.getData(), rp.getLength());
                    sb.append('\n').append(rp.getAddress().getHostAddress())
                      .append(' ')
                      .append(android.util.Base64.encodeToString(data,
                              android.util.Base64.NO_WRAP));
                    n++;
                } catch (java.net.SocketTimeoutException e) {
                    // 没到截止时间就继续等
                }
            }
        } catch (Exception e) {
            return "ERR probe-udp " + e.getClass().getSimpleName() + " "
                    + String.valueOf(e.getMessage());
        }
        sb.append("\nEND");
        return sb.toString();
    }

    // ------------------------------------------------------------ 蓝牙
    //
    // 只扫不连：BLUETOOTH_SCAN 带 neverForLocation，不申请定位也能扫，
    // 因为这里不拿蓝牙信号推位置。扫到的设备按 BLE 广播念出来，
    // 名字/厂商字段能认出耳机、手环、电视、门锁这类「身边的东西」。

    private BluetoothAdapter btAdapter() {
        BluetoothManager m = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
        return m == null ? null : m.getAdapter();
    }

    private String btState() {
        BluetoothAdapter a = btAdapter();
        if (a == null) return "ERR no-adapter";
        return "OK bt " + (a.isEnabled() ? "on" : "off");
    }

    private String ble(String rest) {
        int secs = 8;
        try {
            if (!rest.isEmpty()) secs = Math.max(1, Math.min(Integer.parseInt(rest.trim()), 30));
        } catch (NumberFormatException ignored) {
        }
        BluetoothAdapter a = btAdapter();
        if (a == null) return "ERR no-adapter";
        if (!a.isEnabled()) return "ERR bt-off";
        if (checkSelfPermission("android.permission.BLUETOOTH_SCAN") != PackageManager.PERMISSION_GRANTED) {
            return "ERR no-permission BLUETOOTH_SCAN";
        }
        final BluetoothLeScanner sc = a.getBluetoothLeScanner();
        if (sc == null) return "ERR no-scanner";

        // 同一个 MAC 会被反复广播，按地址留信号最强的那次
        final Map<String, ScanResult> seen = new LinkedHashMap<>();
        ScanCallback cb = new ScanCallback() {
            @Override
            public void onScanResult(int type, ScanResult r) {
                keep(seen, r);
            }

            @Override
            public void onBatchScanResults(List<ScanResult> rs) {
                if (rs != null) for (ScanResult r : rs) keep(seen, r);
            }
        };
        try {
            sc.startScan(cb);
        } catch (Exception e) {
            return "ERR scan-start " + e.getClass().getSimpleName();
        }
        try {
            Thread.sleep(secs * 1000L);
        } catch (InterruptedException ignored) {
        }
        try {
            sc.stopScan(cb);
        } catch (Exception ignored) {
        }

        StringBuilder sb = new StringBuilder("OK ble ");
        int n = 0;
        synchronized (seen) {
            for (ScanResult r : seen.values()) {
                if (n++ > 0) sb.append(";");
                String mac = "";
                try {
                    mac = r.getDevice().getAddress();
                } catch (SecurityException ignored) {
                }
                ScanRecord rec = r.getScanRecord();
                String name = rec == null ? null : rec.getDeviceName();
                String mfr = "";
                if (rec != null && rec.getManufacturerSpecificData() != null
                        && rec.getManufacturerSpecificData().size() > 0) {
                    int id = rec.getManufacturerSpecificData().keyAt(0);
                    mfr = String.format("%04x", id);
                }
                sb.append(mac).append(",").append(r.getRssi()).append(",")
                  .append(rec == null ? -127 : rec.getTxPowerLevel()).append(",")
                  .append(clean(name)).append(",").append(mfr);
            }
        }
        return sb.toString();
    }

    private static void keep(Map<String, ScanResult> seen, ScanResult r) {
        if (r == null || r.getDevice() == null) return;
        String mac;
        try {
            mac = r.getDevice().getAddress();
        } catch (SecurityException e) {
            return;
        }
        if (mac == null || mac.isEmpty()) return;
        synchronized (seen) {
            ScanResult old = seen.get(mac);
            if (old == null || r.getRssi() > old.getRssi()) seen.put(mac, r);
        }
    }

    /** 一行一个回复，字段分隔符不许出现在名字里。 */
    private static String clean(String s) {
        if (s == null) return "";
        return s.replace(";", " ").replace(",", " ").replace("\n", " ").replace("\r", " ").trim();
    }
}
