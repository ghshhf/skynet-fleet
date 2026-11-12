"""手机算力节点（fleet.kinds.phone_node）——把「能力下放到手机」正式接进注册表。

这台设备不挂在 adb 上，而是挂在手机自己跑的两类服务上（都是「手机自己」，不依赖 PC）：
  · llama-server（默认 18080）：本地推理 / 人格包渲染；
  · fleet-node agent（默认 8770，见 phone/agent.py）：手机执行层 —— shell / 文件 /
    本地双模型 / Termux:API 硬件能力，全部在手机 Debian 沙箱里跑。

区别很重要：
  · AndroidPhone（adb）是「我去操控它」；
  · PhoneNode 是「它替我干活」——算力和执行都在设备上，PC 只是发问/编排的人。

人格不在这台设备里，也不在权重里：它在记忆包里，渲染成 system 注进请求。
所以同一台手机、同一个 llama-server 进程，换包就换人，进程都不用重启。

能力：
    phone.node.status       手机算力现状（服务是否在跑、模型、内存、记忆包）  只读
    memory.pack.*          本地人格包 列表/渲染/差分/同步                      只读/写手机
    phone.llm.ask          用指定人格包 + 手机上的权重回答一句话             占用算力
    # 以下经 fleet-node agent（8770）暴露，手机自己动手，完全权限、不防御：
    phone.exec             在手机沙箱执行 shell 命令
    phone.fs.read/write/list  手机沙箱文件读写列
    phone.llm.chat        手机本地双模型答一句话（fast/acc 自选）
    phone.termux.*        手机硬件/系统能力（相机/传感/电池/通知/剪贴板/TTS/手电/...）
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

# llama-server（推理，18080）与 fleet-node agent（执行层，8770）是手机上两个独立服务。
# 前者走 _base() / _OPENER，后者走 _agent_base() / _AGENT_OPENER。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _agent_base():
    """手机运行时 agent（fleet-node）端点，默认 8770。手机自己跑的执行层。"""
    return os.environ.get("PHONE_AGENT_BASE", "http://127.0.0.1:8770")


_AGENT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base():
    return os.environ.get("PHONE_LLM_BASE", "http://127.0.0.1:18080")


def _get(path, timeout=8):
    req = urllib.request.Request(_base().rstrip("/") + path)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _agent_post(cap, args, timeout=60):
    """把 catalog 侧 phone.* 执行能力转发到手机运行时 agent（默认 8770）。

    手机 agent 是独立运行时（Debian 沙箱里跑、自己监听端口、自己干活），
    不是 PC 的附件 —— 这里只是「控制面 → 手机节点」的调用通道，
    **不加任何闸门、不预设 denied**：完全权限、执行提权到最高（主人定调）。
    结果照 agent 回传的 ok/error 折成 DeviceResult，不替主人做风险判断。
    """
    payload = json.dumps({"cap": cap, "args": args or {}}).encode("utf-8")
    req = urllib.request.Request(
        _agent_base().rstrip("/") + "/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with _AGENT_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


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


# 手机 agent（fleet-node / 8770）暴露的执行类能力，经本桥接进 catalog。
# 命名对齐 agent 本地协议名 + phone. 前缀；短信/通话类（无 SIM）不接。
# 这些能力在 catalog 里不标 side_effect/approval —— 主人开完全权限，执行提权到最高，
# 平台 fail-closed 默认拒绝不适用于此节点。
_AGENT_CAPS = (
    "phone.exec",            # ← agent: shell.exec
    "phone.fs.read",         # ← agent: fs.read
    "phone.fs.write",        # ← agent: fs.write
    "phone.fs.list",         # ← agent: fs.list
    "phone.llm.chat",       # ← agent: llm.chat
    "phone.sys.info",       # ← agent: sys.info（agent 版本 / 平台 / 能力清单）
    "phone.sandbox.info",   # ← agent: sandbox.info（沙箱根 / 内核 / 磁盘）
    "phone.llm.models",     # ← agent: llm.models（手机上可用的权重）
    "phone.termux.camera",
    "phone.termux.sensor",
    "phone.termux.battery",
    "phone.termux.notification",
    "phone.termux.clipboard.get",
    "phone.termux.clipboard.set",
    "phone.termux.toast",
    "phone.termux.tts",
    "phone.termux.torch",
    "phone.termux.wifi",
    "phone.termux.wifi.scan",
    "phone.termux.location",
    "phone.termux.vibrate",
    "phone.termux.brightness",
    "phone.termux.volume",
    "phone.termux.ir",
    "phone.termux.info",
)

CAPS = ("phone.node.status", "memory.pack.list", "memory.pack.render",
        "memory.pack.diff", "memory.pack.sync", "phone.llm.ask") + _AGENT_CAPS

PERSONA_DIR = os.environ.get("FLEET_PERSONAS") or os.path.join(ROOT, "personas")


class PhoneNode(BaseDevice):
    kind = "phone_node"
    via = "http"

    def __init__(self, name="phone:compute", **kw):
        super().__init__(name, via="http", **kw)

    def probe(self):
        # 手机节点在线 = 推理端点(18080) 或 运行时 agent(8770) 任一可达。
        # 两者都是「手机自己」，不依赖 PC。
        try:
            h = _get("/health", timeout=5)
        except (urllib.error.URLError, OSError, ValueError):
            h = None
        agent_ok = False
        try:
            ah = _agent_post("sys.info", {}) or {}
            agent_ok = ah.get("ok") is True
        except Exception:
            agent_ok = False
        if h and (h.get("status") == "ok" or h.get("ok") is not None):
            self.meta["addr"] = _base()
            return True, ""
        if agent_ok:
            self.meta["addr"] = _agent_base()
            return True, ""
        return False, ("手机算力端点不通（llama %s / agent %s 都连不上）："
                       "先在手机上起 llama-server 或 fleet-node agent"
                       % (_base(), _agent_base()))

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        # ---- 手机 agent 执行类能力（8770，完全权限，不防御）----
        if cap in _AGENT_CAPS:
            return self._agent_invoke(cap, args)
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

    def _agent_invoke(self, cap, args):
        """catalog 的 phone.* 执行能力 → 手机 agent 本地协议名，转发并折结果。

        映射：catalog 名 → agent 本地 cap（去掉 phone. 前缀；少数改名单独列）。
        短信/通话不在此（无 SIM）。不加 denied/确认：完全权限，结果客观上报。
        """
        local = {
            "phone.exec": "shell.exec",
            "phone.fs.read": "fs.read",
            "phone.fs.write": "fs.write",
            "phone.fs.list": "fs.list",
            "phone.llm.chat": "llm.chat",
        }.get(cap) or cap.removeprefix("phone.")
        out = _agent_post(local, args)
        if out.get("ok") is True:
            return R.success(cap, self.name, data=out, via=self.via)
        if "error" in out:
            return R.error(cap, out["error"], device=self.name, via=self.via)
        return R.unavailable(cap, "手机 agent 无有效回执：%r" % out,
                             device=self.name, via=self.via)

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
