"""门禁 t9：审批弹窗（W2）——真审批、真挂起、真一次性。

W1 的真实状态是：/api/call 因为**没配闸门**，一切有副作用的能力一律
denied（带原因）。这是故意的 fail-closed，不是 bug。W2 加的是：需要审批的
能力会挂起等主人在弹窗里点确认，点确认才执行。

锁死的形状（每一条都是真会出事的）：

  ① **没确认就不执行**。
     审批中枢挂起这次调用；不点确认，设备那边**一次都没被碰过**。

  ② **点确认才执行，而且只执行一次**。
     同一个 request_id 第二次回应 → 拒绝（「已经结束了，不能重复回应」）。
     授权是一次性的，用掉即失效——没有可以复制出去重放的凭据。

  ③ **超时 = 拒绝，且理由说清「审批超时未确认」**。
     等不到人就是没人授权（fail-closed）。请求不许永久挂死。

  ④ **并发不互堵**。
     两个能力同时在等审批时，回应其中一个，另一个照样能等到自己的回应；
     谁也不许把另一个的等待吃掉（各自独立的 Event）。

  ⑤ **鉴权**：不带 token → 401；带错 token → 403。
     两种都带明确原因（照 secrets.compare_digest 的常数时间口径）。

  ⑥ **审批记录不能跨会话重放**。
     别的会话拿着同一个 request_id 来回应 → 拒绝。

  ⑦ **判定权在注册表层**（静态断言）。
     「要不要审批、几级、要不要双确认」全在 fleet/core/registry.py 与
     fleet/gui/approval.py 里；UI 层不许自己判。前端绕不过去。

全离线、可复现：**绝不真连设备、绝不真开麦、绝不真调 LLM**。
设备是假设备，AI 后端是假后端，网络 I/O 一律假掉。
门禁红 = 代码坏了。

跑法：D:/tools/python/python.exe evals/t9_approval.py
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.core import Capability, DeviceResult as R, Param, Registry  # noqa: E402
from fleet.gui.approval import ApprovalBroker                             # noqa: E402
from fleet.gui import server as GS                                       # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)
    return bool(cond)


# ------------------------------------------------------------------ 假设备

_ACTED = []


from fleet.core.device import BaseDevice  # noqa: E402


class FakeActDev(BaseDevice):
    """假动作设备：只往 _ACTED 里记一笔，绝不碰真屏幕/真鼠标。"""

    kind = "fakeact"

    def probe(self):
        return True, "假动作设备"

    def caps(self):
        return ("act.click", "act.burn", "act.read")

    def invoke(self, cap, args=None):
        _ACTED.append((cap, dict(args or {})))
        if cap == "act.read":
            return R.success(cap, self.name, data={"read": True})
        return R.success(cap, self.name, side_effect=True, data={"acted": cap})


def make_registry():
    reg = Registry(path="", ttl=0)
    reg.register(Capability("act.read", "读一下", params=(Param("x", "int"),)))
    reg.register(Capability("act.click", "点一下",
                            params=(Param("x", "int"),), side_effect=True))
    reg.register(Capability("act.burn", "烧掉", side_effect=True, destructive=True))
    reg.add(FakeActDev("act:local"))
    return reg


# ------------------------------------------------------------------ ① 没确认不执行

reg = make_registry()
broker = ApprovalBroker(timeout=5.0)
_ACTED.clear()

res = {}
t = threading.Thread(target=lambda: res.update(
    r=reg.call("act.click", device="act:local", args={"x": 1},
               gate=broker.gate_for(tag="t1"))))
t.daemon = True
t.start()

# 等它挂起：pending 里出现一条
deadline = time.time() + 3
while not broker.pending() and time.time() < deadline:
    time.sleep(0.02)
pend = broker.pending()
check(len(pend) == 1, "有副作用的能力会挂起等审批（pending 里有 1 条）")
check(_ACTED == [], "还没点确认时，设备一次都没被碰过")
if pend:
    p0 = pend[0]
    check(p0.get("cap") == "act.click", "待审批条目里有能力名（%s）" % p0.get("cap"))
    check(bool(p0.get("request_id")), "待审批条目有唯一 request_id")
    check(p0.get("level") == 1, "副作用能力是 1 级审批（实际 %s）" % p0.get("level"))

    # ① 拒绝 → denied 且不执行
    ok, why = broker.respond(p0["request_id"], False, reason="主人说先别动")
    check(ok, "拒绝信号能送达（%s）" % why)
    t.join(timeout=5)
    r = res.get("r")
    check(r is not None and r.code == R.DENIED, "拒绝后这次调用是 denied")
    check(r is not None and "先别动" in (r.err or ""), "拒绝理由原样带回（%s）"
          % (r.err if r else ""))
    check(_ACTED == [], "拒绝后设备仍然没被碰过")

    # ② 同一个 request_id 再回应一次 → 必须被拒（一次性）
    ok2, why2 = broker.respond(p0["request_id"], True)
    check(not ok2, "同一个 request_id 第二次回应被拒（一次性授权）")
    check("结束" in why2 or "不认识" in why2, "重复回应的拒绝理由说清了（%s）" % why2)

# ------------------------------------------------------------------ ② 确认后执行

broker2 = ApprovalBroker(timeout=5.0)
_ACTED.clear()
res2 = {}
t2 = threading.Thread(target=lambda: res2.update(
    r=reg.call("act.click", device="act:local", args={"x": 7},
               gate=broker2.gate_for(tag="t2"))))
t2.daemon = True
t2.start()
deadline = time.time() + 3
while not broker2.pending() and time.time() < deadline:
    time.sleep(0.02)
p2 = broker2.pending()
check(len(p2) == 1, "第二次调用也挂起了")
if p2:
    ok, why = broker2.respond(p2[0]["request_id"], True)
    check(ok, "确认信号能送达（%s）" % why)
    t2.join(timeout=5)
    r2 = res2.get("r")
    check(r2 is not None and r2.ok, "确认后这次调用成功（%s）"
          % (r2.err if r2 else "没返回"))
    check(len(_ACTED) == 1 and _ACTED[0][0] == "act.click",
          "确认后设备才被调用了一次（%s）" % (_ACTED,))
    # 一次性：再回应一次必须被拒
    ok3, why3 = broker2.respond(p2[0]["request_id"], True)
    check(not ok3, "确认过的 request_id 不能被再次使用（不可重放）")

# ------------------------------------------------------------------ ③ 破坏性双确认

broker3 = ApprovalBroker(timeout=5.0)
_ACTED.clear()
res3 = {}
t3 = threading.Thread(target=lambda: res3.update(
    r=reg.call("act.burn", device="act:local", args={},
               gate=broker3.gate_for(tag="t3"))))
t3.daemon = True
t3.start()
deadline = time.time() + 3
while not broker3.pending() and time.time() < deadline:
    time.sleep(0.02)
p3 = broker3.pending()
check(len(p3) == 1 and p3[0].get("destructive") is True,
      "破坏性能力标了 destructive=%s" % (p3[0].get("destructive") if p3 else None))
check(p3 and p3[0].get("level") == 2, "破坏性能力是 2 级审批（实际 %s）"
      % (p3[0].get("level") if p3 else None))
if p3:
    ok, why = broker3.respond(p3[0]["request_id"], True)
    check(not ok, "破坏性能力不给二次确认（double=true）→ 拒绝放行")
    check("二次确认" in why, "双确认的拒绝理由说清了（%s）" % why)
    ok, why = broker3.respond(p3[0]["request_id"], True, double=True)
    check(ok, "给了 double=true 才放行（%s）" % why)
    t3.join(timeout=5)
    check(_ACTED and _ACTED[0][0] == "act.burn", "双确认后才执行破坏性动作")

# ------------------------------------------------------------------ ④ 超时 = 拒绝

broker4 = ApprovalBroker(timeout=0.6)
_ACTED.clear()
t0 = time.time()
r4 = reg.call("act.click", device="act:local", args={"x": 3},
              gate=broker4.gate_for(tag="t4"))
dt = time.time() - t0
check(r4.code == R.DENIED, "没人点确认 → 超时后 denied")
check("审批超时未确认" in (r4.err or ""),
      "超时理由里写清「审批超时未确认」（%s）" % r4.err)
check(_ACTED == [], "超时后设备没被碰过")
check(dt < 5.0, "超时是按配的秒数来的（等了 %.2fs）" % dt)

# 超时之后再回应 → 必须被拒（这条已经结束了）
ok, why = broker4.respond("不存在的编号", True)
check(not ok, "回应不存在的编号被拒（%s）" % why)

# ------------------------------------------------------------------ ⑤ 并发不互堵

broker5 = ApprovalBroker(timeout=8.0)
_ACTED.clear()
res5 = {}
threads = []
for i in (1, 2):
    th = threading.Thread(target=lambda i=i: res5.update(
        {i: reg.call("act.click", device="act:local", args={"x": i},
                     gate=broker5.gate_for(tag="c%d" % i))}))
    th.daemon = True
    th.start()
    threads.append(th)

deadline = time.time() + 3
while len(broker5.pending()) < 2 and time.time() < deadline:
    time.sleep(0.02)
p5 = broker5.pending()
check(len(p5) == 2, "两个请求同时在等审批（pending %d 条）" % len(p5))
if len(p5) == 2:
    rids = {r["request_id"] for r in p5}
    check(len(rids) == 2, "两个待审批的 request_id 互不相同")
    # 先回应其中一个，另一个不许被带走
    ok, why = broker5.respond(p5[0]["request_id"], True)
    check(ok, "并发时能回应其中一个（%s）" % why)
    time.sleep(0.3)
    left = broker5.pending()
    check(len(left) == 1 and left[0]["request_id"] != p5[0]["request_id"],
          "回应一个不影响另一个（另一个还在等）")
    ok, why = broker5.respond(left[0]["request_id"], True)
    check(ok, "另一个也能被独立回应（%s）" % why)
    for th in threads:
        th.join(timeout=5)
    check(len(_ACTED) == 2, "两个都确认后各执行一次（%s）" % (_ACTED,))

# ------------------------------------------------------------------ ⑥ 跨会话重放

broker6 = ApprovalBroker(timeout=5.0, session="会话甲")
_ACTED.clear()
res6 = {}
t6 = threading.Thread(target=lambda: res6.update(
    r=reg.call("act.click", device="act:local", args={"x": 9},
               gate=broker6.gate_for(tag="s1", session="会话甲"))))
t6.daemon = True
t6.start()
deadline = time.time() + 3
while not broker6.pending() and time.time() < deadline:
    time.sleep(0.02)
p6 = broker6.pending()
if p6:
    ok, why = broker6.respond(p6[0]["request_id"], True, session="会话乙")
    check(not ok, "别的会话拿同一个 request_id 来回应 → 被拒")
    check("会话" in why, "跨会话的拒绝理由说清了（%s）" % why)
    check(len(broker6.pending()) == 1, "被拒的回应没有把这次审批吃掉")
    ok, why = broker6.respond(p6[0]["request_id"], True, session="会话甲")
    check(ok, "本会话回应正常放行（%s）" % why)
    t6.join(timeout=5)
    check(len(_ACTED) == 1, "本会话确认后才执行")

# ------------------------------------------------------------------ ⑦ HTTP 层

def http_json(url, token=None, method="GET", body=None, timeout=10):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Fleet-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, _try_json(raw), ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _try_json(raw), ""
    except Exception as e:
        return 0, None, "%s: %s" % (type(e).__name__, e)


def _try_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


_tmp = tempfile.mkdtemp(prefix="t9_approval_")
_cards_dir = os.path.join(_tmp, "cards")
os.makedirs(_cards_dir, exist_ok=True)
_persona = GS.Persona(card_dir=_cards_dir,
                      state_path=os.path.join(_tmp, "persona_state.json"))

# 真 HTTP 服务：本地注册表 + 审批中枢。假设备，不连网。
http_reg = make_registry()
http_broker = ApprovalBroker(timeout=8.0)
srv = GS.FleetServer(registry=http_reg, persona=_persona, approval=http_broker)
srv.start()
BASE = srv.base_url
TOKEN = srv.token

try:
    # ⑦a 不带 token → 401
    st, js, _ = http_json(f"{BASE}/api/approvals")
    check(st == 401, f"不带 token → 401（实际 {st}）")
    check(bool(js and js.get("err")), "401 带明确原因")

    # ⑦b 带错 token → 403
    st, js, _ = http_json(f"{BASE}/api/approvals", token="wrong-token")
    check(st == 403, f"带错 token → 403（实际 {st}）")
    check(bool(js and js.get("err")), "403 带明确原因")

    # ⑦c 带对 token → 200
    st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
    check(st == 200, f"带对 token → 200（实际 {st}）")
    check(js is not None and "pending" in js, "待审批清单带 pending 字段")
    check(js is not None and js.get("enabled") is True, "答复说明审批中枢开着")

    # ⑦d 走 HTTP 发起的调用会挂起，/api/approvals 能看见它在等
    _ACTED.clear()
    call_res = {}

    def _do_call():
        st, js, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                              body={"cap": "act.click", "args": {"x": 5},
                                    "tag": "http1", "session": "会话甲"},
                              timeout=20)
        call_res["st"] = st
        call_res["js"] = js

    th = threading.Thread(target=_do_call)
    th.daemon = True
    th.start()
    deadline = time.time() + 4
    while time.time() < deadline:
        st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
        if js and js.get("pending"):
            break
        time.sleep(0.05)
    st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
    pend = (js or {}).get("pending") or []
    check(len(pend) == 1, f"界面上能看出「哪些能力在等审批」（pending {len(pend)} 条）")
    check(pend and pend[0].get("cap") == "act.click",
          "待审批清单里有能力名（%s）" % (pend[0].get("cap") if pend else None))
    check(pend and pend[0].get("tag") == "http1",
          "待审批清单回显了调用方的 tag（认领回自己那次调用）")
    check(_ACTED == [], "等审批期间设备没被碰过")

    if pend:
        rid = pend[0]["request_id"]
        # 错会话回应 → 409，且不生效
        st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                              method="POST",
                              body={"request_id": rid, "approve": True,
                                    "session": "会话乙"})
        check(st == 409, f"跨会话回应 → 409（实际 {st}）")
        check(bool(js and js.get("err")), "409 带明确原因")
        check(_ACTED == [], "跨会话回应没有让调用执行")

        # 本会话回应 → 放行
        st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                              method="POST",
                              body={"request_id": rid, "approve": True,
                                    "session": "会话甲"})
        check(st == 200, f"本会话回应 → 200（实际 {st}）")
        th.join(timeout=10)
        check(call_res.get("st") == 200, "这次调用最后有答复（%s）" % call_res.get("st"))
        cjs = call_res.get("js") or {}
        check(cjs.get("ok") is True, "确认后调用成功（%s）" % cjs.get("err"))
        check(len(_ACTED) == 1, "确认后设备被调用了一次")

        # 同一个 request_id 再回应 → 409（一次性）
        st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                              method="POST",
                              body={"request_id": rid, "approve": True,
                                    "session": "会话甲"})
        check(st == 409, f"同一个 request_id 重复回应 → 409（实际 {st}）")
        check(bool(js and js.get("err")), "重复回应的 409 带原因")

        # ⑦g 历史随 /api/approvals 返回（W2 #8）
        st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
        hist = (js or {}).get("history") or []
        check(isinstance(hist, list) and len(hist) >= 1,
              "历史随待审批清单一并返回（%d 条）" % len(hist))
        if hist:
            hh = hist[0]
            check(hh.get("decision") in ("approved", "denied", "timeout"),
                  "历史条目有 decision（%s）" % hh.get("decision"))
            check(hh.get("closed_at", 0) > 0, "历史条目带 closed_at")

        # ⑦h 同 token 跨会话接管（W2 #2）走 HTTP
        _ACTED.clear()
        tk_res = {}

        def _do_tk():
            st, js, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                                  body={"cap": "act.click", "args": {"x": 31},
                                        "tag": "http-tk", "session": "会话甲"},
                                  timeout=20)
            tk_res["st"] = st
            tk_res["js"] = js

        thtk = threading.Thread(target=_do_tk)
        thtk.daemon = True
        thtk.start()
        rid_tk = None
        deadline = time.time() + 4
        while time.time() < deadline:
            st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
            pend = (js or {}).get("pending") or []
            if pend:
                rid_tk = pend[0]["request_id"]
                break
            time.sleep(0.05)
        if rid_tk:
            st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                                  method="POST",
                                  body={"request_id": rid_tk, "approve": True,
                                        "session": "会话乙", "takeover": True})
            check(st == 200, f"HTTP 接管（takeover=True）→ 200（实际 {st}）")
            thtk.join(timeout=10)
            cjs = tk_res.get("js") or {}
            check(cjs.get("ok") is True, "HTTP 接管后调用成功（%s）" % cjs.get("err"))
        else:
            check(False, "HTTP 接管：没等到待审批条目")

    # ⑦e 回应缺字段 → 400 带原因
    st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                          method="POST", body={"approve": True})
    check(st == 400, f"缺 request_id → 400（实际 {st}）")
    check(bool(js and js.get("err")), "400 带原因")

    st, js, _ = http_json(f"{BASE}/api/approval/respond", token=TOKEN,
                          method="POST", body={"request_id": "x"})
    check(st == 400, f"缺 approve → 400（实际 {st}）")

    # ⑦f token 不出现在任何答复里
    st, js, _ = http_json(f"{BASE}/api/approvals", token=TOKEN)
    check(bool(js) and TOKEN not in json.dumps(js, ensure_ascii=False),
          "token 不出现在待审批清单里")

finally:
    srv.stop()
    try:
        for root, dirs, files in os.walk(_tmp, topdown=False):
            for fn in files:
                os.remove(os.path.join(root, fn))
            for dn in dirs:
                os.rmdir(os.path.join(root, dn))
        os.rmdir(_tmp)
    except OSError:
        pass

# ------------------------------------------------------------------ ⑧ 判定权在注册表层

def _read(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


_reg_src = _read("fleet/core/registry.py")
_appr_src = _read("fleet/gui/approval.py")
_ui_src = _read("ui/app.js")
_srv_src = _read("fleet/gui/server.py")

check("needs_approval" in _reg_src and "side_effect" in _reg_src,
      "「要不要审批」的判定写在注册表层（fleet/core/registry.py）")
check("cap.destructive" in _reg_src and "level = 2" in _reg_src,
      "审批级别（破坏性 → 2 级）由注册表算")
check("destructive" in _appr_src and "double" in _appr_src,
      "破坏性双确认在审批中枢里判（不靠 UI 自觉）")
# UI 层不许自己判定
check(not re.search(r"side_effect\s*\?", _ui_src),
      "UI 层不自己判「要不要审批」")
check("approval/respond" in _ui_src and "approve" in _ui_src,
      "UI 只回传「用户点了确认」这个信号")
check("/api/approvals" in _ui_src, "UI 会拉待审批清单（界面上看得见）")
# 前端不许直接调设备
check("registry.call(" not in _ui_src and "/api/call" in _ui_src,
      "前端走 /api/call，不直接碰设备")
# 审批中枢不自己判定
check("needs_approval" not in _appr_src,
      "审批中枢不重做「要不要审批」的判定（判定权在注册表层）")
# 超时 fail-closed
check("审批超时未确认" in _appr_src, "超时的理由写明「审批超时未确认」")
# 一次性
check("_closed" in _appr_src and "不能重复回应" in _appr_src,
      "审批记录进「已结束」账本，重复回应被拒")

# ------------------------------------------------------------------ ⑨ 历史留存（W2 #8）
# 已结束的审批（批准/拒绝/超时）必须留快照给 UI 看「刚才批了/拒了什么」。
# 这是收口 W2 已知限制 #8——之前审批进程一关就没、UI 也看不到。
def _run_one(b, x, approve, reason=""):
    _ACTED.clear()
    th = threading.Thread(target=lambda: reg.call(
        "act.click", device="act:local", args={"x": x}, gate=b.gate_for(tag="h%d" % x)))
    th.daemon = True
    th.start()
    dl = time.time() + 3
    while not b.pending() and time.time() < dl:
        time.sleep(0.02)
    p = b.pending()
    if p:
        b.respond(p[0]["request_id"], approve, reason=reason)
        th.join(timeout=5)
    return p

# 拒绝 → 历史 denied
b_deny = ApprovalBroker(timeout=5.0)
_run_one(b_deny, 101, False, reason="历史留存测试：拒")
hd = b_deny.history()
check(len(hd) >= 1, "拒绝后历史至少留一条（实际 %d）" % len(hd))
if hd:
    check(hd[0].get("decision") == "denied", "历史 decision=denied（%s）" % hd[0].get("decision"))
    check("历史留存" in (hd[0].get("closed_reason") or ""), "历史带 closed_reason（%s）" % hd[0].get("closed_reason"))
    check(hd[0].get("closed_at", 0) > 0, "历史带 closed_at（%s）" % hd[0].get("closed_at"))

# 确认 → 历史 approved，且确实执行了
b_ok = ApprovalBroker(timeout=5.0)
_run_one(b_ok, 102, True)
hk = b_ok.history()
check(hk and hk[0].get("decision") == "approved", "确认后历史 decision=approved")
check(len(_ACTED) == 1 and _ACTED[0][0] == "act.click", "历史与实际一致：确认的那条真执行了")

# 超时 → 历史 timeout
b_to = ApprovalBroker(timeout=0.5)
_ACTED.clear()
reg.call("act.click", device="act:local", args={"x": 103}, gate=b_to.gate_for(tag="h103"))
ht = b_to.history()
check(ht and ht[0].get("decision") == "timeout", "超时也进历史（decision=timeout）")
check(ht and "审批超时未确认" in (ht[0].get("closed_reason") or ""), "超时历史理由写清「审批超时未确认」")

# ------------------------------------------------------------------ ⑩ 同 token 跨会话接管（W2 #2）
# 收口点：同一个 broker、同一个 token，但 session 不同的另一个窗口/页面
# （同一主人的另一个界面）应该能接管这条审批——之前跨页面接不过来。
# 安全基线不变：不带 takeover 仍被拒；跨 broker 重放仍被 _closed 账本挡死。
b_tk = ApprovalBroker(timeout=8.0, session="会话甲")
_ACTED.clear()
th_tk0 = threading.Thread(target=lambda: reg.call(
    "act.click", device="act:local", args={"x": 201},
    gate=b_tk.gate_for(tag="tk", session="会话甲")))
th_tk0.daemon = True
th_tk0.start()
dl = time.time() + 3
while not b_tk.pending() and time.time() < dl:
    time.sleep(0.02)
ptk = b_tk.pending()
if ptk:
    ok, why = b_tk.respond(ptk[0]["request_id"], True, session="会话乙")
    check(not ok, "接管前：别的会话仍被拒（%s）" % why)
    check(len(b_tk.pending()) == 1, "被拒的回应没吃掉这条审批")
    ok, why = b_tk.respond(ptk[0]["request_id"], True, session="会话乙", takeover=True)
    check(ok, "同 token 别的会话带 takeover=True 可接管（%s）" % why)
    th_tk0.join(timeout=5)
    check(len(_ACTED) == 1, "接管后这次调用执行了一次")

# ------------------------------------------------------------------ #2/#8 代码层钉死
check("def history(" in _appr_src, "审批中枢提供 history()（UI 拉历史）")
check("_history" in _appr_src, "已结束审批进 _history 快照账本")
check('"history": hist' in _srv_src or "history\": hist" in _srv_src,
      "服务端把历史随 /api/approvals 一并返回")
check("renderApprovalHistory" in _ui_src, "前端渲染审批历史面板")
check("takeover" in _appr_src and "takeover" in _srv_src,
      "接管开关在审批中枢 + 服务端打通")
check('"接管"' in _ui_src, "前端对「非本会话发起」的审批打「接管」标")

print("t9 approval:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
