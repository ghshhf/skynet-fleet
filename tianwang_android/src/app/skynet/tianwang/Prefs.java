package app.skynet.tianwang;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 三个旋钮：后端地址 + 令牌 + 走哪个 AI 后端。
 *
 * 令牌为什么不写死：后端每次启动都 secrets.token_urlsafe(32) 现生成一枚，
 * 只活在进程内存里（这是它的安全设计，不改）。所以手机这边必须能改，
 * 联调时电脑上用 FLEET_GUI_TOKEN 固定一枚，手机填同一枚即可。
 *
 * 后端为什么也要有旋钮：手机是语音对话，一次只念一句。默认走全部后端时
 * 三路同时回、同屏串台，念出来是乱的。GUI/TUI 那边多后端对比照旧不动。
 */
public final class Prefs {

    private static final String FILE = "tianwang";
    private static final String K_BASE = "base_url";
    private static final String K_TOKEN = "token";
    private static final String K_BACKEND = "backend";

    public static final String DEFAULT_BASE = "http://127.0.0.1:8000";
    /** 已拍板的默认：pollinations（免 key、不挑机器）。 */
    public static final String DEFAULT_BACKEND = "pollinations";

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

    public static void save(Context c, String base, String token, String backend) {
        sp(c).edit()
                .putString(K_BASE, base == null ? "" : base.trim())
                .putString(K_TOKEN, token == null ? "" : token.trim())
                .putString(K_BACKEND, backend == null ? "" : backend.trim())
                .apply();
    }
}
