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
                case "IRINFO":
                    return irInfo();
                case "IR":
                    return ir(rest);
                case "BTSTATE":
                    return btState();
                case "BLE":
                    return ble(rest);
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
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return "ERR no-window 当前没有活动窗口（锁屏、下拉状态栏或系统弹窗遮住时会出现）";
        }
        int maxLines = 150;
        try {
            if (!rest.trim().isEmpty()) {
                maxLines = Math.max(20, Math.min(1200, Integer.parseInt(rest.trim().split("\\s+")[0])));
            }
        } catch (NumberFormatException ignored) {
        }

        long t0 = System.nanoTime();
        StringBuilder body = new StringBuilder(8192);
        int[] nodes = {0};
        render(root, "", body, nodes);
        long ms = (System.nanoTime() - t0) / 1000000L;

        String[] lines = body.toString().split("\n");
        int keep = Math.min(lines.length, maxLines);
        StringBuilder out = new StringBuilder(body.length() + 128);
        for (int i = 0; i < keep; i++) out.append(lines[i]).append('\n');
        if (keep < lines.length) {
            out.append("… 已截断 ").append(lines.length - keep)
               .append(" 行（本次遍历 ").append(nodes[0]).append(" 节点）\n");
        }

        CharSequence pkg = root.getPackageName();
        String head = "OK ui " + keep + " " + out.length() + " "
                + ms + "ms pkg=" + (pkg == null ? "?" : pkg);
        return head + "\n" + out + "END";
    }

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
        String vid = n.getViewIdResourceName();
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
