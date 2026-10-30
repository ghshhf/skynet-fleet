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
  Theme.kt        配色/字体（深色为主）
  Model.kt        🔴 双后端抽象 —— 全案枢纽
  Chat.kt         数据类 + 演示数据
  ChatScreen.kt   聊天（思维链、工具卡片、MarkdownLite）
  ModelsScreen.kt 模型（本地/云同一列表）
  FleetScreen.kt  编队（上游两家都没有的一页）
  MainActivity.kt 骨架 + 底部导航
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

17 个 JVM 单测，**不需要真机、不插 USB**，几十秒跑完：

| 文件 | 测什么 | 为什么值得测 |
|---|---|---|
| `ApiTest` | 云侧地址归一化 + 端点拼接 | `no protocol: /api/providers` 就是「空地址直接拼路径」造出来的；地址尾部的 `/` 还会拼出 `//api/xxx` |
| `SseParserTest` | SSE 事件切分（多行 `data:`、无尾空行、注释行） | 多行 data 直接 `append` 会粘成非法 JSON，再被 `runCatching` 静默吞掉 —— 表现是「对话没反应」，查起来极慢 |
| `JsonParseTest` | `/api/providers`、`/api/caps` 的解析 | 字段名错一个字母，界面就是一片空白，而空白跟「服务端真没数据」长得一模一样 |

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


## 还没接的

- `LocalBackend` 只是桩：要接 llama.cpp JNI（照 `tianwang_android/jni/` 那套 CMake）
- `CloudBackend` 只是桩：要接 OpenAI 兼容 `/v1/chat/completions` 的 SSE 流
- `Demo.nodes` / `Demo.models` 是假数据：真链路要搬老工程 `Net.java` 的协议
