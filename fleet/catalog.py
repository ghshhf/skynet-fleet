"""能力清单：全仓唯一一处「有哪些能力」。

这份清单是唯一真相——终端补全、MCP 工具列表、她/模型能看见的东西，全部从这里
长出来。以前加一项能力要改三处（commander 的 elif、手写 help、accounts 的分权表），
漏一处就是「能力存在但没人知道」；现在只改这里。

命名一律 domain.verb（screen.read / input.tap / pc.state）。domain 是能力族，
verb 是动作；这样补全能按族分组，权限树也能按族授权。
"""
import os

from . import netrunner as NR
from .core import Capability, Param, Registry
from .kinds.android_phone import AndroidPhone
from .kinds.fleet_node import FleetNode, load_nodes
from .kinds.internet import Internet
from .kinds.lan_scanner import LanScanner
from .kinds.windows_pc import WindowsPC
from .transports import get as get_transport

DEVICES_PATH = os.environ.get("FLEET_DEVICES", os.path.join(
    os.path.expanduser("~"), ".fleet", "devices.json"))


def _bridge_reason(reg, dev):
    """红外发射走编队桥（不是 adb）——桥死了，能力就该如实下线。"""
    t = get_transport("bridge")
    ok, reason = t.available() if t else (False, "没有 bridge 通道")
    return ok, reason


def _netforce_reason(reg, dev):
    """联网层分两个供应商，各自缺各自的——缺哪个，只有那几个能力下线。"""
    if NR.netforce_cli():
        return True, ""
    return False, "没装 netforce：把 NETFORCE_HOME 指到它的仓库"


def _papers_reason(reg, dev):
    if NR.papers_cli():
        return True, ""
    return False, "没装论文爬虫：把 PAPERS_HOME 指到 train_15b"


CAPABILITIES = [
    # ---- 本机 ----
    Capability("pc.state", "本机状态（前台/在座/负载/磁盘）",
               params=(Param("clip", "bool", doc="要不要剪贴板内容（默认不给）"),)),
    Capability("pc.usage", "今天在电脑前做了什么",
               params=(Param("day", "str", doc="YYYY-MM-DD，默认今天"),)),
    Capability("pc.shell", "本机执行命令（白名单）",
               params=(Param("cmd", "str", True, "命令"),
                       Param("timeout", "int", doc="秒，默认 25")),
               side_effect=True),
    # ---- 手机：看 ----
    Capability("screen.read", "看手机屏幕（前台 + 可见文字）"),
    Capability("tree.read", "手机 UI 树",
               params=(Param("limit", "int", doc="最多几行，默认 40"),)),
    Capability("apps.list", "手机应用清单",
               params=(Param("third_only", "bool", doc="只要第三方，默认 true"),)),
    Capability("app.foreground", "手机前台应用"),
    Capability("notify.read", "手机通知（只读，不点开）",
               params=(Param("limit", "int", doc="默认 10"),)),
    Capability("state.read", "手机全身状态（电/网/声音/屏/传感器/存储）"),
    Capability("bt.scan", "手机蓝牙扫描",
               params=(Param("secs", "int", doc="扫几秒，默认 8"),)),
    # ---- 手机：动 ----
    Capability("app.launch", "启动手机应用（带前台核验）",
               params=(Param("target", "str", True, "包名/activity/action/URI"),
                       Param("expect", "str", doc="核验用的包名"),
                       Param("timeout", "int")),
               side_effect=True),
    Capability("input.tap", "点击（按文字或坐标）",
               params=(Param("text", "str", doc="按文字点"),
                       Param("x", "int"), Param("y", "int"),
                       Param("dry_run", "bool")),
               side_effect=True),
    Capability("input.type", "输入文字",
               params=(Param("text", "str", True),), side_effect=True),
    Capability("input.key", "按键（back/home/enter/power/音量…）",
               params=(Param("key", "str", True),), side_effect=True),
    Capability("ir.send", "红外发射（默认只算序列不发射）",
               params=(Param("protocol", "str", doc="nec/nec_ext/samsung/sony"),
                       Param("addr", "int"), Param("cmd", "int"),
                       Param("dry_run", "bool", doc="默认 true")),
               side_effect=True, check=_bridge_reason,
               note="经编队桥发射；第一次用必须显式 dry_run=false"),
    # ---- 这张网 ----
    Capability("net.scan", "局域网端口指纹扫描",
               params=(Param("quick", "bool", doc="跳过 NBT/mDNS"),
                       Param("timeout_ms", "int"))),
    Capability("net.nearby", "身边全景（IP + 无线 + 蓝牙三层）",
               params=(Param("no_lan", "bool"), Param("no_wifi", "bool"),
                       Param("no_bt", "bool"))),
    # ---- 编队节点（远程机器）----
    Capability("shell.exec", "在编队节点上执行命令（白名单）",
               params=(Param("node", "str"), Param("cmd", "str", True)),
               side_effect=True, note="D0 只登记：控制面无对外代发口，D1 补"),
    Capability("file.push", "推文件到编队节点",
               params=(Param("node", "str"), Param("path", "str", True)),
               side_effect=True, note="同上"),
    Capability("file.pull", "从编队节点取文件",
               params=(Param("node", "str"), Param("path", "str", True)),
               side_effect=True, note="同上"),
    # ---- 万维网（联网层）：外面的世界，经两个爬虫伸进去 ----
    Capability("web.status", "联网层现状（两个源在不在 / 各攒了多少）",
               note="只读：不联网也看得见本地已攒的"),
    Capability("web.discover", "资源站发现（netforce 无限流，必须限时）",
               params=(Param("minutes", "int", doc="跑几分钟就收，默认 3"),),
               side_effect=True, check=_netforce_reason,
               note="这活本身没有终点（原样跑到早上 6 点），所以由我们限时叫停"),
    Capability("web.corpus", "游戏 wiki 角色语料（netforce）",
               params=(Param("tag", "str", doc="站点代号，默认 blhx"),
                       Param("limit", "int", doc="最多几个角色，默认 10")),
               side_effect=True, check=_netforce_reason),
    Capability("web.diffuse", "站群扩散盘点（netforce，逐个判活）",
               params=(Param("limit", "int", doc="最多几个子站，默认 10"),),
               side_effect=True, check=_netforce_reason),
    Capability("web.papers", "前沿论文抓取（arXiv+HF+OpenAlex 中英）",
               params=(Param("days", "int", doc="补抓近几天，默认 1"),),
               side_effect=True, check=_papers_reason),
    Capability("web.frontier", "今天的论文清单（只读，不联网）",
               params=(Param("day", "str", doc="YYYY-MM-DD，默认今天"),),
               check=_papers_reason),
    Capability("web.sites", "站群活站清单（只读，不联网）",
               check=_netforce_reason,
               note="看的是本地已攒的那本账，跑 web.diffuse 才会涨"),
]


def build(path=None, gate=None, ttl=5.0, with_nodes=True):
    """组装注册表。设备按「先能验证的」顺序加：本机 → 手机 → 扫描器 → 编队节点 → 万维网。

    联网层排最后：前几台都要动真设备（adb / 端口 / 桥），联网是唯一一个
    「不在场也能登记」的，且它的探测最慢（要起子进程）。放最后是为了让
    「本机好不好使」这种第一手的判断先出结果，不被慢活拖住。
    """
    reg = Registry(path=path if path is not None else DEVICES_PATH, gate=gate, ttl=ttl)
    reg.register_all(CAPABILITIES)
    reg.add(WindowsPC())
    reg.add(AndroidPhone())
    reg.add(LanScanner())
    reg.add(Internet())
    if with_nodes:
        for name, rec in load_nodes().items():
            try:
                reg.add(FleetNode(name, rec))
            except ValueError:
                continue
    reg.load()          # 历史只用于「谁来过」，不造在线设备
    return reg
