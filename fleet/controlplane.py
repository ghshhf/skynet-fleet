"""控制面（commander/enroll）的 fleet 侧唯一入口。

为什么要有这个文件：仓库根目录的 commander.py / enroll.py 是跨机编队的**真实
控制面**（PSK 注册、心跳看门狗、离线命令队列、节点 socket）。它们历史比 fleet
包早，且 2700+ 行，不可能一轮迁完。但 fleet 包不能直接 `import commander`——
那是两层架构漂移的根源（hiddenimports 里 40+ 老模块、运行时 import 炸）。

本文件是** owned API**：fleet 包只认这个文件，不认 commander.py。commander.py
的函数通过运行时 import 借过来（和现在 commander.py `from fleet import jobs` 的
方向一致，只是反向），一旦 commander 改签名，只改这一处。

D1 目标：把 send_to_node / 节点注册 / 看门狗真正迁进 fleet 包，commander.py
退化为 CLI 入口 shim。在此之前，本文件是「过渡胶水」，不是最终形态。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 运行时借 commander 的 API——commander.py 本身也在 sys.path 里（根目录）。
try:
    import commander as _CMD
except Exception as e:  # pragma: no cover - 无控制面时
    _CMD = None
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None


def available():
    """控制面（commander）是否可导入。"""
    return _CMD is not None


def send_to_node(name, msg, timeout=30):
    if _CMD is None:
        return {"ok": False, "err": f"控制面不可用：{_IMPORT_ERR}"}
    return _CMD.send_to_node(name, msg, timeout=timeout)


def registry_nodes():
    """返回当前在线节点名列表（只读快照）。"""
    if _CMD is None:
        return []
    try:
        reg = getattr(_CMD, "REGISTRY", None)
        if reg is None:
            return []
        snap = reg.with_lock(lambda d: dict(d)) if hasattr(reg, "with_lock") else {}
        return list(snap.keys()) if isinstance(snap, dict) else []
    except Exception:
        return []


def import_error():
    return _IMPORT_ERR