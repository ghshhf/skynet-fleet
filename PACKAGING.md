# 打包（X6）

本仓库的红线之一：**零构建、断网可用**——用户拿到的是源码 + `requirements.txt`，
在本机 `pip install -r requirements.txt` 后直接 `python fleet_gui.py` / `python -m fleet` 就能跑，
不需要任何构建链（没有 Electron / Tauri / 前端 bundler）。

但图形版（Windows GUI）和终端版（TUI）也提供 **PyInstaller 冻扁成单文件 exe** 的路径，
方便分发给不想装 Python 的人。冻扁产物**不入库**（`build/ dist/ *.spec *.exe` 已在 `.gitignore`），
只在 CI（`windows-latest`）上构建并作为 artifact / release 资产发布。

---

## 1. 谁负责构建

| 形态 | 入口脚本 | CI 工作流 | 产物 |
|------|----------|-----------|------|
| 终端版 TUI | `fleet_tui.py` | `.github/workflows/build-tui.yml` | `fleet-tui.exe` |
| 图形版 GUI | `fleet_gui.py` | `.github/workflows/build-gui.yml` | `fleet-gui.exe` |

两个工作流都在 `push` 到 `main` / 打 `v*` tag / 手动触发时跑。

---

## 2. 图形版打包怎么配

配方全部钉在 **`tools/build_gui.py`**（PyInstaller 的程序化入口，不是散落的命令行）：
- `--paths <repo根>`：让根下平铺模块（`persona` / `persona_card` / `memory` 等被 `fleet` 包内
  代码用顶层 `import` 拉进来的模块）能被解析——它们不在 `fleet` 包里。
- `--collect-submodules fleet`：整包收进来（含动态子模块）。
- `--hidden-import`：兜住 PyInstaller 静态分析看不到的动态导入
  （函数内 `__import__`、可选音频后端 `sounddevice` / `pyaudio`、第三方设备依赖）。
- `--exclude-module`：刻意排除**不该进 fleet 解释器**的东西——
  `torch`（属 polaris STT 子进程，架构铁律：fleet 解释器绝不 import torch）、
  `skynet`（外部 netforce 仓库，经 `NETFORCE_HOME` 指路，不 vendoring）、
  `listen` / `speak`（polaris 模块，跨进程）、`rules_relay`（可选广告跳过中继）。
- `--add-data ui:ui`：前端静态资源整目录打进冻扁包（`server.py` 从 `ROOT/ui` 读）。
- `--runtime-hook tools/fleet_runtime_hook.py`：冻扁后把只读的 MEIPASS 状态路径
  重定向到 `%APPDATA%/fleet`（见第 3 节）。

本地想试打（需 Windows + 已装依赖 + 显示器无关，因为打包本身不开窗）：
```powershell
pip install -r requirements.txt pyinstaller
python tools/build_gui.py
# 产物：dist/fleet-gui.exe
```

---

## 3. 冻扁后的可写路径（重要）

开发态下这些状态文件默认写到**仓库根**（可写）：
- `persona_state.json`（人格抽屉的「她现在怎么样」）
- `fleet_context.db`（记忆层，E-7：可寻址可删）
- `cards/`（角色卡库）
- `fleet_queue.json`（待办队列账本）

冻扁包解压到临时目录 `MEIPASS`，是**只读**的——直接往那儿写会当场崩。
`tools/fleet_runtime_hook.py` 在冻扁后把这些路径重定向到 `%APPDATA%/fleet`（可写），
且只在冻结态生效，绝不污染开发态。用户仍可用同名环境变量覆盖。

> 要在没打包的开发态里改默认位置，直接设环境变量即可：
> `FLEET_PERSONA` / `FLEET_CTX` / `FLEET_CARD_DIR` / `FLEET_QUEUE`。

---

## 4. 已知边界（不是 bug，是范围）

- **torch / polaris 不在冻扁包里**：STT/TTS 走独立的 polaris 解释器（跨进程），
  `fleet-gui.exe` 本身不含 torch。要启用语音能力，需另起一个装好 torch+polaris 的
  Python 跑 STT 子进程——这与「fleet 解释器绝不 import torch」的架构铁律一致。
  （GUI 语音界面 W4/W5 按计划当前暂缓，等麦克风硬件与跨仓点头。）
- **`--windowed` 无控制台**：发布态是纯窗口程序，stdout/stderr 不显示。
  排查时可用 `python tools/build_gui.py` 改 `--console` 重打，或先 `python fleet_gui.py --smoke`
  看后端是否起得来。
- **WebView2 运行时**：`pywebview` 在 Windows 上依赖系统 Edge WebView2 Runtime
  （Win10/11 自带）。若目标机没有，窗口起不来——这是运行时环境依赖，不在打包范围。

---

## 5. 冒烟自检

`fleet_gui.py --smoke` 起本地服务、带一次性 token 探 `/api/health`、打完即退，
不开窗、不依赖 webview、不需要显示器。CI 在打包前（源码）和打包后（exe 产物）各跑一次，
任一处起不来都会挡住构建。
