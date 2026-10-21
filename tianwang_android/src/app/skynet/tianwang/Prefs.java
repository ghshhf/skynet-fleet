package app.skynet.tianwang;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * 两个旋钮：后端地址 + 令牌。
 *
 * 令牌为什么不写死：后端每次启动都 secrets.token_urlsafe(32) 现生成一枚，
 * 只活在进程内存里（这是它的安全设计，不改）。所以手机这边必须能改，
 * 联调时电脑上用 FLEET_GUI_TOKEN 固定一枚，手机填同一枚即可。
 */
public final class Prefs {

    private static final String FILE = "tianwang";
    private static final String K_BASE = "base_url";
    private static final String K_TOKEN = "token";

    public static final String DEFAULT_BASE = "http://127.0.0.1:8000";

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

    public static void save(Context c, String base, String token) {
        sp(c).edit()
                .putString(K_BASE, base == null ? "" : base.trim())
                .putString(K_TOKEN, token == null ? "" : token.trim())
                .apply();
    }
}
