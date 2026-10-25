"""依赖声明表 —— doctor 扫环境、出「该装什么」报告的单一事实源。

为什么独立成表（不塞进 Capability）：
  Capability 是 frozen dataclass，往里加 deps 字段要动 149 处构造 + 破坏
  __post_init__。依赖声明是「环境层面」的事，和能力本身的语义正交，放外部表
  （没声明 = 无第三方依赖）零侵入，也方便 GUI / 手机端直接复用同一张表。

Dep.kind:
  pip  → importlib.find_spec(import_name) 探测；可自动 pip install（「点一下」）
  exe  → shutil.which(exe) 探测；只给 fix 命令，绝不自动下二进制
  repo → 环境变量(env)指向的目录存在；给 fix（设环境变量 / 克隆仓库）
  sys  → doctor 不探测（麦克风/摄像头/显示），留给各自 probe

hard: 缺失是否导致能力完全不可用。False = 降级可用(warn)，True = 不可用(fail)。

android_fix: 手机端等价安装命令（pkg install ...），跨端复用同一张表。
"""

from dataclasses import dataclass, field


@dataclass
class Dep:
    name: str
    kind: str = "pip"
    import_name: str = ""
    pip_pkg: str = ""
    exe: str = ""
    env: str = ""
    fix: str = ""
    android_fix: str = ""
    hard: bool = True
    why: str = ""

    @property
    def pip_install(self):
        if self.kind != "pip":
            return ""
        return "pip install " + (self.pip_pkg or self.import_name or self.name)

    def as_dict(self):
        return {
            "name": self.name, "kind": self.kind,
            "import_name": self.import_name, "pip_pkg": self.pip_pkg,
            "exe": self.exe, "env": self.env, "fix": self.fix,
            "android_fix": self.android_fix, "hard": self.hard,
            "why": self.why, "pip_install": self.pip_install,
        }


def D(name, kind="pip", **kw):
    return Dep(name, kind, **kw)


# —— 复用块 ——
_NETFORCE = D("netforce 仓库", kind="repo", env="NETFORCE_HOME",
              fix="把 NETFORCE_HOME 环境变量指向 netforce 仓库根目录（不克隆就指过去）",
              why="联网层主数据源（web.* 多数能力）")
_PAPERS = D("论文爬虫仓库 train_15b", kind="repo", env="PAPERS_HOME",
            fix="把 PAPERS_HOME 环境变量指向 train_15b 仓库根目录",
            why="前沿论文抓取 web.papers")
_ADB = D("adb (Android platform-tools)", kind="exe", exe="adb",
         fix="下载 Android platform-tools 并加入 PATH"
             "（https://developer.android.com/tools/release-notes-sdk-platform-tools）",
         android_fix="pkg install android-tools", why="无线 ADB 操控手机")
_BETTERCAP = D("bettercap", kind="exe", exe="bettercap",
               fix="PC: go install github.com/bettercap/bettercap@latest；"
                   "手机 Termux: pkg install bettercap",
               android_fix="pkg install bettercap", why="无线侦察 / 流量嗅探")
_FFMPEG = D("ffmpeg", kind="exe", exe="ffmpeg",
            fix="装 ffmpeg 并加入 PATH（https://ffmpeg.org/download.html）",
            why="视频渲染")
_RENDER_ENGINE = D("渲染引擎 RuiC-motion-reel", kind="repo", env="RUIKIT_HOME",
                   fix="git clone RuiC-motion-reel 到本地，并把 RUIKIT_HOME 指向它",
                   why="render.* 动效渲染")

# —— 逐能力 / 逐族声明（key 含 '*' 视为前缀匹配）——
CAP_DEPS = {
    # host 本机操控
    "host.monitor": [D("psutil", import_name="psutil", why="CPU/内存/磁盘/进程监控")],
    "host.apps": [D("psutil", import_name="psutil", why="进程列表")],
    "host.screen": [D("mss", import_name="mss", why="屏幕截图"),
                    D("Pillow", import_name="PIL", pip_pkg="Pillow", why="截图盲文预览")],
    "host.input.*": [D("pyautogui", import_name="pyautogui", why="模拟鼠标键盘")],

    # 网络侦察
    "net.scan": [D("scapy", import_name="scapy", why="LAN 端口指纹扫描",
                   android_fix="pkg install scapy")],
    "net.nearby": [D("scapy", import_name="scapy", why="身边全景（IP+无线+蓝牙）")],
    "net.dissect": [D("scapy", import_name="scapy", why="包解析"), _BETTERCAP],
    "net.survey": [_BETTERCAP],
    "net.wallprobe": [_BETTERCAP],

    # OSINT
    "osint.crt": [D("requests", import_name="requests", why="crt.sh 查询")],
    "osint.dns": [D("requests", import_name="requests", why="公共 DNS 解析")],
    "osint.rdap": [D("requests", import_name="requests", why="RDAP 注册信息")],

    # 联网层（仓库类）
    "web.papers": [_NETFORCE, _PAPERS],
    "web.corpus": [_NETFORCE],
    "web.discover": [_NETFORCE],
    "web.diffuse": [_NETFORCE],
    "web.status": [_NETFORCE],

    # 无线 ADB
    "adb.wifi.*": [_ADB],

    # 渲染
    "render.*": [_RENDER_ENGINE, _FFMPEG],
}

# 项目级依赖（不挂具体能力，doctor 单独列出）
PROJECT_DEPS = [
    D("rich", import_name="rich", why="门禁/日志美化（run_selftests）"),
    D("pyyaml", import_name="yaml", pip_pkg="pyyaml", why="skills 技能系统"),
]

# GUI 自身依赖（非能力，组件级）
GUI_DEPS = [
    D("pywebview", import_name="webview", pip_pkg="pywebview",
      why="Windows 图形窗口（gui.app）"),
    D("sounddevice", import_name="sounddevice",
      why="语音模式录音（gui.voice）", hard=False),
    D("pyaudio", import_name="pyaudio",
      why="语音模式录音备选（gui.voice）", hard=False),
]
