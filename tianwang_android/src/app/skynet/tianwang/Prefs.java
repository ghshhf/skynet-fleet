package app.skynet.tianwang;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 这台手机连哪台电脑、拿什么进门、聊哪个后端。
 *
 * 凭据分两种，别混：
 *   · token        —— 电脑那枚一次性令牌，**每次电脑重启就变**。
 *                     现在只当兜底（本机调试、老版本电脑端）。
 *   · deviceId/Secret —— 配对换来的长期凭据。配一次管到被撤销为止。
 *                     电脑重启不变 ⇒ 主人不用天天重抄令牌。
 *
 * 为什么长期凭据要存在手机上而不是"每次重新配对"：
 * 配对码是「新设备进门」那一趟用的，进完门就该一直有钥匙。
 * 每次开机重配不是安全，是麻烦——麻烦的最后结果是有人把令牌贴到便签上。
 */
public final class Prefs {

    private static final String FILE = "tianwang";
    private static final String K_BASE = "base_url";
    private static final String K_TOKEN = "token";
    private static final String K_BACKEND = "backend";
    private static final String K_DEV_ID = "device_id";
    private static final String K_DEV_SEC = "device_secret";
    private static final String K_DEV_NAME = "device_name";
    private static final String K_LOCAL = "local_mode";
    private static final String K_LOCAL_BASE = "local_base";

    public static final String DEFAULT_BASE = "http://127.0.0.1:8000";
    /** 已拍板的默认：pollinations（免 key、不挑机器）。 */
    public static final String DEFAULT_BACKEND = "pollinations";
    /**
     * 本地模型（MCB）默认打手机自己的 llama-server。Termux 里起一个
     * `llama-server -m ... --host 127.0.0.1 --port 8080` 就能用，
     * OpenAI 兼容，端口照这个走。
     */
    public static final String DEFAULT_LOCAL = "http://127.0.0.1:8080";

    private Prefs() { }

    private static SharedPreferences sp(Context c) {
        return c.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public static String baseUrl(Context c) {
        String v = sp(c).getString(K_BASE, DEFAULT_BASE);
        if (v == null || v.trim().isEmpty()) { return DEFAULT_BASE; }
        v = v.trim();
        while (v.endsWith("/")) { v = v.substring(0, v.length() - 1); }
        return v;
    }

    public static String token(Context c) {
        String v = sp(c).getString(K_TOKEN, "");
        return v == null ? "" : v.trim();
    }

    public static String backend(Context c) {
        String v = sp(c).getString(K_BACKEND, DEFAULT_BACKEND);
        if (v == null || v.trim().isEmpty()) { return DEFAULT_BACKEND; }
        return v.trim();
    }

    // ---------------- 长期设备凭据（配对得来） ----------------

    public static String deviceId(Context c) {
        String v = sp(c).getString(K_DEV_ID, "");
        return v == null ? "" : v.trim();
    }

    public static String deviceSecret(Context c) {
        String v = sp(c).getString(K_DEV_SEC, "");
        return v == null ? "" : v.trim();
    }

    public static String deviceName(Context c) {
        String v = sp(c).getString(K_DEV_NAME, "");
        if (v == null || v.trim().isEmpty()) {
            String m = android.os.Build.MODEL;
            return (m == null || m.isEmpty()) ? "这台手机" : m;
        }
        return v.trim();
    }

    public static boolean paired(Context c) {
        return !deviceId(c).isEmpty() && !deviceSecret(c).isEmpty();
    }

    /** 存一次配对结果。这是「配一次、之后不再配」的落点。 */
    public static void saveDevice(Context c, String id, String secret, String name) {
        sp(c).edit()
                .putString(K_DEV_ID, id == null ? "" : id.trim())
                .putString(K_DEV_SEC, secret == null ? "" : secret.trim())
                .putString(K_DEV_NAME, name == null ? "" : name.trim())
                .apply();
    }

    /** 忘掉配对（这台设备被撤销、或者要换电脑）。 */
    public static void clearDevice(Context c) {
        sp(c).edit()
                .remove(K_DEV_ID)
                .remove(K_DEV_SEC)
                .remove(K_DEV_NAME)
                .apply();
    }

    public static void save(Context c, String base, String token, String backend) {
        sp(c).edit()
                .putString(K_BASE, base == null ? "" : base.trim())
                .putString(K_TOKEN, token == null ? "" : token.trim())
                .putString(K_BACKEND, backend == null ? "" : backend.trim())
                .apply();
    }

    // ---------------- 本地模型（MCB）：手机自己跑，不连电脑 ----------------

    /** 是否走本地模型：默认关，开着就完全不打电脑、不联网。 */
    public static boolean localMode(Context c) {
        return sp(c).getBoolean(K_LOCAL, false);
    }

    /** 本地模型地址，默认 DEFAULT_LOCAL（手机自带 llama-server 的 8080）。 */
    public static String localBase(Context c) {
        String v = sp(c).getString(K_LOCAL_BASE, DEFAULT_LOCAL);
        if (v == null || v.trim().isEmpty()) { return DEFAULT_LOCAL; }
        v = v.trim();
        while (v.endsWith("/")) { v = v.substring(0, v.length() - 1); }
        return v;
    }

    public static void setLocalMode(Context c, boolean on) {
        sp(c).edit().putBoolean(K_LOCAL, on).apply();
    }

    public static void setLocalBase(Context c, String base) {
        sp(c).edit().putString(K_LOCAL_BASE, base == null ? "" : base.trim()).apply();
    }
}
