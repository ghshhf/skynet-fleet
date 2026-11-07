package app.skynet.tianwang.next

import java.nio.charset.StandardCharsets
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * `/pair` 中继的房间令牌。
 *
 * 与 `tools/cloudnode/wsrelay.py:pair_token` 必须逐字节一致：
 * HMAC-SHA256(secret, room) 的小写 hex 取前 32 位。
 *
 * 测试见 `PairTokenTest` —— 这里算错一个字符，现象是「中继一直 waiting」，
 * 从手机界面上看不出是 token 不对。
 */
object PairToken {
    fun of(room: String, secret: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(StandardCharsets.UTF_8), "HmacSHA256"))
        val digest = mac.doFinal(room.toByteArray(StandardCharsets.UTF_8))
        val hex = buildString(digest.size * 2) {
            for (b in digest) {
                val v = b.toInt() and 0xff
                append(HEX[v ushr 4])
                append(HEX[v and 0x0f])
            }
        }
        return hex.take(32)
    }

    private val HEX = "0123456789abcdef".toCharArray()
}
