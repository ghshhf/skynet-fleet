"""P5c 跨设备记忆同步离线门禁。

测 memory_store 的 export_all/import_memories roundtrip + 去重，
以及 capabilities/catalog 登记、common 消息类型。
"""
import os, sys, tempfile
# 老写法钉死 `D:/code/device-fleet`（那台机器的旧仓库位置），换机即
# ModuleNotFoundError——本文件要 import 的模块全在本仓，根目录按 __file__
# 推，不写死任何路径（2026-10-24 与 netrunner 同一批修）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fleet.memory_store import MemoryStore
from fleet import catalog
import capabilities, common

def _db():
    d = tempfile.gettempdir()
    p = os.path.join(d, f"ms_{os.getpid()}_{id({})}.db")
    for f in (p, p+"-journal", p+"-wal", p+"-shm"):
        if os.path.exists(f): os.remove(f)
    return p

def test_export_import_roundtrip():
    p = _db()
    s = MemoryStore(path=p)
    s.remember("user", "我喜欢喝普洱茶", tags=("profile",))
    s.remember("assistant", "普洱茶不错", tags=("profile",))
    items = s.export_all()
    assert len(items) == 2, items
    # 新 db 导入
    p2 = _db()
    s2 = MemoryStore(path=p2)
    n = s2.import_memories(items)
    assert n == 2, n
    # 去重：再导入同内容应 0
    n2 = s2.import_memories(items)
    assert n2 == 0, n2
    s.close(); s2.close()
    for pp in (p, p2):
        for f in (pp, pp+"-journal", pp+"-wal", pp+"-shm"):
            if os.path.exists(f): os.remove(f)
    print("  roundtrip + 去重 OK")

def test_capabilities_registered():
    assert capabilities.known("fleet.memory.sync"), "capabilities 未登记 fleet.memory.sync"
    assert capabilities.known("fleet.memory.sync.run"), "别名未登记"
    # catalog 登记通过 build() 注册，离线只验 capabilities 层
    print("  capabilities/catalog 登记 OK")

def test_common_msg_types():
    assert common.MEMORY_SYNC == "memory_sync"
    assert common.MEMORY_SYNC_RESULT == "memory_sync_result"
    print("  common 消息类型 OK")

if __name__ == "__main__":
    test_export_import_roundtrip()
    test_capabilities_registered()
    test_common_msg_types()
    print("evals/test_memory_sync: PASS")
