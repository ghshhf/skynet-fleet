package app.skynet.tianwang.next

import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject

/**
 * 对端 hello 能看到的自述：机型、版本、能力数、工具名、电量、充电、亮屏。
 *
 * 工具名只给名字，不给参数与实现；口令永远不在 hello 里。
 * 这些字段只用于「这台适不适合接这一单」，不承担鉴权；
 * 「能不能写」由控制方那一档决定，执行方只报告自己有哪些工具。
 */
object LanMeta {
    fun hello(): JSONObject = runCatching {
        val ctx = App.ctx
        val bm = ctx.getSystemService(BatteryManager::class.java)
        val pm = ctx.getSystemService(PowerManager::class.java)
        val tools = JSONArray()
        PhoneMcp.toolList().forEach { tools.put(it.name) }
        JSONObject()
            .put("platform", (Build.MANUFACTURER + " " + Build.MODEL).trim())
            .put("android", Build.VERSION.RELEASE ?: "")
            .put("caps", PhoneMcp.toolCount())
            .put("battery", bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1)
            .put("charging", bm?.isCharging == true)
            .put("screenOn", pm?.isInteractive == true)
            .put("tools", tools)
    }.getOrDefault(JSONObject())
}
