"""审批闸门：有副作用的能力只在这一处判，**默认就是问，不是放**。

🔴 这个模块为什么存在（2026-10-25）
  `Registry.call()` 里有一条硬规矩：有副作用的能力，**没注入 gate 一律拒绝**。
  这条规矩本身是对的（fail-closed），但云侧 `FleetAgentServer` 从建起来那天起
  `gate=None`，于是 `input.tap` / `input.type` / `pc.shell` / `pcctl.*` 这些
  「真动手」的能力全都撞在那一行上，回一句「需要审批闸门」就完了。
  手机端看着是连上了，一按真格的全黑 —— **闸门不是关着，是压根没装**。

  这个文件就是把那个洞补上：装一道**真正的**闸门，而不是把 `gate` 换成一个
  `lambda: True` 把门拆了。

三条硬规矩（都对应一次真实的踩坑）

  ① **不认识的就问，不是放。** 没配置 = 待确认。谁都没配过策略时，
     默认放行等于「不知道就开门」——那是把 fail-closed 改成 fail-open。
  ② **策略是数据，不是代码。** 允许清单写在 `fleet_gate.json` 里，
     主人一句话就能改回来；删掉这个文件，立刻退回「什么都问」。
     **代码里不出现任何一个能力名**——今天手机上叫 `input.tap`，
     明天接入别家设备叫别的，写死就是错的。
  ③ **一次一票，票绑参数。** 批准只对「这条能力 + 这台设备 + 这组参数」
     有效，参数一变就作废。不做「批准一次管一小时」那种顺手后门——
     那等于把一次性确认偷偷升级成常驻授权。

分级（level 由 Registry 给出，本模块只认数）
  · level 1 = 有副作用但可逆（点一下、输一行字）
  · level 2 = 破坏性 / 明确要人确认（`destructive` 或 `approval == "ask"`）
  **level 2 永远不走 allow 清单**：破坏性动作必须一次一票，配了清单也不行。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import threading
import time

VERSION = 1
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "fleet_gate.json")

#: 票据默认活多久（秒）。短是故意的：确认完就该立刻用掉。
TICKET_TTL = 120.0
#: 同时挂着的待确认票据上限，防止被灌爆内存。
TICKET_MAX = 64


def _norm_args(args):
    """参数规范化：同一组参数必须得到同一个指纹。

    `json.dumps` 默认按插入顺序，`{"x":1,"y":2}` 与 `{"y":2,"x":1}` 会算出
    两个不同的哈希 —— 那「票绑参数」就成了「票绑参数顺序」，一换个写法的
    调用方就得重新问一遍。所以定死 `sort_keys=True`。
    """
    try:
        raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False,
                         default=str)
    except (TypeError, ValueError):
        raw = repr(sorted((args or {}).items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _match(patterns, name):
    """通配匹配。`input.*` 能盖住 `input.tap`，但 `input` 不能 —— 故意的。"""
    for p in patterns or ():
        if not p:
            continue
        if p == name or fnmatch.fnmatchcase(name, p):
            return p
    return None


class ApprovalGate:
    """`gate(cap, dev, args, level) -> bool | (bool, reason)` 的真实现。

    可以当一个 callable 直接交给 `catalog.build(gate=...)`。
    """

    def __init__(self, path="", mode=None, allow=None, deny=None,
                 ticket_ttl=TICKET_TTL):
        self.path = path or DEFAULT_PATH
        self._mode = mode            # ask | allow | deny（None = 读文件/默认 ask）
        self._allow = list(allow or [])
        self._deny = list(deny or [])
        self.ticket_ttl = float(ticket_ttl)
        self._tickets = {}           # tid -> dict
        self._lk = threading.RLock()
        self._mt = 0.0               # 策略文件 mtime，改了就重读
        self._policy_note = ""
        self._load(force=True)

    # ---------------- 策略读取（数据驱动，改文件即生效） ----------------

    def _load(self, force=False):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = 0.0
        if not force and (not mt or mt == self._mt):
            return
        with self._lk:
            self._mt = mt
            if not mt:
                return               # 没文件 = 保持构造时的参数 / 默认
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as e:
                # 策略文件坏了**不能当场放行**，也不能让服务起不来。
                # 记下原因，按「什么都没配」= 全问 处理。
                self._policy_note = "策略文件读不了（按全问处理）: %s" % e
                return
            if self._mode is None:
                m = str(data.get("mode") or "ask").strip().lower()
                self._mode = m if m in ("ask", "allow", "deny") else "ask"
            if not self._allow:
                self._allow = [str(x) for x in (data.get("allow") or [])]
            if not self._deny:
                self._deny = [str(x) for x in (data.get("deny") or [])]
            if data.get("ticket_ttl"):
                try:
                    self.ticket_ttl = float(data["ticket_ttl"])
                except (TypeError, ValueError):
                    pass
            self._policy_note = str(data.get("note") or "")

    def reload(self):
        self._load(force=True)
        return self.snapshot()

    # ---------------- 票据 ----------------

    def _gc(self, now=None):
        now = now or time.time()
        dead = [k for k, t in self._tickets.items()
                if t["used"] or now - t["at"] > self.ticket_ttl]
        for k in dead:
            self._tickets.pop(k, None)
        if len(self._tickets) > TICKET_MAX:
            for k in sorted(self._tickets, key=lambda x: self._tickets[x]["at"])[
                    :len(self._tickets) - TICKET_MAX]:
                self._tickets.pop(k, None)

    def _open_ticket(self, cap, dev, args, level, why):
        """开一张待确认票。同一组 (能力,设备,参数) 复用同一张，不刷屏。"""
        key = (cap.name, dev.name if dev else "", _norm_args(args))
        with self._lk:
            self._gc()
            for tid, t in self._tickets.items():
                if t["key"] == key and not t["used"]:
                    t["at"] = time.time()          # 续期，不是新票
                    return tid, t
            tid = hashlib.sha256(
                ("%s|%s|%s|%.6f" % (key[0], key[1], key[2], time.time()))
                .encode("utf-8")).hexdigest()[:10]
            t = {"id": tid, "key": key, "cap": cap.name,
                 "dev": key[1], "level": int(level), "args_h": key[2],
                 "args_preview": _preview(args), "why": why,
                 "at": time.time(), "used": False,
                 "title": getattr(cap, "title", "") or cap.name}
            self._tickets[tid] = t
            return tid, t

    def pending(self, only_alive=True):
        with self._lk:
            self._gc()
            out = []
            for t in self._tickets.values():
                if only_alive and t["used"]:
                    continue
                out.append({"id": t["id"], "cap": t["cap"], "device": t["dev"],
                            "level": t["level"], "title": t["title"],
                            "args": t["args_preview"], "why": t["why"],
                            "age": round(time.time() - t["at"], 1),
                            "ttl": self.ticket_ttl})
            return sorted(out, key=lambda x: x["age"])     # 挂得最久的排最前

    def find_pending(self, cap_name, dev_name="", args=None):
        """查「刚才那次调用是不是挂了张票」。给上层把票号带回去用。"""
        key = (cap_name, dev_name or "", _norm_args(args))
        with self._lk:
            self._gc()
            for t in self._tickets.values():
                if t["key"] == key and not t["used"]:
                    return dict(t, args=t["args_preview"])
        return None

    def approve(self, tid, allow=True):
        """人点了「允许」/「拒绝」。

        允许：把票标成 used=False → 但下一次匹配调用会**消费**它（一次性）。
        拒绝：直接删票。
        """
        with self._lk:
            t = self._tickets.get(str(tid))
            if not t:
                return {"ok": False, "err": "没有这张票（可能已过期或已被用掉）"}
            if t["used"]:
                return {"ok": False, "err": "这张票已经用掉了"}
            if not allow:
                self._tickets.pop(str(tid), None)
                return {"ok": True, "id": str(tid), "allow": False,
                        "note": "已拒绝，票已作废"}
            t["approved"] = True
            t["at"] = time.time()        # 批准时刻重新计时
            return {"ok": True, "id": str(tid), "allow": True,
                    "cap": t["cap"], "device": t["dev"],
                    "note": "已批准，同一组参数的**下一次**调用生效，用完即废"}

    # ---------------- 判定主体 ----------------

    def __call__(self, cap, dev, args, level):
        self._load()                 # 文件改过就重读，不用重启

        name = getattr(cap, "name", "") or ""
        dname = getattr(dev, "name", "") or ""
        level = int(level or 1)

        # ① 硬否决最优先：写了 deny 的能力，连票都不给 —— 这是**配置意图**，
        #    不是「等人确认」。给票等于把否决变成一个可以点的按钮。
        hit = _match(self._deny, name)
        if hit:
            return False, "策略明文否决 %s（fleet_gate.json deny 里有 %r）" % (name, hit)

        # ② 已批准的票：消费掉，放行。票绑 (能力,设备,参数) 三元组。
        with self._lk:
            key = (name, dname, _norm_args(args))
            for t in self._tickets.values():
                if t["key"] == key and t.get("approved") and not t["used"]:
                    t["used"] = True
                    return True, ""

        # ③ 破坏性动作永不静默：level 2 必须一次一票，allow 清单盖不住它。
        if level >= 2:
            tid, t = self._open_ticket(cap, dev, args, level,
                                       "破坏性动作，需要单独确认")
            return False, ("破坏性动作 %s @ %s 需要人确认（待确认 #%s，"
                           "确认后同一组参数的这一次调用才会执行）"
                           % (name, dname or "-", tid))

        # ④ 免问清单（只在 level 1 生效）
        hit = _match(self._allow, name)
        if hit:
            return True, ""

        # ⑤ 兜底：按 mode 走。默认 ask = 开票等人点。
        mode = (self._mode or "ask").lower()
        if mode == "allow":
            return True, ""
        if mode == "deny":
            return False, ("策略 mode=deny，%s 一律不做（要放开就改 fleet_gate.json "
                           "的 mode 或 allow 清单）" % name)
        tid, t = self._open_ticket(cap, dev, args, level,
                                   "有副作用的能力，策略里没写免问")
        return False, ("需要确认：%s @ %s（待确认 #%s；批准后重发这一次即可）"
                       % (name, dname or "-", tid))

    # ---------------- 快照 ----------------

    def snapshot(self):
        self._load()
        with self._lk:
            self._gc()
            live = [t for t in self._tickets.values() if not t["used"]]
        return {"ok": True, "version": VERSION,
                "mode": self._mode or "ask",
                "allow": list(self._allow), "deny": list(self._deny),
                "ticket_ttl": self.ticket_ttl,
                "path": self.path, "file_present": bool(self._mt),
                "note": self._policy_note,
                "pending": len(live),
                "waiting": [{"id": t["id"], "cap": t["cap"], "device": t["dev"],
                             "level": t["level"], "args": t["args_preview"]}
                            for t in sorted(live, key=lambda x: x["at"])]}


def _preview(args, n=160):
    """参数预览：人要靠它判断「该不该点允许」，但别把整段文本抖进 UI。"""
    try:
        s = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = repr(args)
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 自测

def _fake_cap(name, destructive=False):
    class _C:
        pass
    c = _C()
    c.name = name
    c.title = name
    c.destructive = destructive
    c.approval = "ask" if destructive else ""
    return c


def _fake_dev(name="dev:test"):
    class _D:
        pass
    d = _D()
    d.name = name
    return d


def _selftest():
    import tempfile

    cap = _fake_cap("input.tap")
    dev = _fake_dev()

    # ① 什么都没配 = 全问，且开出一张票（不是放行）
    g = ApprovalGate(path=os.path.join(tempfile.gettempdir(), "_no_such_gate.json"))
    ok, why = g(cap, dev, {"text": "设置"}, 1)
    assert ok is False and "待确认" in why, why
    tid = why.split("#")[1].split("；")[0].split("）")[0]
    assert tid in [t["id"] for t in g.pending()], g.pending()

    # ② 批准之后，同一组参数放行一次，第二次又得问
    assert g.approve(tid)["ok"] is True
    assert g(cap, dev, {"text": "设置"}, 1)[0] is True
    assert g(cap, dev, {"text": "设置"}, 1)[0] is False, "票居然能重复用"

    # ③ 参数变了，票不认（票绑参数）
    g2 = ApprovalGate(path=os.path.join(tempfile.gettempdir(), "_no_such_gate2.json"))
    _, why = g2(cap, dev, {"x": 1, "y": 2}, 1)
    tid = why.split("#")[1].split("；")[0].split("）")[0]
    g2.approve(tid)
    assert g2(cap, dev, {"y": 2, "x": 1}, 1)[0] is True, "换个键序就失配 = 绑了顺序"
    g2b = ApprovalGate(path=os.path.join(tempfile.gettempdir(), "_no_such_gate2b.json"))
    _, why = g2b(cap, dev, {"x": 1}, 1)
    tid = why.split("#")[1].split("；")[0].split("）")[0]
    g2b.approve(tid)
    assert g2b(cap, dev, {"x": 2}, 1)[0] is False, "参数变了还能过"

    # ④ 破坏性动作即使写进 allow 清单也不静默
    p = os.path.join(tempfile.gettempdir(), "_gate_t.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"mode": "allow", "allow": ["input.*"], "deny": []}, f)
    g3 = ApprovalGate(path=p)
    assert g3(cap, dev, {"text": "a"}, 1)[0] is True, "allow 清单没生效"
    boom = _fake_cap("pcctl.proc.kill", destructive=True)
    assert g3(boom, dev, {"pid": 1}, 2)[0] is False, "破坏性动作被 allow 清单放过了"

    # ⑤ deny 连票都不给
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"mode": "ask", "deny": ["pcctl.proc.*"]}, f)
    g4 = ApprovalGate(path=p)
    assert g4(boom, dev, {"pid": 1}, 2)[0] is False
    assert g4.find_pending("pcctl.proc.kill", dev.name, {"pid": 1}) is None, "deny 居然开了票"

    os.unlink(p)
    print("gate 自测：5 组全过（全问/一次一票/票绑参数/破坏性不静默/deny 无票）")


if __name__ == "__main__":
    _selftest()
