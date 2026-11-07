# 天网 Next · 手机端第二代

Compose 原型。与 `tianwang_android/`（老的、能出货的、手搓构建那份）**并存**，
包名不同（`app.skynet.tianwang.next`），可以同时装在手机上对照。

## 为什么另起一份

老的手机端 6084 行 Java、`MainActivity` 单文件 2242 行、零第三方依赖、
aapt2+javac 手搓五步、产物一百多 KB。它瘦得漂亮，但**长不成 Operit 那种界面**：
手写 View 没有 Material3、没有声明式列表、没有动效。

要照 Operit 抄，就得有 Compose；要有 Compose，就得有 Gradle。
两条构建路硬塞进一份脚本只会把两条都弄坏 ⇒ 并存，追上了再谈替换。

## 三个融合（这是本工程存在的唯一理由）

上游两家都是一条腿：

| | 本地模型 | 云端模型 | 天网编队 |
|---|---|---|---|
| pocketpal-ai | ✅ GGUF | ❌ | ❌ |
| Operit | ❌（旁枝） | ✅ | ❌ |
| **天网 Next** | ✅ | ✅ | ✅ |

`Model.kt` 里 `ModelBackend` 接口 + `LocalBackend`/`CloudBackend` 两个实现，
`backendFor()` 是**全仓唯一一处**判本地还是云的地方。
UI 层永远不写 `if (是不是本地)` —— 一旦漏进 UI，第三个后端加进来就得改十处。

## 目录

```
app/src/main/java/app/skynet/tianwang/next/
  Theme.kt              配色/字体（深色为主）
  Model.kt              🔴 双后端抽象 —— 全案枢纽
  Chat.kt               数据类 + 演示数据
  ChatScreen.kt         聊天（思维链、工具卡片、MarkdownLite）
  ModelsScreen.kt       模型（本地/云同一列表 + SAF 导入 + 链接下载）
  ModelDownload.kt      链接直下 GGUF：.part → 体积校验 → 魔数校验 → 改名
  FleetScreen.kt        编队（上游两家都没有的一页）
  ToolsScreen.kt        工具箱：电脑侧 / 手机本机 两条能力面
  PhoneAccessService.kt 无障碍自持通道（读窗口、手势、截图）
  PhoneSelf.kt          本机能力面 + 执行（界面操作/等界面/应用清单/通知/剪贴板/电量/音量/网络/设备档案）
  PhoneSelfScreen.kt    「手机本机」页：通道开关 + 能力清单 + 单点执行 + 入网配置
  SelfAccess.kt         纯逻辑：树压缩 / 文字匹配 / 顺序稳定（JVM 可测）
  McpCore.kt            手机 MCP 协议层（initialize / tools/list / tools/call）
  PhoneMcp.kt           本机能力 → MCP 工具映射 + 执行
  PhoneMcpLink.kt       出站 WebSocket 连 /pair 中继，断线重连
  PairToken.kt          /pair HMAC 令牌（与 PC 侧逐字节一致）
  LocalToolProtocol.kt  本地模型工具协议：提示词 + 严格 JSON 解析
  LocalAgent.kt         本地模型工具闭环（调一次 → 执行 → 回灌 → 再生成）
  VoiceInput.kt         16k 单声道录音 → WAV（老工程 WavRecorder 的 Kotlin 版）
  WavWriter.kt          WAV 头（纯字节，JVM 可测）
  NotificationWatch.kt  通知监听 + 最近 40 条本机内存缓存
  ActionLog.kt          远程动作审计：最近 50 条副作用操作（密钥隐藏、正文只留字数）
  PhoneLinkService.kt   入网前台服务：进程被回收后仍能常驻重连
  BootReceiver.kt       开机恢复上次开着的入网
  MainActivity.kt       骨架 + 底部导航
```

## 出包

```bash
bash tianwang_app/build.sh          # release，约 2.2 MB
bash tianwang_app/build.sh debug    # debug，约 55 MB（含 tooling，仅调试用）
```

签名沿用 `tianwang_android/keystore.jks`（alias/pass 都是 `tianwang`）。

> debug 包 55 MB 不是代码大，是 `material-icons-extended` 里几千个图标；
> release 开了 R8 + shrinkResources 之后只留引用到的十几个 ⇒ 2.2 MB。

## 测试

```bash
cd tianwang_app
JAVA_HOME=<jdk17+> ./gradlew :app:testDebugUnitTest
```

75 个 JVM 单测，**不需要真机、不插 USB**，几十秒跑完：

| 文件 | 测什么 | 为什么值得测 |
|---|---|---|
| `ApiTest` | 云侧地址归一化 + 端点拼接 | `no protocol: /api/providers` 就是「空地址直接拼路径」造出来的；地址尾部的 `/` 还会拼出 `//api/xxx` |
| `SseParserTest` | SSE 事件切分（多行 `data:`、无尾空行、注释行） | 多行 data 直接 `append` 会粘成非法 JSON，再被 `runCatching` 静默吞掉 —— 表现是「对话没反应」，查起来极慢 |
| `JsonParseTest` | `/api/providers`、`/api/caps` 的解析 | 字段名错一个字母，界面就是一片空白，而空白跟「服务端真没数据」长得一模一样 |
| `UiTreeTest` | 无障碍树的压缩 / 文字与 id 匹配 / 正则容错 / 顺序稳定 | 树这层判断错了，真机表现是「点到了别的按钮」或「明明有字却找不到」，adb 日志里看不出来 |
| `McpCoreTest` | MCP 初始化 / 工具表 / 只读闸门 / 错误码 / 通知不执行 | 通知当请求执行、只读模式漏放行，都是「看起来能调、实际点错了」的安全事故 |
| `PairTokenTest` | `/pair` HMAC 向量 + 地址归一化 | token 错一位，真机表现只是「中继一直 waiting」，从手机界面上看不出原因 |
| `LocalToolProtocolTest` | 围栏 JSON / 前后废话 / 嵌套花括号 / 字符串化参数 / 非工具 JSON / GBNF 生成 / 半截工具调用判定 | 小模型最常见的坏习惯；解析乱猜会导致点错按钮；文法写错会让约束解码在设备上直接失效 |
| `WavWriterTest` | 44 字节 WAV 头 / 16k 单声道字段 / RIFF 长度 | 头写错后端的表现是「识别出乱码」，不是报错 |
| `PairClaimTest` | `/api/pair/claim` 请求字段 / 成功解析 / HTTP 与业务错误 / 缺凭据 | 配对失败时界面只显示一句原因；原因丢了就只能「再试一次」 |
| `PhoneMcpToolsTest` | 工具名唯一 / 新工具在册 / 写操作标记 / 只读闸门 | 漏标 sideEffect 会让只读模式偷偷改手机状态 |
| `PhoneNotificationTest` | 通知压缩成一行 / 缺 App 名 / 常驻标记 | 通知正文要喂给模型，空冒号和字段错位会让它读错验证码 |
| `ModelDownloadTest` | URL 文件名归一 / 成功落盘 / 非 GGUF 丢弃 / 体积不符 / 非 http | 半截 `.part` 冒充可用模型是最难查的那类故障，宁可下载时就判死 |
| `ActionLogTest` | 最近 50 条滚动 / 密钥隐藏 / 正文只留字数 / 失败格式 | 审计日志一旦把 token、输入正文原样存下来，本身就是新的泄漏源 |

真机侧（依赖手机，**不进门禁**）：

```bash
adb reverse tcp:9124 tcp:9124          # 手机 127.0.0.1:9124 → 电脑 9124
adb shell am start -n app.skynet.tianwang.next/.MainActivity
```

> MIUI 会拦 `adb shell input tap`（`INJECT_EVENTS`），除非开发者选项里开了
> 「USB 调试（安全设置）」。没开的话 `input tap` 静默无效，真机 UI 验证只能手点。
> 装包也一样：`adb install` 会返回 `INSTALL_FAILED_USER_RESTRICTED`，
> 改用 `am start -a android.intent.action.INSTALL_PACKAGE -d file:///sdcard/Download/x.apk`
> 就能直接弹安装器让手点。


## 手机自持通道（2026-11-07）

工具箱 →「手机本机」→ 按提示去系统设置开无障碍。不连电脑、不走 adb。

| 能力 | 说明 |
|---|---|
| `self.status` | 通道是否在线、当前前台包名、屏幕分辨率 |
| `self.ui.dump` | 当前窗口压成紧凑树（文字/描述/id/坐标/可点可滚可输入） |
| `self.ui.find` | 按文字 / 描述 / 控件 id 找，支持正则 |
| `self.ui.tap` | 点中匹配控件；不可点时向上找可点父容器，再不行按坐标点 |
| `self.ui.tap_at` | 按屏幕坐标点/长按（WebView、自绘界面） |
| `self.ui.swipe` | 按坐标滑动手势（翻页、拖拽、画轨迹） |
| `self.ui.text` | 往输入框写字；控件不认 `ACTION_SET_TEXT` 时自动退到剪贴板粘贴 |
| `self.ui.scroll` | 向前/向后滚一屏，没有可滚控件就滑屏幕 |
| `self.ui.key` | back / home / recents / notifications |
| `self.ui.wait` | 等界面出现指定文字/描述/id，超时报错（别再用盲等） |
| `self.app.launch` | 按包名打开 App |
| `self.app.open_url` | 用系统浏览器打开 http/https 链接（不是网页抓取） |
| `self.app.list` | 列出桌面可启动应用（名称 + 包名），可按名称/包名过滤 |
| `self.ui.screenshot` | 系统无障碍截图（Android 11+）：PNG 存本机；MCP 调用默认回传压缩 JPEG（最长边 1280） |
| `self.notif.list` / `clear` | 读最近通知（需系统「通知使用权」）；本机内存 40 条，进程重启清空 |
| `self.clipboard.get` / `set` | 读写系统剪贴板（后台读取可能被 Android 10+ 拒绝） |
| `self.battery.status` | 电量 / 充电 / 省电模式 / 是否在电池优化白名单 |
| `self.audio.volume` / `set_volume` | 读媒体音量 / 按百分比静默调音量 |
| `self.net.status` | Wi-Fi/蜂窝/以太网/VPN、是否计费、是否已验证可上网 |
| `self.screen.state` | 屏幕亮灭 / 是否锁屏（锁屏下无障碍点不到 App） |
| `self.device.info` | 厂商型号、Android 版本、ABI、包名、是否已长期配对 |
| `self.diag` | 全通道自检：无障碍、通知、权限、电池白名单、云侧、MCP、本地模型 |
| `self.audit` | 最近有副作用动作的审计记录（最新在上，敏感参数隐藏） |

验收 = 不连电脑：`app.launch` 打开设置 → `ui.dump` 读到控件 → `ui.tap` 点中「搜索/返回」。

边界：无障碍只能做**界面上能做的事**；装应用、改系统设置、跨 App shell 仍需 Shizuku（未做）。

## 入网：手机当 MCP Server（2026-11-07）

手机不开端口、不走 adb、不装 VPN：手机和 PC 各自**出站**连 `/pair` 中继，
配对后中继盲转发字节。手机端把 `self.*` 映射成 MCP 工具：

| MCP 工具 | 对应本机能力 | 默认 |
|---|---|---|
| `self_status` | `self.status` | 只读 |
| `self_ui_dump` / `self_ui_find` | `self.ui.dump` / `find` | 只读 |
| `self_ui_screenshot` | `self.ui.screenshot` | 只读；回包带 image 内容（JPEG） |
| `self_ui_wait` | `self.ui.wait` | 只读 |
| `self_ui_tap` / `self_ui_tap_at` / `self_ui_swipe` / `self_ui_text` / `self_ui_scroll` / `self_ui_key` | 对应 `self.ui.*` | 需开「允许操作」 |
| `self_app_launch` / `self_app_open_url` | `self.app.launch` / `open_url` | 需开「允许操作」 |
| `self_app_list` | `self.app.list` | 只读 |
| `self_notif_list` / `self_notif_clear` | `self.notif.list` / `clear` | 读只读；清缓存需开「允许操作」 |
| `self_clipboard_get` / `self_clipboard_set` | `self.clipboard.get` / `set` | 读只读；写需开「允许操作」 |
| `self_battery_status` | `self.battery.status` | 只读 |
| `self_audio_volume` / `self_audio_set_volume` | `self.audio.volume` / `set_volume` | 读只读；调音量需开「允许操作」 |
| `self_net_status` / `self_device_info` / `self_screen_state` / `self_diag` / `self_audit` | `self.net.status` / `device.info` / `screen.state` / `diag` / `audit` | 只读 |

手机：工具箱 →「手机本机」→「入网设置」填中继地址 / 房间 / 密钥，
三处与 PC 的 `WSRELAY_SECRET` 一致；「允许操作」默认关。

PC 侧：

```bash
# 看一眼通道（初始化 + tools/list）
python -m fleet.mcp.pair --url wss://relay.example.com/pair \
  --room phone-1 --secret "$WSRELAY_SECRET" --status --list

# 直接调一个工具
python -m fleet.mcp.pair --url ... --room ... --secret ... \
  --call self_ui_dump --args '{"limit":40}'

# 当 stdio MCP server 接到控制面（fleet/mcp/client.py 可发现的配置）
python -m fleet.mcp.pair --url ... --room ... --secret ... --serve-stdio
```

协议：JSON-RPC 2.0，配对后每行一个 UTF-8 JSON，二进制帧传输。
只读闸门在手机端执行，**不靠 PC 自觉**；关掉「允许操作」后写工具直接返回 `isError`。

## 设备配对 / 长期凭据（2026-11-07）

老工程的能力继续往第二代搬：电脑端界面上的 6 位配对码 →
`POST /api/pair/claim` → 换回 `device_id` + `secret`，存本机，之后每次请求带
`X-Fleet-Device: deviceId:secret`。配一次长期有效，电脑重启也不用重配。

- 入口：设置页「云侧连接」里的「配对这台手机」；已配对显示设备 id，可「解除配对」。
- 配对成功会清空旧 Token —— 两把钥匙都带会让「到底哪把在生效」变得不可查。
- token 仍保留：老电脑端只认 `X-Fleet-Token`，新电脑端认长期凭据，装一版同时对付两种。
- 请求头里**不打印 secret**，只落 SharedPreferences。

## 本地推理 .so 构建（2026-11-07）

本地 GGUF 推理要一个 `libfleet_llama.so`（llama.cpp 静态链进单库）。
`build-llama.sh` 已改成可复现构建，不再依赖某台机器的盘符：

```bash
cd tianwang_app
./build-llama.sh                       # 默认 arm64-v8a
ABIS="arm64-v8a armeabi-v7a" ./build-llama.sh
LLAMA_REF=v0.9.0 ./build-llama.sh      # 固定上游版本（默认就是这个）
```

- NDK / cmake / ninja 全部自动探测（也认 `ANDROID_NDK_HOME`、`ANDROID_HOME/ndk/*`）；
- 仓库里没有 llama.cpp 源码时，按 `LLAMA_REF` 浅克隆到 `third_party/llama.cpp`；
- 编之前校验上游 API 与 `llama_jni.cpp` 一致（penalties 签名、`llama_sampler_init_grammar`），
  上游改接口会立刻失败，而不是装到手机上才崩；
- 产物落 `app/src/main/jniLibs/<abi>/`。**这是构建产物，不进 git**：换机器重跑脚本即可。

## 本地模型工具闭环（2026-11-07）

无网也能跑：`LocalAgent` 在系统提示里给本地模型工具表，模型按
`{"tool":"self_ui_tap","arguments":{"query":"搜索"}}` 输出；解析成功就真执行，
把结果回灌给模型下一步，最多 3 步防打转。

三条边界：

- 解析失败**不猜**，原样当回答；工具名必须命中当前工具表。
- 通道不在线时不启用工具协议，避免模型拿到一串「没开」把回答带偏。
- `model.tools == false` 或只读入网时，有副作用的工具不会进提示词。

**GBNF 约束解码（2026-11-07）**：如果模型明显想调工具、但 JSON 写坏了
（漏右括号 / 多写解释），会用 `LocalToolProtocol.toolGrammar()` 生成的文法**重试一次**。
JNI 每轮重建采样链（`penalties → grammar → top_k → top_p → temp → dist`），
文法把在册工具名穷举成分支 —— 编造的工具名在采样层就生成不出来。
正常聊天**不加** grammar：长期强制 JSON 会让模型连话都不会说。

## 语音输入与常驻（2026-11-07）

语音：聊天页麦克风 → `AudioRecord` 16k/单声道/PCM16 → 本地 WAV →
`POST /api/voice/recognize`（`Content-Type: audio/wav`）→ 识别文字落回输入框。
权限是 `RECORD_AUDIO`，首次点麦克风时请求。**只搬了云侧识别**，
老工程的本机 `whisper.cpp` 还没接。

常驻：入网连接跑在 `PhoneLinkService` 前台服务里，通知常驻；
`BootReceiver` 开机按上次状态恢复（Android 15 对后台 dataSync 有限制，
失败会回落到进程内直连）。App 进程被杀后，系统按 `START_STICKY` 重启服务。

## 模型直链下载（2026-11-07）

模型页「添加本地模型」保留 SAF 导入（电脑推 / 网盘 / 聊天软件收到都能覆盖），
另加「从链接下载」：手机自己 HTTP(S) 下 GGUF，不经过电脑。

- 先写 `xxx.gguf.part`，体积（Content-Length / 预期值）与 GGUF 魔数都过了才改名；
- 失败一律删临时文件，**半截文件不许冒充可用模型**；
- 同名不覆盖，追加 `-1` / `-2`，避免悄悄换掉同名的另一个量化版本。

## 还没接的

- Shizuku 特权通道：装应用 / 改设置 / 跨 App shell（无障碍做不到的才用它）
- 真机验收：`.so` 需在带 NDK 的机器上编一次；断网工具闭环、`/pair` 公网中继、开机恢复都待真机跑
- 本机 Whisper：语音录完目前走云侧识别；离线可用 sherpa-onnx 的 AAR（v2.0.1，GitHub Releases 直接发，不需要 JitPack）
- PC 审批流：长期凭据已接，但「危险操作先让电脑点确认」还没做
- 悬浮窗：通知与剪贴板已接，悬浮窗/悬浮球还没做
- 老工程删除：等本工程把 `tianwang_android` 的能力接完再谈
