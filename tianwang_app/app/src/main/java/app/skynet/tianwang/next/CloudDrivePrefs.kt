package app.skynet.tianwang.next

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/** 一个网盘连接：OpenList 服务地址+账号密码，或 WebDAV 地址+账号密码。 */
data class CloudConn(
    val type: String, // "openlist" | "webdav"
    val name: String,
    val host: String,
    val user: String,
    val pass: String,
)

/** 文件条目（统一两种后端的返回形状）。 */
data class FileItem(
    val name: String,
    val path: String,
    val isDir: Boolean,
    val size: Long,
)

/**
 * 网盘连接只存本机 SharedPreferences（私有）；secret（密码）绝不进日志 / 界面以外的地方。
 * 引用天网既有的 App.ctx，不另开 Context 来源。
 */
object CloudDrivePrefs {
    private const val SP = "cloud_drive_conns"
    private const val KEY = "conns"
    private val sp: SharedPreferences
        get() = App.ctx.getSharedPreferences(SP, Context.MODE_PRIVATE)

    fun load(): List<CloudConn> {
        val arr = JSONArray(sp.getString(KEY, "[]") ?: "[]")
        val out = ArrayList<CloudConn>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            out.add(
                CloudConn(
                    type = o.optString("type", "openlist"),
                    name = o.optString("name"),
                    host = o.optString("host"),
                    user = o.optString("user"),
                    pass = o.optString("pass"),
                )
            )
        }
        return out
    }

    fun saveAll(list: List<CloudConn>) {
        val arr = JSONArray()
        for (c in list) {
            arr.put(
                JSONObject().apply {
                    put("type", c.type); put("name", c.name); put("host", c.host)
                    put("user", c.user); put("pass", c.pass)
                }
            )
        }
        sp.edit().putString(KEY, arr.toString()).apply()
    }
}
