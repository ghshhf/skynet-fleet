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
    # ---- 对话语音（语音对话视图用；server.py 的 /api/voice/* 走它）----
    # 🔴 这里是**仓库根**的 voice.py，不是 fleet/gui/voice.py（命令式那套）。
    #    --paths REPO 已把根放进搜索路径，所以 `voice` 解析到根那个。
    "voice",
    "faster_whisper", "ctranslate2", "edge_tts",
    # 天网语音模式（--voice）：桌面端 + 语音链 + 嵌合件
    "desktop", "dialogue", "mock_brain", "tianwang_cards",
    # 刻意**不**列 kokoro：它 import torch，而 torch 在 EXCLUDES 里（架构铁律：
    # fleet 解释器绝不 import torch）。列进来只会让 PyInstaller 报一堆 torch 缺失。
    # 结果：冻扁包里 TTS 只剩 edge_tts（联网）一路，voice.say() 会自动回落。
    # 要离线 kokoro，得让语音走子进程（用系统 Python），不是把它塞进这个包。
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
# faster_whisper 带的是 tokenizer 资产、ctranslate2 带一串 dll/vc runtime，
# 光 --hidden-import 只收 .py，收不到这些东西 —— 冻扁后一 import 就炸。
# 用 collect-all 把 data/binaries 一起带走（2026-10-22 补）。
args += ["--collect-all", "faster_whisper"]
args += ["--collect-all", "ctranslate2"]
# edge_tts 是纯 py，没有资产，hidden-import 够了（但它得真装上，见 requirements.txt）
# 前端静态资源：整个 ui/ 目录 -> 冻扁后的 ui/
args += ["--add-data", UI_DIR + os.pathsep + "ui"]
# 角色卡源卡：整个 cards/ -> 冻扁后的 cards/（ccv2 json + _protocols/）
# 冻扁包里它只读；fleet_runtime_hook.py 会在首次启动时把卡播种到
# %APPDATA%/fleet/cards（可写），否则语音对话视图打开是空卡库、没有音色。
args += ["--add-data", os.path.join(REPO, "cards") + os.pathsep + "cards"]
# 入口脚本
args += [APP]

if __name__ == "__main__":
    run(args)
