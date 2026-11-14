# 天网 Next · 手机端（唯一在维护的 Android 工程）

Compose 原型，包名 `app.skynet.tianwang.next`。
第一代 `tianwang_android` 已于 2026-11-14 删除，能力和密钥都并到本工程，
手机上只剩这一个包在维护。

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
  ToolScope.kt          工具面裁剪：九域两级，60+ 个裁到 ≤8 个再喂模型
  WebTool.kt            出站联网：搜索 / 读页 / 多篇汇总（纯逻辑 + 执行）
  WebRender.kt          无头 WebView：JS 渲染后取 DOM，不弹界面
  VoiceInput.kt         16k 单声道录音 → WAV（老工程 WavRecorder 的 Kotlin 版）
  WavWriter.kt          WAV 头（纯字节，JVM 可测）
  NotificationWatch.kt  通知监听 + 最近 40 条本机内存缓存
  ActionLog.kt          远程动作审计：最近 50 条副作用操作（密钥隐藏、正文只留字数）
  PhoneLinkService.kt   入网前台服务：进程被回收后仍能常驻重连
  BootReceiver.kt       开机恢复上次开着的入网
  DebugBridge.kt        debug-only 广播验证桥：无屏幕注入权限时从电脑调工具 / 跑闭环
  MainActivity.kt       骨架 + 底部导航
```

## 出包

```bash
bash tianwang_app/build.sh          # release，约 2.2 MB
bash tianwang_app/build.sh debug    # debug，约 55 MB（含 tooling，仅调试用）
```

签名沿用第一代那份密钥 `tianwang_app/keystore.jks`（alias/pass 都是 `tianwang`，
随老工程删除一起迁到本工程根目录）。

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

老工程留下的配对能力已并入：电脑端界面上的 6 位配对码 →
`POST /api/pair/claim` → 换回 `device_id` + `secret`，存本机，之后每次请求带
`X-Fleet-Device: deviceId:secret`。配一次长期有效，电脑重启也不用重配。

- 入口：设置页「云侧连接」里的「配对这台手机」；已配对显示设备 id，可「解除配对」。
- 配对成功会清空旧 Token —— 两把钥匙都带会让「到底哪把在生效」变得不可查。
- token 仍保留：老电脑端只认 `X-Fleet-Token`，新电脑端认长期凭据，装一版同时对付两种。
- 请求头里**不打印 secret**，只落 SharedPreferences。

## 工具面裁剪（2026-11-09）

工具全量塞进提示词会让小模型选不动：4 个工具 5.6 秒、20 个 14.9 秒、58 个 56.2 秒，
而决策超时线是 25 秒。`ToolScope` 改成先按目标判域、域内排序截到 8 个再喂模型。

- 九域：界面操作 / 应用与系统 / 集群协作 / 模型与算力 / 网络接入 / 联网查资料 /
  网盘文件 / 通知与剪贴板 / 高权限。判不出域时给通用底座（看状态→找→点）。
- 九个域全部命中不了就走通用域，不会退回全量。
- 同一个动词属于不同域时用否决词切开：「网盘里搜一下」归网盘，不进联网域。
- **补位只补观察，不补动作**。域内工具不满 5 个时补 `self_status` / `self_diag` /
  `self_ui_dump`，补到 5 个就收，不为了凑满 8 个塞无关工具：联网域只有 3 个工具，
  补位曾经把 `self_ui_tap` 塞进去，模型读完正文看见列表里有 tap，就去点页面标题。
- 实测 system prompt 从约 2474 tokens 降到 293 tokens。

## 联网查资料（2026-11-09）

手机自己发请求，查外面。三个工具按「一次要读几篇」分工：

| 工具 | 什么时候用 | 行为 |
|---|---|---|
| `web_search` | 只有关键词、没有链接 | 解析 Bing 结果页；不引第三方 key，解析不到就显式失败，**不返回编造条目** |
| `web_read` | 一条链接、要原文 | 抽正文纯文本，可 `offset` 续读；正文优先 `<article>`/`<main>`，不够长才回落 `<body>`；`render=true` 换无头 WebView |
| `web_digest` | 一条到五条链接、要结论 | 自己抓正文 → 外文翻中文 → 按关注点抽要点，一次给全 |

`web_digest` 为什么不整篇翻译：最后给模型看的只有几百字要点，整篇按 450 字
一段翻要十几次请求；**先抽句、只翻抽出来的句子**，请求数降到 3～4 次，
时间从十几秒压到一两秒，翻译通道也不容易被限流。

每篇字数预算算成 `maxOf(下限, min(想要的, 总预算 ÷ 篇数))`。不能写成
`coerceIn(下限, 想要的)` —— 传进来的值小于下限时上下界会反过来，
工具直接抛 `Cannot coerce value to an empty range`。

三道兜底：

- 直连正文 <300 字（SPA / 空壳页）→ 自动换无头 WebView 渲染再抽，仍不行才报失败；
  渲染全程无界面、不占焦点，取完立即销毁；
- 翻译通道不可达 → 保留外文原句并在材料里标注「翻译暂不可达」，不假装翻过；
- 单篇失败单独记账，一篇挂了不影响其它篇 —— 给五条链接，四条有结果也比整批失败有用。

联网工具都只读，不依赖无障碍通道。

### 抽取质量：踩过的六个坑（2026-11-09）

网页解析是最容易「看起来跑通了、其实读的是垃圾」的一环。这六条都是真机实测踩出来的：

1. **HTML 软换行不是句子边界**。`<p>` 里为排版写的硬折行会被当成换行，
   一句话切成三四个碎片（「2、4、20 ）的类型为int ，」）。做法：先把源码换行收成空格，
   再按块级标签打换行 —— 顺序不能反。
2. **缩写句点不是句号**。`e.g.` / `i.e.` / `U.S.` 后面的句点会把句子劈成两截。
   切句前用哨兵字符保护，切完再还原。
3. **切分不能吃掉字符**。Java 的 `split` 会丢弃匹配到的分隔符，用 `\.` 直接匹配
   等于把每个句号从正文里删掉，译文全变成半截句。句子边界全部改成零宽断言。
4. **翻译缓存必须带内容指纹**。缓存是「url → {序号: 译文}」，抽句规则一改，
   同一个 url 抽出的句子就完全不同；只按 url 命中会把旧译文贴到新句子上
   （实测出现过「正文讲整数类型，译文却是上一版的代码行」），看起来像模型胡说。
5. **导航条会把焦点词吸走**。栏目串「热点追踪 | 中新真探 | 新闻浮世绘 | …」里
   正好含焦点词「新闻」，靠命中焦点词拿到最高分，把正文第一段挤掉，
   模型只能据此说「报道了国内外热点」这种空话。正文不会连用三个竖线 → 按噪音丢。
6. **失败不能包装成成功**。不少站点把「找不到」渲染成状态码 200 的页面，
   渲染通道不看状态码，404 页就被当成正文回灌，模型只能回一句「材料没提到」。
   短正文 + 明确的找不到字样 → 判失败，并明确告诉模型**不要自己拼 URL**。

配合工具面裁剪，联网闭环走「搜 → 读一条 → 回答」，连续工具步数上限放到 4。

## 本地推理 .so 构建（2026-11-07）

🔴 **改过 `llama_jni.cpp` 就必须重跑 `build-llama.sh`，否则一调模型就崩。**
`.so` 是构建产物，但它跟着仓库走，所以最容易出现「Kotlin 加了参数、`.so` 还是旧的」：
JNI 会按旧签名解析，多出来的字符串参数正好落到 `callback` 位置，
报 `JNI NewStringUTF called with pending exception: NoSuchMethodError "Ljava/lang/String;.onDelta"`，
整个进程 SIGABRT。2026-11-09 就是这个问题（源码 11-07 改的，`.so` 还是 10-26 的）。

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
- 产物落 `app/src/main/jniLibs/<abi>/`，**随代码入库**（`.gitignore:96` 有约定：
  运行时引擎入库备份，CI 构建完整 APK 要用）。所以重编后要一起提交，别只提交 Kotlin 侧改动。

## 本地模型工具闭环（2026-11-07）

无网也能跑：`LocalAgent` 在系统提示里给本地模型工具表，模型按
`{"tool":"self_ui_tap","arguments":{"query":"搜索"}}` 输出；解析成功就真执行，
把结果回灌给模型下一步，最多 4 步防打转（联网要「搜 → 读一条 → 回答」，3 步卡太死）。

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
本机 `whisper.cpp` 还没接（第一代那份随老工程删除，这项缺口回到零）。

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

- 本机 Whisper：语音录完目前走云侧识别；离线可用 sherpa-onnx 的 AAR（v2.0.1，GitHub Releases 直接发，不需要 JitPack）
- 对等审批：**不做**。手机不能给自己发通行证（那等于自己审自己）；要控另一台设备就走「远程控制台」的直连遥控，人点一下就是意图
- 远程控制台：手机对端已能看屏 + 点图即点屏 / 拖动即滑动 / 打字；PC 对端仍走 pcctl.*
- 悬浮窗：通知与剪贴板已接，悬浮窗/悬浮球还没做
