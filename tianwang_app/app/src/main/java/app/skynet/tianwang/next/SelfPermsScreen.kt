package app.skynet.tianwang.next

import android.Manifest
import android.content.pm.PackageManager
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
 * 应用权限自查：列出本应用已授予 / 未授予的危险权限（直接在端上查）。
 * 纯 Android API，不依赖 proot、不联网。
 */
@Composable
fun SelfPermsScreen() {
    val ctx = LocalContext.current
    val rows = remember {
        listOf(
            Manifest.permission.READ_EXTERNAL_STORAGE to "读取外部存储",
            Manifest.permission.WRITE_EXTERNAL_STORAGE to "写入外部存储",
            Manifest.permission.CAMERA to "相机",
            Manifest.permission.RECORD_AUDIO to "麦克风",
            Manifest.permission.ACCESS_FINE_LOCATION to "精确定位",
            Manifest.permission.ACCESS_COARSE_LOCATION to "粗略定位",
            Manifest.permission.READ_CONTACTS to "读取联系人",
            Manifest.permission.READ_PHONE_STATE to "电话状态",
            Manifest.permission.READ_SMS to "读取短信",
            Manifest.permission.POST_NOTIFICATIONS to "发送通知",
        ).map { (perm, label) ->
            val granted = ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED
            SelfPermRow(label, perm, granted)
        }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("应用权限自查", "本应用已授予 / 未授予的危险权限。直接在端上查，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureSection("权限（${rows.size}）")
        rows.forEach { r ->
            FeatureCard {
                Text(r.label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                Text(r.perm, fontSize = 9.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(3.dp))
                Text(
                    if (r.granted) "已授予" else "未授予",
                    fontSize = 11.sp,
                    color = if (r.granted) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error,
                )
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private data class SelfPermRow(val label: String, val perm: String, val granted: Boolean)
