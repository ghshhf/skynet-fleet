package app.skynet.tianwang.next

/**
 * 云侧地址的规范化与拼接。
 *
 * 单独抽成一个纯函数对象，有两个理由：
 *
 * 1. 原来每个调用点都自己写 `"${loadConfig().url}/api/xxx"`。地址只要能是空的，
 *    拼出来就是 `/api/xxx` —— `URL("/api/xxx")` 会直接抛
 *    `MalformedURLException: no protocol: /api/xxx`，用户看到的是「连接失败」而不是
 *    「你没填地址」。地址以 `/` 结尾还会拼出 `http://h:9124//api/xxx`。
 *    收敛到一处，才不会每个调用点各错一次。
 * 2. 这是纯函数，能在 JVM 单测里跑，不用连真机。
 */
object Api {

    /** 没配地址时的兜底：手机走 USB 反代，固定映射到本机 9124。 */
    const val DEFAULT_BASE = "http://127.0.0.1:9124"

    /**
     * 把用户填的地址收敛成「带协议、无尾斜杠」的 base。
     *
     * 空串返回空串（**不**兜底）—— 兜底只在 [endpoint] 做，这样「没配」和「配错」
     * 在 UI 上还能分得开：没配提示去填，配错提示连不上。
     */
    fun normalizeBase(raw: String): String {
        var s = raw.trim()
        if (s.isEmpty()) return ""
        // http://h:9124/// -> http://h:9124
        while (s.endsWith("/")) s = s.dropLast(1)
        if (s.isEmpty()) return ""
        // 用户常只填 127.0.0.1:9124，补个 http://，别让他为这个报错
        val hasScheme = s.startsWith("http://", ignoreCase = true) ||
            s.startsWith("https://", ignoreCase = true)
        return if (hasScheme) s else "http://$s"
    }

    /**
     * 拼一个端点。
     *
     * base 为空时退回 [DEFAULT_BASE]，保证永远拼出绝对地址 —— 哪怕配置丢了，
     * 报的也是「连不上 9124」这种能自己修的信息，而不是 `no protocol`。
     */
    fun endpoint(rawBase: String, path: String): String {
        val base = normalizeBase(rawBase).ifEmpty { DEFAULT_BASE }
        val p = path.trimStart('/')
        return "$base/$p"
    }
}
