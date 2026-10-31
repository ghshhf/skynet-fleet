// 常驻壳兼控制端口：进程活着的这一层，和"能不能注入"彻底分开。
//
// 为什么端口开在这里，而不是无障碍服务里（2026-11-01 改）：
//   原先两件事挤在一个服务上，「端口通不通」同时意味着「进程活没活」和
//   「无障碍开没开」。可它们坏起来完全不是一回事：
//     · 进程被回收   → 端口消失，但 `settings get secure enabled_accessibility_services`
//                      里还写着「已启用」，电脑侧读到 enabled=True 就判定「本来就在」，
//                      然后连不上端口，报出来是「桥被系统杀了，或 adb forward 没建」——
//                      三件事挤一句话里，等于没说。
//     · 无障碍被关闭 → 进程还在、端口还在，但 dispatchGesture 什么都点不动。
//   分开之后每种故障都能被单独认出来：
//     端口连不上      = 进程没了（该拉活）
//     端口通但 ERR no-a11y = 进程在，无障碍没开（该去设置里点一下）
//     动作正常        = 真能用
//
// 为什么必须有前台服务（不是可选优化）：
//   HyperOS 回收这个进程之后，没有任何 adb 通路能把它拉回来——
//   写 secure settings 重开会直接拒（WRITE_SECURE_SETTINGS 被拒），
//   startservice 也拒（要 BIND_ACCESSIBILITY_SERVICE）。
//   前台服务 + START_STICKY 是唯一一条"不用人手"的路：
//   进程被杀了系统会把服务重建回来，重建后端口自己就回来了。
//
// 不做的事：不联网、不读任何内容、不持 wake lock。唯一副作用是通知栏一行字。
package app.fleet.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.provider.Settings;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class BridgeKeepAliveService extends Service {

    private static final String TOKEN = "__TOKEN__";
    private static final int PORT = 8765;

    private static final String CHANNEL_ID = "fleet-bridge-alive";
    private static final int NOTIF_ID = 0x4654;          // 'F''T'

    private final ExecutorService pool = Executors.newCachedThreadPool();
    private volatile boolean running = true;
    private volatile ServerSocket server;

    /**
     * 把进程抬成前台。任何入口（无障碍服务连上、权限页被拉起）都可以调，幂等。
     *
     * 一定要 try/catch：Android 12+ 的「后台启动前台服务」限制会抛
     * ForegroundServiceStartNotAllowedException。真被拦了也不该把桥带崩 ——
     * 掉回普通 startService，服务照样活，只是优先级少一层，比直接挂掉强。
     */
    public static void ensure(Context ctx) {
        if (ctx == null) return;
        Intent i = new Intent(ctx, BridgeKeepAliveService.class);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i);
            } else {
                ctx.startService(i);
            }
        } catch (Exception e) {
            try {
                ctx.startService(i);
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channel(this);
        try {
            Notification n = build(this);
            if (Build.VERSION.SDK_INT >= 34) {
                // targetSdk 35 下 startForeground 必须带 type。
                // 用 specialUse 而不是 dataSync：dataSync 在 Android 14+ 有
                // 每 24 小时 6 小时的配额，跑满就再也起不来 —— 对一个要求
                // 「一直在」的桥来说那是定时炸弹。specialUse 没有时长配额。
                startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTIF_ID, n);
            }
        } catch (Exception e) {
            // 前台化失败不阻止服务运行：进程活着这件事本身就还有价值
        }
        listen();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // 被杀后由系统重建，重建时 intent 为 null —— 这也是正常路径，不要当异常处理。
        // 重建会走 onCreate，端口跟着回来。
        return START_STICKY;
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
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ------------------------------------------------------------ 控制通道

    private void listen() {
        if (server != null) return;                      // 已经在听了，别重开（会 Address already in use）
        Thread t = new Thread(() -> {
            try {
                server = new ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"));
                while (running) {
                    Socket s = server.accept();
                    pool.execute(() -> handle(s));
                }
            } catch (Exception e) {
                // 端口被占或服务被停：静默退出，不打扰用户
            }
        }, "fleet-bridge-accept");
        t.setDaemon(true);
        t.start();
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
                // 两条指令在**这一层**就答，不下发到无障碍服务（2026-11-01）：
                //   PING 问的是「进程活着吗」。如果它也去问无障碍，那么进程活着
                //   但无障碍没开时 PING 就失败 —— 电脑侧又把两种故障看成一回事了，
                //   这次改造就白做。
                //   A11Y 是把「能不能点」单独拿出来问的一句，与 PING 互补。
                String up = line.toUpperCase();
                if (up.equals("PING")) {
                    out.println("PONG");
                    continue;
                }
                if (up.equals("A11Y") || up.equals("STATE")) {
                    out.println(BridgeService.ready()
                            ? "OK a11y ready"
                            : "ERR no-a11y 无障碍服务未连接（手机上 设置 → 无障碍 → 编队桥 打开）");
                    continue;
                }
                // 其余指令要注入能力，住在无障碍服务里；它没连上时这里会回
                // ERR no-a11y，而不是让连接直接失败 —— 「端口通」和「能点」是两件事。
                out.println(BridgeService.dispatch(line));
            }
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------ 通知

    private static void channel(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel c = new NotificationChannel(
                CHANNEL_ID, "编队桥", NotificationManager.IMPORTANCE_LOW);
        c.setDescription("电脑点按本机屏幕时的常驻状态提示");
        c.setShowBadge(false);
        c.enableVibration(false);
        c.setSound(null, null);
        nm.createNotificationChannel(c);
    }

    private static Notification build(Context ctx) {
        // 点通知直接跳到无障碍设置：桥真被关了，人第一眼就能在这里重开
        Intent go = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
        go.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getActivity(ctx, 0, go, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(ctx, CHANNEL_ID)
                : new Notification.Builder(ctx);
        b.setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentTitle("编队桥运行中")
                .setContentText("电脑可点按本机 · 127.0.0.1:8765")
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(pi);
        return b.build();
    }
}
