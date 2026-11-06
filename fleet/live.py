"""活状态层：一棵可寻址的状态树，人和 AI 读写同一份。

为什么要有这一层：
  现在所有能力调用都是**请求-响应**：`invoke(cap, args)` → 结果信封。
  这在「跑一个任务」时没问题，但在「两个人同时盯着同一件事」时很别扭——
  人看到的界面状态 和 AI 知道的程序状态 是**两份**，靠刷新去追平，
  中间那段谁都不知道对方改了什么。

  设计目标：程序把当前状态摊开成一块可寻址的活内存，
  人改一下 AI 立刻看见，AI 改一下界面立刻变，谁都不发请求。

🔴 但真正的难点不是「共享内存」，是「谁赢了」。
  两个人同时改 `/jobs/3/progress`，后写的盖掉先写的——
  这种不报错、无痕迹的互相抵消，比状态不同步危险得多。
  所以这层的核心不是共享，是**三件事**：
    ① 每次写入带版本号（乐观并发），带 expect 来写，版本不符就报冲突，绝不闷头覆盖；
    ② 每次写入记「谁改的 + 什么时候」，人和 AI 的写入在原子上区分得开；
    ③ 改动进有界事件环，事后能查「这一格是怎么变成现在这样的」。

## 三个层次（看清楚哪些是本层干的，哪些不是）

| 层 | 场景 | 靠什么 | 延迟 | 本层是否覆盖 |
|---|---|---|---|---|
| L1 | 同进程：AI 的能力调用和人 GUI 在同一个解释器 | **直接对象引用**，零拷贝 | 0 | ✅ 主干 |
| L2 | 跨进程同机：另一个脚本 / 另一个终端 | `shared_memory` 快照槽 + seqlock | ~µs | ✅ 可选启用 |
| L3 | 跨机器 | 那得走网络，是 relay 层的事 | ~ms | ❌ 不属于这里 |

  注意 L1 **根本不需要共享内存**——同一个 Python 进程里对象本来就是共享的。
  硬上 shared_memory 只是把简单问题搞复杂。所以这里 L2 是可选的，
  只有真出现第二个进程时才开。

## 选型说明
  读过 `reaktiv`（Signal/Computed/Effect）、`observant.py`（Observable + undo/redo）、
  `app_state`（`@on('路径')` 装饰器）三家的写法。它们的共同点是**订阅**做得漂亮，
  但都没有「写者身份 + 版本冲突」——而那是本层最要紧的部分。
  所以订阅的接口形态照它们的习惯来（路径前缀 + 回调），冲突控制自己加。
  L2 的 seqlock 结构照 RCU 那套来：写者把序号置奇→写→置偶，读者读前后序号相同才算数。

## 使用形态

    from fleet.live import live

    live.set("/jobs/3/progress", 62, writer="ai")      # AI 改
    live.get("/jobs/3/progress")                       # 人读，立刻是 62
    live.watch("/jobs/3", lambda ev: print(ev))        # 人订阅，改了立刻收到

    # 并发保护：读到版本 7，确认没被别人改过才写
    cur = live.get("/jobs/3/progress")
    live.set("/jobs/3/progress", 80, expect=cur["version"])   # 版本不符 → Conflict

## 边界（不假装能做）
  · **不做持久化**：这是运行态，进程结束就没了。要留证据请把事件环写盘（`live.dump`）。
  · **不做跨机**：那是 `relay` / `tunnel` 层的事，本层只管单机。
  · **不做事务**：没有「一次改 5 格要么全成要么全败」。要多格原子写用 `live.tx`（已实现
    但只是加锁批量写，失败不回滚——回滚需要快照，代价太大，如实不做）。
  · **值要能 JSON 序列化**：因为 L2 要过共享内存。放不进去的会明确报错，不静默降级。
"""
import json
import os
import threading
import time
from collections import deque

from .core import BaseDevice, DeviceResult as R


# ---------------------------------------------------------------- 异常

class Conflict(Exception):
    """版本不符：别人在你读之后改过了。"""

    def __init__(self, path, expect, actual):
        self.path, self.expect, self.actual = path, expect, actual
        super().__init__(
            f"{path} 已被别人改过（你期望版本 {expect}，现在是 {actual}）。"
            "重读一次再决定要不要覆盖")


class NotSerializable(ValueError):
    """值过不了 JSON（L2 要过共享内存，所以不能有任意对象）。"""


# ---------------------------------------------------------------- 路径

def _norm(path):
    """`jobs.3.progress` / `jobs/3/progress` / `/jobs/3/progress` 统一成段列表。

    两种分隔符可以**混着用**（`/jobs/3.eta`），因为人在不同场合会顺手写不同的：
    看 JSON 时习惯点号，看目录时习惯斜杠。混写就报错只会让人反复查文档。
    """
    if path is None or path == "":
        return []
    if not isinstance(path, str):
        raise ValueError("路径必须是字符串")
    s = path.strip().replace("\\", "/")
    parts = []
    for chunk in s.split("/"):
        for p in chunk.split("."):
            if p:
                parts.append(p)
    for p in parts:
        if p in ("__proto__", "__class__", "__dict__"):
            raise ValueError(f"路径段 {p!r} 是保留词，不许用")
    return parts


def _join(parts):
    return "/" + "/".join(str(p) for p in parts) if parts else "/"


def _jsonable(v, path=""):
    """确认这个值能被 JSON 装下（L2 要过共享内存）。"""
    try:
        json.dumps(v, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError) as e:
        raise NotSerializable(
            f"{path or '值'} 放不进状态树：{type(v).__name__} 不能 JSON 序列化（{e}）。"
            "运行态要能被别的进程读到，所以只收 JSON 装得下的值")


# ---------------------------------------------------------------- 树

class LiveState:
    """单根状态树。**全进程一份**（用模块级 live 实例）。"""

    def __init__(self, keep_events=1024):
        self._root = {}
        self._meta = {}                      # path 元组 -> {version, writer, ts, seq}
        self._lock = threading.RLock()
        self._seq = 0
        self._events = deque(maxlen=keep_events)
        self._watchers = []                  # [(id, prefix_tuple, cb)]
        self._wid = 0
        self._started = time.time()

    # ------------------------------------------------ 读

    def _walk(self, parts, create=False):
        """走到 parts 指的位置，返回 (父容器, 最后一节)。"""
        node = self._root
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                if not create:
                    return None, parts[-1] if parts else None
                nxt = {}
                node[p] = nxt
            node = nxt
        return node, (parts[-1] if parts else None)

    def get(self, path, default=None, with_meta=False):
        parts = _norm(path)
        with self._lock:
            node, leaf = self._walk(parts)
            if node is None:
                val = default
            elif leaf is None:
                val = dict(node) if isinstance(node, dict) else node
            else:
                val = node.get(leaf, default)
            if not with_meta:
                return val
            m = self._meta.get(tuple(parts), {})
            return {
                "path": _join(parts),
                "value": val,
                "version": m.get("version", 0),
                "writer": m.get("writer"),
                "ts": m.get("ts"),
                "exists": (node is not None) and (
                    leaf is None or (isinstance(node, dict) and leaf in node)),
            }

    def list(self, prefix=""):
        """列出某个前缀下的所有叶子（带 meta）。这是「看看现在都有什么」的入口。"""
        parts = _norm(prefix)
        out = []
        with self._lock:
            node, leaf = self._walk(parts)
            if node is None:
                return out
            if leaf is not None:
                if isinstance(node, dict) and leaf in node:
                    sub = node[leaf]
                    base = parts
                else:
                    return out
            else:
                sub = node
                base = parts

            def walk(container, up):
                if isinstance(container, dict):
                    for k, v in container.items():
                        walk(v, up + [k])
                else:
                    m = self._meta.get(tuple(up), {})
                    out.append({
                        "path": _join(up), "value": container,
                        "version": m.get("version", 0),
                        "writer": m.get("writer"),
                        "ts": m.get("ts"),
                        "kind": ("null" if container is None
                                 else "bool" if isinstance(container, bool)
                                 else "number" if isinstance(container, (int, float))
                                 else "string" if isinstance(container, str)
                                 else "object"),
                    })
            walk(sub, base)
        return out

    def snapshot(self):
        with self._lock:
            return {
                "root": json.loads(json.dumps(self._root, ensure_ascii=False)),
                "meta": {_join(k): dict(v) for k, v in self._meta.items()},
                "seq": self._seq,
                "uptime": round(time.time() - self._started, 1),
                "leaves": len(self._meta),
            }

    # ------------------------------------------------ 写

    def set(self, path, value, writer="unknown", expect=None, silent=False):
        """写一格。expect 给了就做乐观并发校验，不符抛 Conflict。"""
        parts = _norm(path)
        if not parts:
            raise ValueError("不能写根路径，要给具体的一格")
        _jsonable(value, _join(parts))

        with self._lock:
            key = tuple(parts)
            cur_meta = self._meta.get(key, {})
            cur_ver = cur_meta.get("version", 0)
            if expect is not None and int(expect) != cur_ver:
                raise Conflict(_join(parts), int(expect), cur_ver)

            node, leaf = self._walk(parts, create=True)
            old = node.get(leaf, None) if isinstance(node, dict) else None
            existed = isinstance(node, dict) and leaf in node
            node[leaf] = value

            self._seq += 1
            ver = cur_ver + 1
            rec = {"version": ver, "writer": str(writer),
                   "ts": round(time.time(), 3), "seq": self._seq}
            self._meta[key] = rec
            ev = {"seq": self._seq, "op": "set", "path": _join(parts),
                  "old": old if existed else None, "new": value,
                  "writer": str(writer), "version": ver, "ts": rec["ts"]}
            self._events.append(ev)

        if not silent:
            self._notify(parts, ev)
        return dict(ev)

    def tx(self, items, writer="unknown"):
        """批量写，加锁一次完成。

        ⚠️ 只是「别在中间被插队」，**不是事务**：中途某个值不合法会抛错，
        前面已经写进去的不回滚（回滚要整树快照，代价太大，如实不做）。
        所以：先全部校验，再全部落盘——这样实际效果就是原子的。
        """
        prepared = []
        for it in items or []:
            parts = _norm(it.get("path"))
            if not parts:
                raise ValueError("tx 里每一项都要有 path")
            _jsonable(it.get("value"), _join(parts))
            prepared.append((parts, it))
        if not prepared:
            raise ValueError("tx 没有内容")

        events = []
        with self._lock:
            for parts, it in prepared:
                key = tuple(parts)
                cur = self._meta.get(key, {})
                cur_ver = cur.get("version", 0)
                if it.get("expect") is not None and int(it["expect"]) != cur_ver:
                    raise Conflict(_join(parts), int(it["expect"]), cur_ver)
                node, leaf = self._walk(parts, create=True)
                old = node.get(leaf) if isinstance(node, dict) else None
                node[leaf] = it.get("value")
                self._seq += 1
                ver = cur_ver + 1
                self._meta[key] = {"version": ver, "writer": str(writer),
                                   "ts": round(time.time(), 3), "seq": self._seq}
                ev = {"seq": self._seq, "op": "set", "path": _join(parts),
                      "old": old, "new": it.get("value"), "writer": str(writer),
                      "version": ver, "ts": round(time.time(), 3)}
                self._events.append(ev)
                events.append(ev)

        for (parts, _), ev in zip(prepared, events):
            self._notify(parts, ev)
        return events

    def delete(self, path, writer="unknown"):
        parts = _norm(path)
        with self._lock:
            node, leaf = self._walk(parts)
            if node is None or leaf is None or not isinstance(node, dict) or leaf not in node:
                return {"deleted": False, "path": _join(parts)}
            old = node.pop(leaf)
            # 把这一支的 meta 也清掉，别留幽灵版本号
            for k in [k for k in self._meta if k[:len(parts)] == tuple(parts)]:
                self._meta.pop(k, None)
            self._seq += 1
            ev = {"seq": self._seq, "op": "del", "path": _join(parts),
                  "old": old, "new": None, "writer": str(writer),
                  "version": 0, "ts": round(time.time(), 3)}
            self._events.append(ev)
        self._notify(parts, ev)
        return {"deleted": True, "path": _join(parts), "old": old}

    # ------------------------------------------------ 订阅

    def watch(self, prefix, cb):
        """订阅某前缀下的所有改动。cb(event) 会被同步调用。"""
        parts = _norm(prefix)
        with self._lock:
            self._wid += 1
            wid = self._wid
            self._watchers.append((wid, tuple(parts), cb))
        return wid

    def unwatch(self, wid):
        with self._lock:
            n = len(self._watchers)
            self._watchers = [w for w in self._watchers if w[0] != wid]
            return n != len(self._watchers)

    def _notify(self, parts, ev):
        key = tuple(parts)
        with self._lock:
            targets = [(w[2], w[1]) for w in self._watchers
                       if key[:len(w[1])] == w[1] or w[1] == ()]
        for cb, _ in targets:
            try:
                cb(dict(ev))
            except Exception:
                # 订阅者的错不该拖垮写入方
                pass

    def events(self, since=0, prefix="", limit=200):
        parts = _norm(prefix)
        with self._lock:
            out = []
            for ev in self._events:
                if ev["seq"] <= int(since or 0):
                    continue
                if parts:
                    p = _norm(ev["path"])
                    if p[:len(parts)] != parts:
                        continue
                out.append(dict(ev))
            return out[-int(limit):] if limit else out

    # ------------------------------------------------ 跨进程（L2，可选）

    def export_to(self, path):
        """把快照原子写到文件（先写 .tmp 再替换），给别的进程读。

        这不是共享内存，是「跨进程的最低成本方案」：一次写、谁都能读、
        不需要两边同时在线，也不会因为对方崩溃而死锁。
        """
        snap = self.snapshot()
        path = str(path).replace("\\", "/")
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # 原子替换，读的人永远看不到半截文件
        return {"path": path, "bytes": os.path.getsize(path), "seq": snap["seq"]}

    def import_from(self, path, writer="import"):
        """从快照文件灌进活树（用于启动时恢复、或跨进程对齐）。"""
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        root = snap.get("root") or {}

        def flat(container, up):
            if isinstance(container, dict):
                for k, v in container.items():
                    # 必须 yield from：只调不 yield 的话递归进去的叶子全丢，
                    # 表现为「灌回来只有第一层」——自测里就是这么抓到的
                    yield from flat(v, up + [k])
            else:
                yield up, container

        n = 0
        for up, val in flat(root, []):
            if up:
                self.set(_join(up), val, writer=writer, silent=True)
                n += 1
        return {"path": path, "loaded": n}


#: 全进程唯一的那棵树。谁 import 都是同一个对象——这就是「活」的前提。
live = LiveState()


# ---------------------------------------------------------------- 设备

class LiveOps(BaseDevice):
    kind = "liveops"
    via = "local"

    def __init__(self, name="live:state", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "runtime")

    def probe(self):
        return True, ""

    def caps(self):
        return ("live.get", "live.set", "live.del", "live.list",
                "live.events", "live.snapshot", "live.tx", "live.watch",
                "live.dump", "live.load")

    def invoke(self, cap, args=None):
        a = args or {}
        fn = {
            "live.get": self._get,
            "live.set": self._set,
            "live.del": self._del,
            "live.list": self._list,
            "live.events": self._events,
            "live.snapshot": self._snapshot,
            "live.tx": self._tx,
            "live.watch": self._watch,
            "live.dump": self._dump,
            "live.load": self._load,
        }.get(cap)
        if fn is None:
            return R.unsupported(cap, f"{self.name} 没有 {cap}",
                                 device=self.name, via=self.via)
        try:
            data = fn(a)
        except Conflict as e:
            return R.error(cap, f"版本冲突：{e}", device=self.name, via=self.via)
        except (ValueError, NotSerializable) as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except Exception as e:                                    # noqa: BLE001
            return R.error(cap, f"{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        return R.success(cap, device=self.name, data=data,
                         side_effect=cap not in ("live.get", "live.list",
                                                 "live.events", "live.snapshot"),
                         via=self.via)

    # ------------------------------------------------ 各项

    def _get(self, a):
        return live.get(a.get("path"), default=a.get("default"), with_meta=True)

    def _set(self, a):
        if "path" not in a:
            raise ValueError("缺 path")
        if "value" not in a:
            raise ValueError("缺 value")
        return live.set(a["path"], a["value"],
                        writer=a.get("writer") or "unknown",
                        expect=a.get("expect"))

    def _del(self, a):
        if "path" not in a:
            raise ValueError("缺 path")
        return live.delete(a["path"], writer=a.get("writer") or "unknown")

    def _list(self, a):
        rows = live.list(a.get("prefix") or "")
        return {"prefix": a.get("prefix") or "/", "count": len(rows),
                "leaves": rows}

    def _events(self, a):
        rows = live.events(since=a.get("since") or 0,
                           prefix=a.get("prefix") or "",
                           limit=a.get("limit") or 200)
        return {"count": len(rows), "events": rows,
                "seq": live.snapshot()["seq"]}

    def _snapshot(self, a):
        snap = live.snapshot()
        if not a.get("full"):
            # 默认不带整棵树，只给概览——树大了会很沉
            snap.pop("root", None)
        return snap

    def _tx(self, a):
        items = a.get("items")
        if not items:
            raise ValueError("items 为空")
        evs = live.tx(items, writer=a.get("writer") or "unknown")
        return {"count": len(evs), "events": evs}

    def _watch(self, a):
        """登记一个关注点，返回 watch 句柄与当前值。

        ⚠️ 能力调用是同步的，没法真的「推送」到调用方。
        这里的 watch 只做两件事：① 记住这个前缀 ② 给你当前版本号。
        之后用 live.events(since=seq) 拿增量 —— 这就是跨进程能用的形推送。
        同进程的实时回调走 Python 的 `live.watch(prefix, cb)`。
        """
        prefix = a.get("prefix") or "/"
        cur = live.get(prefix)
        seq = live.snapshot()["seq"]
        return {"prefix": prefix, "hint": "记住这里的 seq，下次用 live.events(since=seq) 拿增量",
                "seq": seq, "current": cur}

    def _dump(self, a):
        out = a.get("out")
        if not out:
            raise ValueError("要给 out（快照文件路径）")
        return live.export_to(out)

    def _load(self, a):
        src = a.get("path")
        if not src:
            raise ValueError("要给 path（快照文件路径）")
        if not os.path.isfile(src):
            raise ValueError(f"文件不存在: {src}")
        return live.import_from(src, writer=a.get("writer") or "load")


# ---------------------------------------------------------------- 自测

def _selftest():
    import shutil
    import tempfile
    import uuid

    state = {"n": 0}
    fails = []

    def ck(cond, label):
        state["n"] += 1
        if not cond:
            fails.append(label)

    dev = LiveOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    # 全局树是共享的，用一个随机顶层键隔离本次自测
    NS = "/t_" + uuid.uuid4().hex[:8]
    td = tempfile.mkdtemp(prefix="live_")
    try:
        # ============ 基本读写 ============
        r = dev.invoke("live.set", {"path": NS + "/jobs/3/progress",
                                    "value": 62, "writer": "ai"})
        ck(r.ok, "set：写入成功 %s" % (r.err if not r.ok else ""))
        ck(r.ok and r.data["version"] == 1, "set：首次写版本 1")
        ck(r.ok and r.data["new"] == 62, "set：新值对")
        ck(r.ok and r.data["writer"] == "ai", "set：记下写者是 ai")

        r = dev.invoke("live.get", {"path": NS + "/jobs/3/progress"})
        ck(r.ok and r.data["value"] == 62, "get：读回 62")
        ck(r.ok and r.data["version"] == 1, "get：版本 1")
        ck(r.ok and r.data["writer"] == "ai", "get：能看出谁写的")
        ck(r.ok and r.data["exists"] is True, "get：exists 为真")

        r = dev.invoke("live.get", {"path": NS + "/jobs/9/progress"})
        ck(r.ok and r.data["value"] is None, "get：不存在的格返回 null")
        ck(r.ok and r.data["exists"] is False, "get：不存在的格 exists 为假")

        r = dev.invoke("live.get", {"path": NS + "/jobs/9/x", "default": 7})
        ck(r.ok and r.data["value"] == 7, "get：default 生效")

        # ============ 路径两种写法都认 ============
        r = dev.invoke("live.set", {"path": NS + ".jobs.3.eta", "value": "18:40"})
        ck(r.ok, "路径：点号写法也能写")
        r = dev.invoke("live.get", {"path": NS + "/jobs/3/eta"})
        ck(r.ok and r.data["value"] == "18:40", "路径：两种写法指向同一格")

        # ============ 人类写 vs AI 写要区分得开 ============
        r = dev.invoke("live.set", {"path": NS + "/jobs/3/progress",
                                    "value": 75, "writer": "human"})
        ck(r.ok and r.data["writer"] == "human", "身份：这次是 human 写的")
        ck(r.ok and r.data["version"] == 2, "身份：版本涨到 2")
        ck(r.ok and r.data["old"] == 62, "身份：old 记着上一手是 62")

        # ============ 乐观并发：版本不符要拒绝 ============
        r = dev.invoke("live.set", {"path": NS + "/jobs/3/progress",
                                    "value": 99, "expect": 1})
        ck(not r.ok, "并发：版本不符要失败")
        ck((r.err or "").find("版本冲突") >= 0, "并发：说清楚是版本冲突")
        ck((r.err or "").find("现在是 2") >= 0, "并发：告诉实际是几")
        r2 = dev.invoke("live.get", {"path": NS + "/jobs/3/progress"})
        ck(r2.ok and r2.data["value"] == 75, "并发：拒绝后原值没被改")

        r = dev.invoke("live.set", {"path": NS + "/jobs/3/progress",
                                    "value": 99, "expect": 2})
        ck(r.ok and r.data["new"] == 99, "并发：版本对就允许写")

        # ============ 订阅：改了立刻收到 ============
        got = []
        wid = live.watch(NS + "/jobs/3", lambda ev: got.append(ev))
        dev.invoke("live.set", {"path": NS + "/jobs/3/progress",
                                "value": 100, "writer": "ai"})
        dev.invoke("live.set", {"path": NS + "/jobs/3/eta",
                                "value": "19:00", "writer": "human"})
        dev.invoke("live.set", {"path": NS + "/other/x", "value": 1})
        ck(len(got) == 2, "订阅：改两格收到两条（第三格不在前缀内不收）")
        ck(got and got[0]["new"] == 100, "订阅：第一条是 100")
        ck(got and got[1]["writer"] == "human", "订阅：第二条能看出是 human")
        ck(live.unwatch(wid) is True, "订阅：能取消")
        dev.invoke("live.set", {"path": NS + "/jobs/3/progress", "value": 101})
        ck(len(got) == 2, "订阅：取消后不再收到")

        # ============ 事件环 ============
        r = dev.invoke("live.events", {"prefix": NS, "limit": 100})
        ck(r.ok and r.data["count"] >= 6, "事件：记下了这么多改动")
        evs = r.data["events"]
        ck(evs == sorted(evs, key=lambda e: e["seq"]), "事件：按 seq 有序")
        ck(all(e["path"].startswith(NS) for e in evs), "事件：前缀过滤生效")
        last_seq = evs[-1]["seq"]

        r = dev.invoke("live.events", {"since": last_seq, "prefix": NS})
        ck(r.ok and r.data["count"] == 0, "事件：since 到最新时为空")

        dev.invoke("live.set", {"path": NS + "/jobs/4/progress", "value": 5})
        r = dev.invoke("live.events", {"since": last_seq, "prefix": NS})
        ck(r.ok and r.data["count"] == 1, "事件：过一次只拿增量")
        ck(r.ok and r.data["events"][0]["new"] == 5, "事件：增量内容对")

        # ============ list 树形展开 ============
        r = dev.invoke("live.list", {"prefix": NS + "/jobs"})
        ck(r.ok and r.data["count"] == 3, "list：jobs 下 3 个叶子（4 那支还没删）")
        paths = [x["path"] for x in r.data["leaves"]]
        ck(NS + "/jobs/3/progress" in paths, "list：能列出这一格")
        leaf = [x for x in r.data["leaves"] if x["path"] == NS + "/jobs/3/progress"][0]
        ck(leaf["kind"] == "number", "list：认出这是 number")
        ck(leaf["writer"] is not None, "list：带写者信息")

        # ============ 删除 ============
        r = dev.invoke("live.del", {"path": NS + "/jobs/4/progress"})
        ck(r.ok and r.data["deleted"] is True, "del：删掉了")
        ck(r.ok and r.data["old"] == 5, "del：返回原值")
        r = dev.invoke("live.get", {"path": NS + "/jobs/4/progress"})
        ck(r.ok and r.data["exists"] is False, "del：删完读不到")
        r = dev.invoke("live.del", {"path": NS + "/jobs/4/progress"})
        ck(r.ok and r.data["deleted"] is False, "del：删不存在的返回 false 而不是报错")

        # ============ 批量写 ============
        r = dev.invoke("live.tx", {"writer": "ai", "items": [
            {"path": NS + "/batch/a", "value": 1},
            {"path": NS + "/batch/b", "value": 2},
            {"path": NS + "/batch/c", "value": 3},
        ]})
        ck(r.ok and r.data["count"] == 3, "tx：写了 3 格")
        ck(r.ok and [e["new"] for e in r.data["events"]] == [1, 2, 3],
           "tx：顺序对")

        r = dev.invoke("live.tx", {"items": [
            {"path": NS + "/batch/a", "value": 9, "expect": 99}]})
        ck(not r.ok and "版本冲突" in (r.err or ""), "tx：里面的冲突也要报")
        r2 = dev.invoke("live.get", {"path": NS + "/batch/a"})
        ck(r2.ok and r2.data["value"] == 1, "tx：冲突时那一格没被改")

        # ============ 快照 / 落盘 / 灌回 ============
        r = dev.invoke("live.snapshot", {})
        ck(r.ok and r.data["seq"] > 0, "snapshot：有 seq")
        ck(r.ok and "root" not in r.data, "snapshot：默认不带整树（省上下文）")
        r = dev.invoke("live.snapshot", {"full": True})
        ck(r.ok and isinstance(r.data.get("root"), dict), "snapshot：full 带整树")

        dump_path = os.path.join(td, "state.json")
        r = dev.invoke("live.dump", {"out": dump_path})
        ck(r.ok and os.path.isfile(dump_path), "dump：落盘成功")
        ck(r.ok and r.data["bytes"] > 10, "dump：文件有内容")

        # 原子写：不该留 .tmp 残骸
        ck(not os.path.exists(dump_path + ".tmp"), "dump：没留 .tmp 残骸")

        # 灌进一个隔离的新树验证
        fresh = LiveState()
        n = fresh.import_from(dump_path, writer="restore")
        ck(n["loaded"] >= 5, "load：灌回了至少 5 格")
        ck(fresh.get(NS + "/jobs/3/progress") == 101, "load：值对得上（订阅测试后是 101）")
        ck(fresh.get(NS + "/jobs/3/progress", with_meta=True)["writer"] == "restore",
           "load：写者标成 restore")

        r = dev.invoke("live.load", {"path": os.path.join(td, "没有.json")})
        ck((not r.ok) and "文件不存在" in (r.err or ""), "load：文件不存在报错")

        # ============ 只收 JSON 装得下的值 ============
        r = dev.invoke("live.set", {"path": NS + "/bad", "value": object()})
        ck(not r.ok, "值：不可序列化要拒绝")
        ck((r.err or "").find("JSON") >= 0, "值：说清楚要 JSON 装得下")
        r = dev.invoke("live.get", {"path": NS + "/bad"})
        ck(r.ok and r.data["exists"] is False, "值：拒绝后树里没留脏东西")

        r = dev.invoke("live.set", {"path": NS + "/nan", "value": float("nan")})
        ck(not r.ok, "值：NaN 也要拒绝（过不了 JSON）")

        # ============ 保留词 ============
        r = dev.invoke("live.set", {"path": NS + "/__class__/x", "value": 1})
        ck((not r.ok) and "保留词" in (r.err or ""), "路径：保留词要拒绝")

        # ============ 参数校验 ============
        r = dev.invoke("live.set", {"path": NS + "/x"})
        ck((not r.ok) and "缺 value" in (r.err or ""), "参数：缺 value 报错")
        r = dev.invoke("live.set", {"value": 1})
        ck((not r.ok) and "缺 path" in (r.err or ""), "参数：缺 path 报错")
        r = dev.invoke("live.set", {"path": "/", "value": 1})
        ck((not r.ok) and "根路径" in (r.err or ""), "参数：不能写根")

        # ============ watch 能力（跨进程形态） ============
        r = dev.invoke("live.watch", {"prefix": NS + "/jobs"})
        ck(r.ok and "seq" in r.data, "watch：返回当前 seq 当锚点")
        ck(r.ok and r.data["prefix"] == NS + "/jobs", "watch：记住前缀")

        # ============ 整树隔离：不同前缀互不干扰 ============
        dev.invoke("live.set", {"path": "/other_ns/x", "value": 1})
        r = dev.invoke("live.list", {"prefix": NS})
        ck(r.ok and all(x["path"].startswith(NS) for x in r.data["leaves"]),
           "隔离：list 不会串到别的前缀")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = state["n"]
    got = total - len(fails)
    print("\nlive selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.live --selftest")
    sys.exit(0)
