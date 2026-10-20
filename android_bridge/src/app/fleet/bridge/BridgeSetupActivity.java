// 权限申请页：只为把「附近的设备」这个运行时权限要下来。
//
// MIUI 把 shell 的 GRANT_RUNTIME_PERMISSIONS 也收走了，adb 的 pm grant 会被拒
// （和 WRITE_SECURE_SETTINGS 一个套路）。所以走正路：应用自己弹系统授权框，
// 由控制端读屏点「允许」——我们的无障碍服务本来就能点。
//
// 界面只有一个字：申请完就自己关，不留在后台。
package app.fleet.bridge;

import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

public class BridgeSetupActivity extends Activity {

    private static final int REQ = 1001;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        TextView tv = new TextView(this);
        tv.setText("正在申请蓝牙权限");
        tv.setGravity(Gravity.CENTER);
        setContentView(tv);

        String[] want = new String[]{
                "android.permission.BLUETOOTH_SCAN",
                "android.permission.BLUETOOTH_CONNECT",
        };
        boolean need = false;
        for (String p : want) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) need = true;
        }
        if (need) {
            requestPermissions(want, REQ);
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
