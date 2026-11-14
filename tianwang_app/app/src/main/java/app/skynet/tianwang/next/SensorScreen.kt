package app.skynet.tianwang.next

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorManager
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 传感器清单：直接读 SensorManager 列出机身传感器（名称 / 类型 / 供应商 / 量程 / 功耗）。
 * 比 dumpsys 可靠（普通 App 就能拿全列表，不要求 DUMP 权限），不依赖 proot。
 */
@Composable
fun SensorScreen() {
    val ctx = LocalContext.current
    val sensors = remember {
        val sm = ctx.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
        sm?.getSensorList(Sensor.TYPE_ALL) ?: emptyList()
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("传感器清单", "机身传感器（名称 / 类型 / 供应商 / 量程）。直接读 SensorManager，不依赖 dumpsys")
        Spacer(Modifier.height(8.dp))
        if (sensors.isEmpty()) {
            Text("没有读到传感器（SensorManager 不可用）。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            FeatureSection("共 ${sensors.size} 个")
            sensors.forEach { s ->
                FeatureCard {
                    Text(s.name, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(2.dp))
                    Text("类型：${sensorTypeName(s.type)} · 供应商：${s.vendor.ifBlank { "—" }}",
                        fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(2.dp))
                    Text("最大量程：${s.maximumRange} · 功耗：${"%.2f".format(s.power)} mA · 分辨率：${s.resolution}",
                        fontSize = 9.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(4.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}

private fun sensorTypeName(t: Int): String = when (t) {
    Sensor.TYPE_ACCELEROMETER -> "加速度计"
    Sensor.TYPE_MAGNETIC_FIELD -> "磁力计"
    Sensor.TYPE_ORIENTATION -> "方向"
    Sensor.TYPE_GYROSCOPE -> "陀螺仪"
    Sensor.TYPE_LIGHT -> "光线"
    Sensor.TYPE_PRESSURE -> "压力"
    Sensor.TYPE_TEMPERATURE -> "温度"
    Sensor.TYPE_PROXIMITY -> "距离"
    Sensor.TYPE_GRAVITY -> "重力"
    Sensor.TYPE_LINEAR_ACCELERATION -> "线性加速度"
    Sensor.TYPE_ROTATION_VECTOR -> "旋转矢量"
    Sensor.TYPE_RELATIVE_HUMIDITY -> "相对湿度"
    Sensor.TYPE_AMBIENT_TEMPERATURE -> "环境温"
    Sensor.TYPE_MAGNETIC_FIELD_UNCALIBRATED -> "磁力计(未校准)"
    Sensor.TYPE_GYROSCOPE_UNCALIBRATED -> "陀螺仪(未校准)"
    Sensor.TYPE_STEP_COUNTER -> "计步"
    Sensor.TYPE_STEP_DETECTOR -> "计步检测"
    Sensor.TYPE_SIGNIFICANT_MOTION -> "显著运动"
    Sensor.TYPE_HEART_RATE -> "心率"
    Sensor.TYPE_HEART_BEAT -> "心跳"
    else -> "类型#$t"
}
