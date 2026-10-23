"""手机算力节点（fleet.kinds.phone_node）——把「能力下放到手机」正式接进注册表。

这台设备不挂在 adb 上，而是挂在手机自己跑的 llama-server 上（HTTP，OpenAI 兼容）。
区别很重要：
  · AndroidPhone（adb）是「我去操控它」；
  · PhoneNode（llama-server）是「它替我干活」——算力和人格都在设备上，PC 只是发问的人。

人格不在这台设备里，也不在权重里：它在记忆包里，渲染成 system 注进请求。
所以同一台手机、同一个 llama-server 进程，换包就换人，进程都不用重启。

能力：
    phone.node.status   手机算力现状（服务是否在跑、模型、内存、记忆包）  只读
    memory.pack.list    本地有哪些人格包                                 只读
    memory.pack.render  渲染某个人格包的 system prompt（本地算，不碰设备）只读
    memory.pack.diff    本地包与手机上已同步的内容差多少                  只读
    memory.pack.sync    把记忆包增量下发到手机（有副作用：写手机）
    phone.llm.ask       用指定人格包 + 手机上的权重回答一句话（有副作用：占用算力）
"""
from ..core import BaseDevice, DeviceResult as R
from .. import memory_pack as M

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

CAPS = ("phone.node.status", "memory.pack.list", "memory.pack.render",
        "memory.pack.diff", "memory.pack.sync", "phone.llm.ask")

PERSONA_DIR = os.environ.get("FLEET_PERSONAS") or os.path.join(ROOT, "personas")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base():
    return os.environ.get("PHONE_LLM_BASE", "http://127.0.0.1:18080")


def _get(path, timeout=8):
    req = urllib.request.Request(_base().rstrip("/") + path)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _personas():
    """本地人格包目录：personas/<name>/pack.json"""
    out = []
    if not os.path.isdir(PERSONA_DIR):
        return out
    for name in sorted(os.listdir(PERSONA_DIR)):
        d = os.path.join(PERSONA_DIR, name)
        if os.path.isfile(os.path.join(d, "pack.json")):
            out.append((name, d))
    return out


def _pack_dir(name):
    if not name:
        raise ValueError("缺 pack（人格包名）")
    d = name if os.path.isdir(name) else os.path.join(PERSONA_DIR, name)
    if not os.path.isfile(os.path.join(d, "pack.json")):
        raise ValueError(f"没有这个记忆包：{name}（可用：{[n for n, _ in _personas()]}）")
    return d


class PhoneNode(BaseDevice):
    kind = "phone_node"
    via = "http"

    def __init__(self, name="phone:compute", **kw):
        super().__init__(name, via="http", **kw)

    def probe(self):
        try:
            h = _get("/health", timeout=5)
        except (urllib.error.URLError, OSError) as e:
            return False, (f"手机算力端点不通（{_base()}）：{type(e).__name__}"
                           f"（先 adb forward tcp:18080 tcp:8080 并在手机上起 llama-server）")
        except ValueError as e:
            return False, f"端点返回的不是 JSON：{e}"
        if h.get("status") == "ok" or h.get("ok") is not None:
            self.meta["addr"] = _base()
            return True, ""
        return False, f"端点活着但状态不对：{h}"

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "phone.node.status":
                return self._status()
            if cap == "memory.pack.list":
                rows = []
                for name, d in _personas():
                    p = M.Pack.load(d)
                    rows.append({"name": name, "pack": p.digest() if len(p) else "mem-empty",
                                 "count": len(p), "by_kind": p.by_kind()})
                return R.success(cap, self.name, via=self.via,
                                 data={"packs": rows, "count": len(rows),
                                       "dir": PERSONA_DIR})
            if cap == "memory.pack.render":
                p = M.Pack.load(_pack_dir(args.get("pack")))
                r = p.render(budget_tokens=int(args.get("budget") or 800),
                             query=args.get("query") or "")
                return R.success(cap, self.name, via=self.via, data=r)
            if cap == "memory.pack.diff":
                p = M.Pack.load(_pack_dir(args.get("pack")))
                import provision_phone as PP
                remote = PP._remote_manifest(os.path.basename(_pack_dir(args.get("pack"))))
                d = p.diff(remote)
                return R.success(cap, self.name, via=self.via, data={
                    "local": p.digest(), "local_count": len(p),
                    "remote": remote.get("pack"), "need": len(d["need"]),
                    "local_extra": len(d["local_extra"]), "in_sync": d["in_sync"]})
            if cap == "memory.pack.sync":
                import provision_phone as PP
                d = _pack_dir(args.get("pack"))
                name = os.path.basename(d)
                rc = PP.main(["sync", name, "--from", d])
                return R.success(cap, self.name, side_effect=True, via=self.via,
                                 data={"pack": name, "rc": rc})
            if cap == "phone.llm.ask":
                import phone_llm as PL
                PL.BASE = _base()
                kw = {"n": int(args.get("n") or 160),
                      "temp": float(args.get("temp") or 0.7),
                      "budget": int(args.get("budget") or 800),
                      "query": args.get("query") or "",
                      "stream": False}
                if args.get("pack"):
                    r = PL.ask_with_pack(_pack_dir(args["pack"]), args.get("ask") or "", **kw)
                else:
                    r = PL.chat([{"role": "user", "content": args.get("ask") or ""}],
                                n=kw["n"], temp=kw["temp"], stream=False)
                return R.success(cap, self.name, side_effect=True, via=self.via, data=r)
        except ValueError as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except (urllib.error.URLError, OSError) as e:
            return R.unavailable(cap, f"手机算力端点不通：{type(e).__name__}: {e}",
                                 device=self.name, via=self.via)
        return R.unsupported(cap, f"{self.name} 没有这项能力", device=self.name, via=self.via)

    def _status(self):
        out = {"endpoint": _base(), "service": False}
        try:
            out["models"] = _get("/v1/models", timeout=6)
            out["service"] = True
        except Exception as e:
            out["err"] = f"{type(e).__name__}: {e}"
        try:
            import adbx
            rc, txt, _ = adbx.termux(
                'echo "mem:"; grep -E "MemAvailable" /proc/meminfo; '
                'echo "models:"; ls -l "$HOME/models" 2>/dev/null | awk \'{print $5, $9}\'; '
                'echo "packs:"; ls -1 "$HOME/skynet/memory" 2>/dev/null')
            if rc == 0:
                out["device"] = txt.strip()
        except Exception as e:
            out["device_err"] = f"{type(e).__name__}: {e}"
        return R.success("phone.node.status", self.name, via=self.via, data=out)
