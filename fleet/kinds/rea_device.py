# -*- coding: utf-8 -*-
"""REA 能力设备（实现层）：承载 re.* 能力（原 rea.*，来源 morluto/rea；已并入 re. 命名空间）。

2026-11-03 归并：原先 140 个引擎原子动作各自成能力（Frida 一个会话拆七项），
现在 catalog 只露 6 个领域能力（guidance / web / mobile / dynamic / native /
appgraph），子动作由 action 参数指定。ENGINE_OF 仍是「原子名 → 引擎」的内部路由。

两种调用都接：
  - 新：invoke("rea.adb" 形式的域名, {"action": "pull_adb_package", "args": {...}})
  - 旧：invoke("rea.pull_adb_package", {...})   ← 靠 DOMAIN_OF 自动归位
不给 action 时返回该域的子动作清单（省一次猜）。
已接 handler 的引擎真跑，未接的如实返回 unavailable。
"""
import os

from ..core import BaseDevice, DeviceResult as R

try:
    import sqlite3
    _HAS_SQLITE = True
except Exception:
    _HAS_SQLITE = False

from ..vendored_rea import DOMAIN_OF, DOMAINS, ENGINE_OF
from .rizin_eng import _h_rizin
from .rea_reverse_eng import _h_apktool, _h_binwalk, _h_tshark, _h_frida


class ReaDevice(BaseDevice):
    kind = "rea"
    via = "local"

    def __init__(self, name="rea:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "REA(morluto/rea) 逆向工程能力；6 个领域，子动作走 action；"
                    "各依赖底层引擎，未装如实下线")

    def probe(self):
        # 设备整体可用；单个能力靠各自的 check= 诚实判依赖
        return True, ""

    def caps(self):
        # 只露领域能力（6 个），不再把 140 个原子动作当能力
        return tuple(DOMAINS.keys())

    def invoke(self, cap, args=None, dev=None):
        # dev 由委派方（Reverse）传入，让结果统一署名 re:bench；不传就自用
        dev = dev or self
        args = dict(args or {})

        # 旧原子名 → 归位到域 + 预置 action（老门禁/老调用不用改）
        if cap not in DOMAINS:
            dom = DOMAIN_OF.get(cap)
            if dom is None:
                return R.unsupported(cap, "re 设备没有这个能力",
                                     device=dev.name, via=dev.via)
            args.setdefault("action", cap[4:])
            cap = dom

        action = (args.get("action") or "").strip()
        if not action:
            # 不指定子动作 = 问「这个域有哪些动作」，不猜、不默认跑第一个
            acts = list(DOMAINS[cap])
            return R.success(cap, dev.name, via=dev.via, data={
                "domain": cap, "actions": acts, "count": len(acts),
                "hint": "用 action 指定子动作，子动作参数放 args"})

        if action not in DOMAINS[cap]:
            return R.unsupported(
                cap, "%s 没有子动作 %s（该域有 %d 个，先不带 action 调一次拿清单）"
                     % (cap, action, len(DOMAINS[cap])),
                device=dev.name, via=dev.via)

        sub = "rea." + action
        engine = ENGINE_OF.get(sub)
        if engine is None:
            return R.unsupported(sub, "子动作没登记依赖引擎",
                                 device=dev.name, via=dev.via)

        h = _HANDLERS.get(engine)
        if h is None:
            return R.unavailable(
                cap,
                "子动作 %s 依赖引擎 %s，该引擎的 handler 尚未接入"
                "（装好引擎后接 CLI 即可实跑）" % (action, engine),
                device=dev.name, via=dev.via)

        # 子动作参数：顶层散键 + args 子字典（后者优先），action 本身不透传
        payload = {k: v for k, v in args.items() if k not in ("action", "args")}
        payload.update(args.get("args") or {})
        try:
            return h(dev, sub, payload)
        except Exception as e:
            return R.error(cap, "REA 执行失败：%s" % str(e)[:200],
                           device=dev.name, via=dev.via)


def _h_sqlite3(dev, cap, args):
    """用 Python 自带 sqlite3 模块读取本地快照，返回 schema 摘要。"""
    if not _HAS_SQLITE:
        return R.unavailable(cap, "Python sqlite3 模块不可用",
                             device=dev.name, via=dev.via)
    path = (args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return R.error(cap, "缺参数 path（本地 .sqlite 快照路径）",
                       device=dev.name, via=dev.via)
    try:
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view')")
        objs = cur.fetchall()
        schema = []
        for name, typ in objs:
            cur.execute("PRAGMA table_info(%s)" % name)
            cols = [r[1] for r in cur.fetchall()]
            try:
                cur.execute("SELECT COUNT(*) FROM %s" % name)
                cnt = cur.fetchone()[0]
            except Exception:
                cnt = None
            schema.append({"name": name, "type": typ,
                           "columns": cols, "rows": cnt})
        con.close()
        return R.success(cap, dev.name, via=dev.via,
                         data={"path": path, "tables": schema,
                               "table_count": len(schema)})
    except Exception as e:
        return R.error(cap, "sqlite 读取失败：%s" % str(e)[:200],
                       device=dev.name, via=dev.via)


# 已接入真 handler 的引擎。随底层引擎安装，逐步在此补充。
_HANDLERS = {
    "sqlite3": _h_sqlite3,
    # rizin 本体（0.9.1）已装：rizin / ghidra_rizin / ghidra_or_rizin / rizin_cutter
    # 四个引擎全部由 rizin CLI 真跑（Ghidra 是它们的 OR 备选，未装则如实降级）。
    "rizin": _h_rizin,
    "ghidra_rizin": _h_rizin,
    "ghidra_or_rizin": _h_rizin,
    "rizin_cutter": _h_rizin,
    # 轻量逆向引擎（落 D 盘）：apktool(+jadx 反编译) / binwalk / tshark / frida
    "apktool": _h_apktool,
    "adb_apktool": _h_apktool,
    "apktool_keytool": _h_apktool,
    "binwalk": _h_binwalk,
    "tshark": _h_tshark,
    "frida": _h_frida,
}
