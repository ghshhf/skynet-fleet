package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.Brush
import androidx.compose.material.icons.filled.CloudQueue
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.VolumeOff
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 工具箱 —— 只摆「人点一下就做一件事」的动作。
 *
 * 2026-11-07 主人给的标准：**摆出来的界面是给人用的；给 AI / 别的模块调的
 * 能力可以有，但不一定要摆出来**。所以这里不再列电脑侧能力清单、本机能力清单，
 * 不再有坐标点击 / 滑动界面 / 往输入框写字，也不再有让人手填 JSON 参数的执行框。
 * 那些能力仍在 `PhoneSelf` / `PhoneMcp` / catalog 里，由本地模型与编队调用。
 *
 * 布局照国内外大厂的「服务 / 工具」宫格：一屏看完、不用往下滑、顶部不压一块固定区。
 */
@Composable
fun ToolsScreen(
    onNewChat: () -> Unit,
    onOpenSettings: () -> Unit,
    onOpenDevice: () -> Unit,
    onOpenAbout: () -> Unit,
    onOpenImage: () -> Unit,
    onOpenCloudDrive: () -> Unit,
    speakOn: Boolean,
    onToggleSpeak: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var info by remember { mutableStateOf("") }

    val actions = listOf(
        ToolAction("新对话", "开一段新的", Icons.Filled.Add) { onNewChat() },
        ToolAction("抓取资源", "自己联网抓一轮", Icons.Filled.Download) {
            scope.launch {
                info = "抓取中…"
                runCatching { ResourceApi.crawl() }
                    .onSuccess { o ->
                        info = "抓了 ${o.optInt("sources")}/${o.optInt("sources_total")} 个源，" +
                            "新增 ${o.optInt("new")} 条"
                    }
                    .onFailure { info = it.message ?: "抓取失败" }
            }
        },
        ToolAction("大模型端点", "添加 / 密钥 / 模型", Icons.Filled.CloudQueue) {
            onOpenSettings()
        },
        ToolAction("本机出图", "提示词 → 本机 NPU 出片", Icons.Filled.Brush) {
            onOpenImage()
        },
        ToolAction("朗读", if (speakOn) "当前：开" else "当前：关",
            if (speakOn) Icons.Filled.VolumeUp else Icons.Filled.VolumeOff) {
            onToggleSpeak()
        },
        ToolAction("设备与权限", "授权 / 通知 / 入网", Icons.Filled.PhoneAndroid) {
            onOpenDevice()
        },
        ToolAction("版本与更新", "关于与后端", Icons.Filled.Info) {
            onOpenAbout()
        },
        ToolAction("网盘聚合", "集中各网盘搜索", Icons.Filled.Folder) {
            onOpenCloudDrive()
        },
    )

    LazyVerticalGrid(
        columns = GridCells.Fixed(2),
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 14.dp, vertical = 14.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Text("常用", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 2.dp, bottom = 2.dp))
        }
        item { Text("") }
        items(actions) { a -> ToolCard(a) }
        item {
            if (info.isNotBlank()) {
                Text(info, fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 2.dp, top = 4.dp))
            }
        }
    }
}

private data class ToolAction(
    val title: String,
    val sub: String,
    val icon: ImageVector,
    val run: () -> Unit,
)

@Composable
private fun ToolCard(a: ToolAction) {
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = a.run)
            .padding(horizontal = 13.dp, vertical = 15.dp),
    ) {
        Icon(a.icon, null, tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(23.dp))
        Spacer(Modifier.height(11.dp))
        Text(a.title, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(3.dp))
        Text(a.sub, fontSize = 10.5.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** 给底层链路 / AI 调用用的参数转换，不摆到人看的界面里。
 *  PhoneSelfScreen 的手填参数面板仍直接复用它；工具箱本身已不再展示这一层。 */
fun JSONObject.toMap(): Map<String, Any> {
    val m = mutableMapOf<String, Any>()
    val it = keys()
    while (it.hasNext()) { val k = it.next(); m[k] = opt(k) ?: JSONObject.NULL }
    return m
}
