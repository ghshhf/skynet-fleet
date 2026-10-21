"""打包 fleet-gui.exe（Windows 图形版）的 PyInstaller 配方。

⚠️ 本机不执行：没装 pyinstaller / pywebview / 显示器，且 red-line #7 要求
「零构建、断网可用」。真打在 CI（.github/workflows/build-gui.yml，windows-latest）
上完成。本脚本只是把 hiddenimports / datas / excludes / runtime_hooks 一次性钉死，
避免 build 命令散落各处、随人飘。

用法（在装好依赖的 Windows 上）：
    pip install -r requirements.txt pyinstaller
    python tools/build_gui.py
产物：dist/fleet-gui.exe（onefile，windowed）

为什么是这些开关：
  --paths REPO        让根下平铺模块（server.py 用 `import persona / persona_card /
                      memory` 等顶层导入）能被解析——它们不在 fleet 包里。
  --collect-submodules fleet   把整个 fleet 包（含动态子模块）收进来。
  --hidden-import     兜住 PyInstaller 看不到的动态导入：函数内的 __import__、
                      可选音频后端 sounddevice/pyaudio、第三方设备依赖。
  --exclude-module    刻意排除：torch（属 polaris STT 子进程，fleet 解释器绝不
                      import torch）、skynet（外部 netforce 仓库，经 NETFORCE_HOME
                      指路，不 vendoring）、listen/speak（polaris 模块）、
                      rules_relay（可选广告跳过中继）。
  --add-data ui:ui    前端静态资源整目录打进冻扁包（server.py 从 ROOT/ui 读）。
  --runtime-hook      冻扁后把只读的 MEIPASS 状态路径重定向到 %APPDATA%/fleet。
"""
import os
from PyInstaller.__main__ import run

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

APP = os.path.join(REPO, "fleet_gui.py")
RUNTIME_HOOK = os.path.join(HERE, "fleet_runtime_hook.py")
UI_DIR = os.path.join(REPO, "ui")

# 根下平铺模块 + 动态/条件导入（PyInstaller 静态分析看不到的，列这里做双保险）。
HIDDEN = [
    # 根下平铺模块（被 fleet 包内代码顶层 import）
    "persona", "persona_card", "memory",
    "accounts", "arbiter", "audit", "ble", "bootstrap", "capabilities",
    "card_store", "common", "compute_runner", "discovery", "enroll",
    "followups", "infrared", "journal", "llm", "nearby", "netpath",
    "node_agent", "orchestrator", "pc_state", "pc_usage", "perceive",
    "phone_ops", "phone_state", "policies", "queue_store", "recall",
    "registry", "ui_automation",
    # 可选音频后端（voice.py 用 __import__ 动态加载）
    "sounddevice", "pyaudio",
    # 设备/通道依赖的第三方
    "adb_shell", "adb_shell.adb_device", "adb_shell.adb_message",
    "adb_shell.auth.sign_pycryptodome", "adb_shell.auth.sign_cryptography",
    "adb_shell.auth.sign_pythonrsa", "adb_shell.auth.keygen",
    "cryptography", "pycryptodome",
    "PIL", "mss", "numpy", "psutil", "pyautogui", "rule_engine",
    "rich", "textual", "prompt_toolkit", "webview",
]

# 刻意排除（不在 fleet 解释器里出现：架构规定 / 外部仓库 / 可选模块）
EXCLUDES = ["torch", "skynet", "listen", "speak", "rules_relay"]

args = [
    "--onefile",
    "--clean",
    "--name", "fleet-gui",
    "--windowed",
    "--noconfirm",
    "--paths", REPO,
    "--runtime-hook", RUNTIME_HOOK,
]
for h in HIDDEN:
    args += ["--hidden-import", h]
for e in EXCLUDES:
    args += ["--exclude-module", e]
args += ["--collect-submodules", "fleet"]
# 前端静态资源：整个 ui/ 目录 -> 冻扁后的 ui/
args += ["--add-data", UI_DIR + os.pathsep + "ui"]
# 入口脚本
args += [APP]

if __name__ == "__main__":
    run(args)
