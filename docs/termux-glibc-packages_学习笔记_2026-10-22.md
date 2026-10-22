# termux-pacman/glibc-packages 学习笔记（2026-10-22）

> 调研对象：`https://github.com/termux-pacman/glibc-packages`，浅克隆 `main`（`git log` 最新提交 2026-07-07 glibc 2.43）。
> 结论：**没有必须搬的东西，但它改写了我们 P2 的一个技术选项**——详见 §四。

---

## 一、它到底是什么

**在 Termux（bionic libc 的 Android 环境）里跑 glibc 程序的整套发行版。**

Termux 原生的二进制全部链 bionic。而绝大多数 Linux 桌面软件（官方预编译二进制、闭源 SDK）只发 glibc 版。
这个仓库把 glibc 2.43 和 230 个 glibc 链接的库/工具打成 pacman 包，让 Termux 能直接 `pacman -S`。

| 项 | 实测 |
|---|---|
| 包数 | **230**（`git ls-tree --name-only HEAD gpkg/ \| wc -l`） |
| 架构 | aarch64 / arm / x86_64 / i686 |
| 格式 | pacman（`repo.json` 声明 `pkg_format: pacman`） |
| 源 | `https://sync.termux-pacman.dev/{gpkg,main,x11,root}` |
| 目录 | `gpkg/` 主包、`cgct/` 交叉编译工具链、`scripts-cgct/` 构建脚本、`get-build-package.sh` 取构建环境 |
| 许可证 | LICENSE.md（仓库自身）；`glibc-runner` 单个包是 **GPL-3.0** |

值得注意的包：`glibc`、`glibc-runner`、`python`（**3.12.9**）、`clang`、`cmake`、`make`、`git`、`patchelf`、
`box64` / `box86`（x86 模拟）、`strace`、`mesa`/`vulkan-*`（图形栈）、`postgresql`、`apache2`。
实际包名带 `-glibc` 后缀（`bash-glibc`、`binutils-glibc`……），依赖里看得很清楚。

---

## 二、核心机制：`glibc-runner`（`grun`）——130 行 bash，三招

这是我们唯一真正值得读懂的部分。

### ① 不改二进制，直接用 glibc 的 ld.so 当启动器

```bash
exec ld.so $@          # GLIBC_PREFIX/lib/ld-linux-aarch64.so.1  <你的程序>
```

动态链接器的第一个参数就是"要加载的可执行文件"。所以**完全不用 patchelf、不用改任何文件**，
就能让 glibc 程序在一个 bionic 世界里跑起来。这是最省事的一招。

### ② `--configure` (-c)：改一次，之后直接跑

```bash
patchelf --set-rpath ${GLIBC_PREFIX}/lib \
         --set-interpreter ${GLIBC_PREFIX}/lib/ld-*  "$1"
```

把 interpreter 和 rpath 用 patchelf 写死进 ELF 头。适合"要反复启动的东西"（比如给某个工具做常驻）。

### ③ `--findlib` (-f)：按 NEEDED 递归凑 LD_LIBRARY_PATH

```bash
objdump -p "$1" | grep NEEDED        # 读出直接依赖
# 在 ${GLIBC_PREFIX}/lib + 当前目录里按文件名找，找到就把它的目录加进搜索路径
# 循环直到所有 NEEDED 都能找到
export LD_LIBRARY_PATH=...
```

这就是一个**手写的 ldd**。解决的是"闭源二进制把 .so 散落在自己目录里"的问题。

### ④ `--teg` (-t)：`LD_PRELOAD=libtermux-exec.so` 劫持 exec

glibc 进程里 fork/exec 出来的子进程，默认会掉回 bionic 的路径解析（`/system/bin`、`/data/data/.../usr/bin`），
导致"父进程能跑、子进程找不到"。`termux-exec` 用 LD_PRELOAD 改写 exec 系调用，把路径重映射过去。
用 `-t` 时才开，因为 LD_PRELOAD 会污染。

---

## 三、它和我们天网的接口在哪（三条，按实用度排序）

### ✅ ① P2 声学实验台 —— 最实用，且能省一个工作单元

计划书 P2 写的是「whisper.cpp 用 **NDK 交叉编译** arm64-v8a .so + JNI 薄封装」。
而 `gpkg` 里 **clang / cmake / make 都有** ⇒ 可以**在 K80 上原生编译** whisper.cpp：

```bash
pkg install clang cmake make git      # gpkg 源
git clone https://github.com/ggerganov/whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j4
./build/bin/whisper-cli -m ggml-small.bin -f test.wav
```

**这正好补上计划书 §七风险 3 的空缺**：「arm64 K80 上 small 级别模型的速度**未实测**，不能拍脑袋说够快」。
原生编译比 NDK 交叉编译快得多，而且**不进主链路**——只是量数据。

⚠️ 边界必须说清：Termux 里编出来的是 **glibc 链接的 ELF**，不是 `System.loadLibrary()` 能加载的 bionic `.so`。
所以它**不能**直接给 `tianwang_android` 的 Java 层用。它的身份是"选型实验台"，不是"产物来源"。

### ⚠️ ② P3 之后：手机本地跑 Python 侧逻辑 —— 备选，不进计划

`gpkg` 有 python 3.12.9 + pip。理论上能把 `agent_phone.py` 的决策循环搬到手机，真正离线。
代价：Termux 与我们的 APK 是两个世界（只能走 loopback/文件），MIUI 对 Termux 的后台杀得更狠
（需 wakelock + 关电池优化 + 自启白名单），且要用户手动装 Termux（Play 版不是它）。
⇒ **P1/P2/P3 都不依赖它**，留作将来的可选加速。

### 🔵 ③ `box64` / `box86`：将来接只有 x86 Linux 版的外部工具

能在 arm64 手机上跑 x86_64 Linux 程序。对天网的意义：将来若要接某个只有 x86 版的外部 SDK
（量化/行情/驱动），可以省一台 x86 机器。**属于"以后可能"，现在不动。**

---

## 四、它改写了 P2 的一个技术选项（**待 P2 开工时再定，现在不改计划**）

计划书 P2 只拍了「whisper.cpp + GGUF」，**没拍**「JNI 还是子进程」。这个仓库提示了选项 B：

| | 选项 A：NDK 编 .so + JNI（原计划） | 选项 B：Termux 编译 + 子进程/loopback |
|---|---|---|
| 交付物 | `.so` 进 APK，`System.loadLibrary` | `whisper-cli` 可执行文件，APK 通过 socket 喂 WAV、收文本 |
| 架构一致性 | 混进 App 进程 | **与现有「桥 APK + loopback」完全同构**（见计划书 §一①） |
| 最容易出事的层 | JNI 薄封装（崩溃、ABI、线程） | 无 JNI |
| 用户门槛 | 装 APK 即可 | 要装 Termux + 加 gpkg 源 + 自己编 |
| 结论 | 对用户最省事 | 对我最省事 |

⇒ **倾向仍是 A**（用户门槛优先），但把 B 写进 P2 的备选。**按顺序锁死，现在不动 P2。**

---

## 五、明确不搬

- `glibc` 本身、`cgct` 工具链（那是给 Termux 生态用的，我们不在 Termux 里做发行版）。
- `glibc-runner` 的 bash 实现（GPL-3.0；且我们是 Android app + PC Python，没有 bionic/glibc 混跑需求）。
- 唯一可借鉴的**思路**（不是代码）：`--findlib` 用 `objdump -p` 读 NEEDED 递归凑依赖路径 —— 将来若做
  「外部工具依赖自检」可以照这个思路写，不复制源码。

---

## 六、一句话结论

**这不是给我们用的库，是给我们做实验用的环境。** 它的价值不在代码，在于它证明了
「K80 上可以不碰 NDK，直接用 clang+cmake 编出 whisper.cpp 并量出真实速度」——
这正好是 P2 门禁里最缺的那组数字。

对当前 P1 无影响，继续按原计划走。
