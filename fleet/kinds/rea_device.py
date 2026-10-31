# -*- coding: utf-8 -*-
"""REA 能力设备：承载 rea.* 能力（来源 morluto/rea，方案 A 全量声明 + 诚实依赖检查）。

每个 rea.* 能力已在 fleet/vendored_rea.py 里带 check= 探测对应底层引擎
（Ghidra/JADX/rizin/adb/frida...），不在场就如实下线，不谎报可用。
本设备的 invoke 按 ENGINE_OF 路由到引擎 handler；已接入 handler 的引擎真跑，
未接入的如实返回 unavailable（待装好引擎后补齐 CLI 调用即可）。
"""
import os

from ..core import BaseDevice, DeviceResult as R

try:
    import sqlite3
    _HAS_SQLITE = True
except Exception:
    _HAS_SQLITE = False

from ..vendored_rea import ENGINE_OF
from .rizin_eng import _h_rizin
from .rea_reverse_eng import _h_apktool, _h_binwalk, _h_tshark, _h_frida


class ReaDevice(BaseDevice):
    kind = "rea"
    via = "local"

    def __init__(self, name="rea:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "REA(morluto/rea) 逆向工程能力；各依赖底层引擎，未装如实下线")

    def probe(self):
        # 设备整体可用；单个能力靠各自的 check= 诚实判依赖
        return True, ""

    def caps(self):
        return tuple(ENGINE_OF.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        engine = ENGINE_OF.get(cap)
        if engine is None:
            return R.unsupported(cap, "rea 设备没有这个能力",
                                 device=self.name, via=self.via)
        h = _HANDLERS.get(engine)
        if h is None:
            return R.unavailable(
                cap,
                "REA 能力已声明；引擎 %s 的 handler 待接入"
                "（装好引擎后接 CLI 即可实跑）" % engine,
                device=self.name, via=self.via)
        try:
            return h(self, cap, args)
        except Exception as e:
            return R.error(cap, "REA 执行失败：%s" % str(e)[:200],
                           device=self.name, via=self.via)


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
