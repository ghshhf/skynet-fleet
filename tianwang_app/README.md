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

## 还没接的

- `LocalBackend` 只是桩：要接 llama.cpp JNI（照 `tianwang_android/jni/` 那套 CMake）
- `CloudBackend` 只是桩：要接 OpenAI 兼容 `/v1/chat/completions` 的 SSE 流
- `Demo.nodes` / `Demo.models` 是假数据：真链路要搬老工程 `Net.java` 的协议
