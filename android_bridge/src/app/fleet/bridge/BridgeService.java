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
import android.content.Context;
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
                case "IRINFO":
                    return irInfo();
                case "IR":
                    return ir(rest);
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
}
