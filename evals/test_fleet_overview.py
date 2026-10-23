# -*- coding: utf-8 -*-
"""编队总览门禁：算错了比没做更糟。

这里只钉**口径**，不钉像素：
  · 失败率的分母是 done + failed，取消不算失败
  · 没跑过作业 = None（未知），不是 0%
  · 离线设备要排在最前（出问题的先看见）
  · 手机读不到就说读不到，不给假空
  · 网关缓存不存在 = None，不是 0
  · 租约过期还在 running 必须报出来，不然就是僵尸活

跑法：python evals/test_fleet_overview.py
"""
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp()
os.environ["FLEET_JOBS"] = os.path.join(TMP, "jobs.json")
os.environ["FLEET_ARTIFACTS"] = os.path.join(TMP, "arts")
GW = os.path.join(TMP, "gateway_health.json")
os.environ["FLEET_GW_HEALTH"] = GW

from fleet.gui.server import _Handler          # noqa: E402
from fleet import jobs as J                    # noqa: E402
from fleet import imgscale                     # noqa: E402

FAILS = []


def check(cond, why):
    print(("  [ok]   " if cond else "  [FAIL] ") + why)
    if not cond:
        FAILS.append(why)


class FakeReg:
    """假注册表：availability 给固定清单，call 给固定数据或固定失败。"""

    def __init__(self, rows, state=None, call_ok=True, call_err="取不到"):
        self._rows = rows
        self._state = state or {}
        self.call_ok = call_ok
        self.call_err = call_err

    def availability(self):
        return [dict(r) for r in self._rows]

    def call(self, cap, args=None, gate=None):
        r = types.SimpleNamespace()
        r.ok = self.call_ok
        r.err = "" if self.call_ok else self.call_err
        r.data = dict(self._state) if self.call_ok else {}
        return r


def overview(rows=None, **kw):
    reg = FakeReg(rows if rows is not None else [], **kw)
    shim = types.SimpleNamespace(fleet=types.SimpleNamespace(registry=reg, gate=None),
                                 _gateway_digest=_Handler._gateway_digest)
    return _Handler._fleet(shim)


def d_row(name, kind="host", ok=True, reason="", caps=3):
    return {"name": name, "kind": kind, "via": "local", "ok": ok,
            "reason": reason, "caps": ["c%d" % i for i in range(caps)]}


print("=== 1) 没有任何作业时，成功率是「未知」不是 0% ===")
o = overview([d_row("host:local")])
check(o["jobs"]["success_rate"] is None,
      "没跑过作业 → success_rate is None（0% 会被读成「全挂了」）")
check(o["jobs"]["total"] == 0, "作业总数 0")

print("=== 2) 成功率分母 = done + failed，取消不算失败 ===")
js = J.get_store()
ja = js.submit("image.gen", {"prompt": "a"})
jb = js.submit("image.gen", {"prompt": "b"})
jc = js.submit("image.gen", {"prompt": "c"})
# claim(node) 是「节点来领活」，一次领一个 —— 不是按 id 领
for _ in range(3):
    js.claim("phone1")
js.finish(ja["id"], True, data={"ok": 1})
js.finish(jb["id"], False, err="显存不够", retry=False)
ok_c, _ = js.cancel(jc["id"], reason="主人不要了")
check(ok_c, "第三个成功取消")

o = overview([d_row("host:local")])
j = o["jobs"]
check((j["done"], j["failed"], j["cancelled"]) == (1, 1, 1),
      "计数 1 done / 1 failed / 1 cancelled: %s" % [j["done"], j["failed"], j["cancelled"]])
check(j["success_rate"] == 0.5,
      "成功率 0.5（1/(1+1)）；把 cancelled 算进去就会变成 0.33 → %r" % j["success_rate"])
check(j["total"] == 3, "总数 3")
check(len(j["recent"]) == 3, "最近作业列了 3 条")
check(any(x["state"] == "failed" and "显存" in x["err"] for x in j["recent"]),
      "失败原因带出来了（不是只给个状态）")

print("=== 3) 租约过期的 running 必须报出来 ===")
jd = js.submit("video.gen", {"prompt": "x"})
js.claim("phone1")
# 用「续租 0.05 秒后不再续」来模拟节点掉线 —— 比直接改内部字段更贴近真实：
# 心跳过期就是「它再也没回来」，reap 该认出这个状态。
js.heartbeat(jd["id"], lease=0.05)
time.sleep(0.3)
o = overview([d_row("host:local")])
check(o["jobs"]["stale"] >= 1, "stale 计数 ≥1：%d" % o["jobs"]["stale"])
check(any("租约过期" in n for n in o["notes"]),
      "notes 里明说要 reap：%s" % o["notes"][:1])
check(any(x["stale"] for x in o["jobs"]["recent"]), "该条作业自己也标了 stale")
js.cancel(jd["id"], reason="测试收尾")

print("=== 4) 离线设备排最前，且带原因 ===")
rows = [d_row("host:local"),
        d_row("phone:main", kind="android", ok=False, reason="不在线：adb 拒绝"),
        d_row("ai:free", kind="ai")]
o = overview(rows)
check(o["devices"]["rows"][0]["name"] == "phone:main",
      "离线那台排第一（不是沉在列表底）")
check(o["devices"]["online"] == 2 and o["devices"]["offline"] == 1,
      "2 在线 / 1 离线")
check(o["devices"]["rows"][0]["reason"].startswith("不在线"),
      "离线带原因，不是空字符串")
check(o["caps"]["online"] == 6, "在线能力合计 6（不含离线那台的 3 项）")

print("=== 5) 手机读不到就说读不到，不给假空 ===")
o = overview([d_row("phone:main", kind="android", ok=True)], call_ok=False,
             call_err="adb 没响应")
check(o["phone"]["available"] is False, "available=False")
check("adb 没响应" in (o["phone"].get("note") or ""),
      "note 里带真原因：%r" % o["phone"].get("note"))
check(o["phone"]["brief"] == "", "没有 brief 字段的空值造假")

print("=== 6) 手机不在线 vs 编队里没有手机 ===")
o = overview([d_row("phone:main", kind="android", ok=False, reason="掉线了")])
check(o["phone"]["available"] is False and "掉线了" in (o["phone"].get("note") or ""),
      "手机离线 → 说原因")
o = overview([d_row("host:local")])
check("没有手机" in (o["phone"].get("note") or ""),
      "压根没有手机类设备 → 说法不同：%r" % o["phone"].get("note"))

print("=== 7) 网关：没缓存就是 None，不是 0 ===")
if os.path.exists(GW):
    os.remove(GW)
o = overview([d_row("host:local")])
check(o["gateways"] is None, "没探过 → gateways=None（0/0 会被读成「全挂」）")

print("=== 8) 网关：缓存对了就统计对，且区分「等额度刷新」 ===")
now = time.time()
data = {"version": 1, "gateways": {
    "zen": {"probed_at": now - 60, "models": {
        "m/fast": {"ok": True, "sec": 0.9, "ts": now, "ttl": 21600, "kind": "ok"},
        "m/slow": {"ok": True, "sec": 8.9, "ts": now, "ttl": 21600, "kind": "ok"},
        "m/quota": {"ok": False, "note": "HTTP429", "ts": now - 100,
                    "ttl": 600, "kind": "quota"},
        "m/gone": {"ok": False, "note": "HTTP404", "ts": now - 100,
                   "ttl": 21600, "kind": "gone"},
    }},
}}
with open(GW, "w", encoding="utf-8") as f:
    json.dump(data, f)
o = overview([d_row("host:local")])
g = (o["gateways"] or {}).get("zen") or {}
check(g.get("total") == 4 and g.get("live") == 2, "4 条里活 2 条：%s" % g)
check(g.get("quota_waiting") == 1, "429 那条算「等额度刷新」而不是「不可用」")
check(g.get("next_reprobe_in") is not None and g["next_reprobe_in"] <= 600,
      "给了「最快多少秒后自动重探」：%r" % g.get("next_reprobe_in"))
check(len(g.get("live_models") or []) == 2, "live_models 带出来了")

print("=== 9) 总览是只读聚合：调的是只读能力 ===")
# state.read 是只读能力；如果哪天有人改成调 input.tap，这条会拦不住，
# 但至少保证「手机快照走的是 state.read 这条已知只读的路」。
calls = []


class WatchReg(FakeReg):
    def call(self, cap, args=None, gate=None):
        calls.append(cap)
        return FakeReg.call(self, cap, args, gate)


reg = WatchReg([d_row("phone:main", kind="android")], state={"brief": "电 55%"})
shim = types.SimpleNamespace(fleet=types.SimpleNamespace(registry=reg, gate=None),
                             _gateway_digest=_Handler._gateway_digest)
o = _Handler._fleet(shim)
check(calls == ["state.read"], "只调了 state.read：%s" % calls)
check(o["phone"]["brief"] == "电 55%", "brief 原样透传")

print("=== 10) 缩略器：手机截图 4MB 才需要它，它自己得靠得住 ===")
imgscale._selftest()

print()
if FAILS:
    print("test_fleet_overview: FAIL（%d 项）" % len(FAILS))
    for f in FAILS:
        print("   ✗ " + f)
    sys.exit(1)
print("test_fleet_overview: PASS")
