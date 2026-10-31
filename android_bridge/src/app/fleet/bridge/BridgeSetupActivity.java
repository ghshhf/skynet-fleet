// 权限申请页 + 手动拉活入口。
//
// 两个职责，都写死在这里是因为它们共用"应用被拉到前台"这一个时机：
//
// ① 要运行时权限。MIUI 把 shell 的 GRANT_RUNTIME_PERMISSIONS 也收走了，
//    adb 的 pm grant 会被拒（和 WRITE_SECURE_SETTINGS 一个套路），
//    所以走正路：应用自己弹系统授权框，由控制端读屏点「允许」——
//    我们的无障碍服务本来就能点。
//
// ② 给电脑侧一个**不用人手**的拉活手段（2026-11-01 加）。
//    桥的进程被回收后，`settings put secure enabled_accessibility_services`
//    这代 ROM 直接拒（WRITE_SECURE_SETTINGS 被拒），`startservice` 也拒
//    （要 BIND_ACCESSIBILITY_SERVICE）—— 两条路都堵死。
//    剩下唯一能走的是 `am start`：shell 有 START_ACTIVITIES 权限，起 Activity 不受限。
//    Activity 一 onCreate 就把前台服务拉起来，进程回来了，无障碍服务随之重绑。
//    所以这个页面的 onCreate 必须先 ensure() 再去要权限。
//
// 界面只有一个字：申请完就自己关，不留在后台。
package app.fleet.bridge;

import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

public class BridgeSetupActivity extends Activity {

    private static final int REQ = 1001;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        // 先拉活，再谈权限：只要这行跑了，进程就回来了
        BridgeKeepAliveService.ensure(this);

        TextView tv = new TextView(this);
        tv.setText("正在申请权限");
        tv.setGravity(Gravity.CENTER);
        setContentView(tv);

        List<String> want = new ArrayList<>();
        want.add("android.permission.BLUETOOTH_SCAN");
        want.add("android.permission.BLUETOOTH_CONNECT");
        // Android 13+ 通知权限：没有它前台服务的通知不显示。
        // 服务照跑，但用户在通知栏看不见桥 —— 而被杀时那一行字正是唯一线索。
        if (Build.VERSION.SDK_INT >= 33) {
            want.add("android.permission.POST_NOTIFICATIONS");
        }

        boolean need = false;
        for (String p : want) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) need = true;
        }
        if (need) {
            requestPermissions(want.toArray(new String[0]), REQ);
        } else {
            finish();
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(code, perms, granted);
        finish();
    }
}
