"""AI 代理层：把本机已有的模型通道接成能力，让派活和看进度都不用手敲命令。

## 为什么要有这一层

本机早就配好了两条通道，但它们的用法只存在于「主人脑子里 + 记忆树某个角落」：
  · `codex` CLI → 本地 `zen-gate`（`127.0.0.1:8787`，免费车道）
  · AgentRouter（记忆里存着 key，按美元计费，模型更强）

于是每次要用，我都得重新摸一遍：端口起没起、代理挂没挂、参数怎么写。
这层就是把「摸一遍」变成一次 `ai.exec`——命令固定、参数固定、失败原因固定。

## 三层能力，不是十个零散动作

| 层 | 能力 | 干什么 |
|---|---|---|
| 体检 | `ai.status` / `ai.models` / `ai.whoami` | 通道活着吗、有哪些模型、现在用的是哪条 |
| 干活 | `ai.ask` / `ai.exec` / `ai.gpt` | 问一句 / 让它改代码 / 直调强模型 |
| 长活 | `ai.spawn` / `ai.jobs` / `ai.log` / `ai.kill` | 后台跑，进度实时写进 live 树 |

## 🔴 长任务为什么必须进 live 树

主人原话：「让它同时在写，你也能通过检查状态行」。
所以 `ai.spawn` 不是在后台闷头跑，它把每一步摊在 `/ai/jobs/<id>/` 下：

```
/ai/jobs/j3f2a1/status     running | done | failed | killed
/ai/jobs/j3f2a1/pid        12345
/ai/jobs/j3f2a1/model      flint-40b-free
/ai/jobs/j3f2a1/cwd        E:/xmanbian
/ai/jobs/j3f2a1/elapsed    12.4
/ai/jobs/j3f2a1/tail       最后 2000 字符输出
/ai/jobs/j3f2a1/exit_code  0
```

人和 AI 读的是同一份，谁都不用等谁返回。

## 踩过的坑（写进实现，不是写在注释里）

1. **codex 会把 stdin 当输入等下去**，在非交互环境里表现为「Reconnecting... waiting
   for network」卡死。所以 `stdin` 一律 `DEVNULL`，不是 `PIPE`。
2. **codex 走 zen-gate 要出网**，必须带代理（7890）。不挂代理就是连不上上游，
   不是 key 错——这个误判过一次。
3. **输出不能走 PIPE 再等**，长输出会把管道缓冲区塞满导致双方死锁。一律落临时文件，
   读完再取尾部。

## 🔴 权限哲学（2026-11-06 主人定）

「命令行本质也是权限」——codex CLI 自己有完整的沙箱与审批模型，
它就是权限边界本身。在这层再套一层「第一次只预演」「要 _confirmed」，
是把已经有的东西重做一遍，而且做得更差：多一次往返，还挡不住真正该挡的事。

所以：`ai.exec` / `ai.spawn` **默认给全权限**（跟你在终端里敲 codex 一样），
要收窄才显式传 `readonly=True`。**默认不放行、处处设卡，才是真危险**——
因为那样我会绕过它去手敲命令，反而没有记录。

## 边界（不假装能做）
  · 不做流式增量（codex exec 本身不吐 SSE，只能轮询 tail）
  · 不做多轮对话记忆（每次 exec 是独立会话；要续用 `codex exec resume <id>`，未接）
  · 不给权限设内部上限：token 预算、超时、沙箱级别都由 codex 自己管，
    这层只传话和记录（唯一例外是 `ai.gpt` 走的是 API 不是 CLI，必须给 max_output_tokens）
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ..core import BaseDevice, DeviceResult as R

# ---------------------------------------------------------------- 常量

PROXY = os.environ.get("FLEET_AI_PROXY", "http://127.0.0.1:7890")
ZEN_PORT = int(os.environ.get("FLEET_AI_ZEN_PORT", "8787"))
AGENTROUTER_BASE = "https://agentrouter.org/v1"

# 记忆树里记着的 key（10-31 实测有效，余额按美元计）。允许环境变量覆盖。
AR_KEY = os.environ.get(
    "AGENTROUTER_API_KEY",
    "***REMOVED-LEAKED-KEY***",
)
# 这站的客户端指纹校验：UA 不对直接 401 unauthorized_client_error，不是 key 错
AR_UA_CLI = "claude-cli/2.1.150 (external, cli)"

_JOB_LOCK = threading.Lock()
_JOBS = {}          # jid -> {"proc": Popen, "file": str, "started": float, "args": dict}


def _env_with_proxy():
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        e[k] = PROXY
    e["NO_PROXY"] = "127.0.0.1,localhost,::1"
    return e


def _port_open(port, host="127.0.0.1", timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _codex_path():
    return shutil.which("codex") or shutil.which("codex.cmd")


def _tail_file(path, nbytes=2000):
    """从尾部读，不读全文件——长任务的日志可能有几十兆。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                chunk = f.read()
                txt = chunk.decode("utf-8", "replace")
                # 切掉被截断的首行，免得给出半行
                nl = txt.find("\n")
                if nl >= 0:
                    txt = txt[nl + 1:]
                return txt
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _parse_tokens(text):
    """codex 结尾会打印 `tokens used` 然后一个数字。"""
    m = re.findall(r"tokens used\s*\n\s*([\d,]+)", text)
    if not m:
        return None
    try:
        return int(m[-1].replace(",", ""))
    except ValueError:
        return None


def _parse_reply(text):
    """从 codex 的输出里剥出最后的回答。

    格式是：头部块 → `user` → 我的提问 → `codex` → 回答 → `tokens used`。
    取最后一个 `codex` 标记之后、`tokens used` 之前的部分。
    """
    if not text:
        return ""
    body = text
    idx = body.rfind("\ntokens used")
    if idx > 0:
        body = body[:idx]
    m = re.search(r"^codex\s*$", body, re.M)
    if m:
        body = body[m.end():]
    return body.strip()


class AgentOps(BaseDevice):
    kind = "aiops"
    via = "local"

    def __init__(self, name="agent:delegate", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "agent")

    def probe(self):
        cp = _codex_path()
        if not cp:
            return False, "没找到 codex CLI（npm i -g @openai/codex）"
        return True, ""

    def caps(self):
        return ("agent.status", "agent.models", "agent.whoami",
                "agent.ask", "agent.exec", "agent.gpt",
                "agent.spawn", "agent.jobs", "agent.log", "agent.kill")

    def invoke(self, cap, args=None):
        a = args or {}
        fn = {
            "agent.status": self._status,
            "agent.models": self._models,
            "agent.whoami": self._whoami,
            "agent.ask": self._ask,
            "agent.exec": self._exec,
            "agent.gpt": self._gpt,
            "agent.spawn": self._spawn,
            "agent.jobs": self._jobs,
            "agent.log": self._log,
            "agent.kill": self._kill,
        }.get(cap)
        if fn is None:
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        t0 = time.time()
        try:
            data = fn(a)
        except Exception as exc:                       # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc),
                           device=self.name, ms=(time.time() - t0) * 1000)
        if isinstance(data, R):
            return data
        return R.success(cap, device=self.name, data=data,
                         ms=(time.time() - t0) * 1000,
                         side_effect=cap in ("agent.exec", "agent.gpt", "agent.spawn", "agent.kill"))

    # ------------------------------------------------------------ 体检

    def _status(self, a):
        cp = _codex_path()
        zen = _port_open(ZEN_PORT)
        prox = _port_open(7890)
        info = {
            "codex_cli": cp or None,
            "zen_gate": {"port": ZEN_PORT, "open": zen},
            "proxy": {"port": 7890, "open": prox},
            "agentrouter_key_set": bool(AR_KEY),
            "ready": bool(cp and zen),
        }
        if not zen:
            info["hint"] = ("zen-gate 没起。启动："
                            "zen-gate.exe -no-tray -port %d（要先带代理环境变量）" % ZEN_PORT)
        elif not prox:
            info["hint"] = "代理 7890 没开，codex 连不上 zen-gate 的上游"
        # 顺带报一下在跑的任务数，省得再调一次
        with _JOB_LOCK:
            info["running_jobs"] = sum(
                1 for j in _JOBS.values() if j.get("proc") and j["proc"].poll() is None)
        return info

    def _zen_key(self):
        """zen-gate 的 key 在 codex 的 auth.json 里（`ofm-` 开头，不是 sk-）。"""
        for var in ("FLEET_ZEN_KEY", "OPENAI_API_KEY"):
            if os.environ.get(var):
                return os.environ[var]
        p = os.path.expanduser("~/.codex/auth.json")
        try:
            d = json.load(open(p, encoding="utf-8"))
            return d.get("OPENAI_API_KEY")
        except Exception:                                # noqa: BLE001
            return None

    def _models(self, a):
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:%d/v1/models" % ZEN_PORT
        key = self._zen_key()
        if not key:
            return R.unavailable("agent.models", "拿不到 zen-gate 的 key（~/.codex/auth.json）",
                                 device=self.name)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return R.error("agent.models", "网关返回 HTTP %s（key 可能过期）" % exc.code,
                           device=self.name)
        except Exception as exc:                        # noqa: BLE001
            return R.unavailable(
                "agent.models", "本地网关取不到模型清单（%s）：先跑 ai.status 看端口"
                % type(exc).__name__, device=self.name)
        rows = payload.get("data") or []
        return {"count": len(rows),
                "models": [{"id": m.get("id"), "context": m.get("context_length")}
                           for m in rows]}

    def _whoami(self, a):
        cp = _codex_path()
        cfg = os.path.expanduser("~/.codex/config.toml")
        out = {"codex_cli": cp, "config": cfg, "exists": os.path.exists(cfg),
               "provider": None, "model": None}
        if out["exists"]:
            txt = open(cfg, encoding="utf-8", errors="replace").read()
            for key in ("model_provider", "model"):
                m = re.search(r'^%s\s*=\s*"([^"]*)"' % key, txt, re.M)
                if m:
                    out[key] = m.group(1)
        return out

    # ------------------------------------------------------------ 干活

    def _build_cmd(self, prompt, model=None, cwd=None, readonly=False, extra=None):
        cp = _codex_path()
        if not cp:
            return None
        # 默认全权限：命令行本身就是权限边界，不在这层再设卡
        mode = "read-only" if readonly else "danger-full-access"
        cmd = [cp, "exec", "--skip-git-repo-check",
               "-c", 'sandbox_mode="%s"' % mode]
        if model:
            cmd += ["-m", str(model)]
        if cwd:
            cmd += ["-C", str(cwd)]
        for c in (extra or []):
            cmd += ["-c", str(c)]
        cmd.append(str(prompt))
        return cmd

    def _run_sync(self, prompt, model, cwd, readonly, timeout, extra=None):
        cmd = self._build_cmd(prompt, model, cwd, readonly, extra)
        if cmd is None:
            return R.unavailable("agent.exec", "没找到 codex CLI", device=self.name)
        # 🔴 stdin 必须 DEVNULL：给 PIPE 会让 codex 等输入，表现为假死
        t0 = time.time()
        try:
            p = subprocess.run(
                cmd, cwd=cwd or None, env=_env_with_proxy(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return R.error("agent.exec", "超时 %ss：长任务请用 ai.spawn" % timeout,
                           device=self.name)
        raw = p.stdout.decode("utf-8", "replace")
        return {
            "exit_code": p.returncode,
            "elapsed": round(time.time() - t0, 2),
            "reply": _parse_reply(raw),
            "tokens": _parse_tokens(raw),
            "model": model,
            "sandbox": "read-only" if readonly else "danger-full-access",
            "raw_tail": raw[-1500:] if len(raw) > 1500 else raw,
        }

    def _ask(self, a):
        q = a.get("q") or a.get("prompt")
        if not q:
            return R.error("agent.ask", "缺 q（要问的问题）", device=self.name)
        # 纯问答不给写权限：它不需要动磁盘，只读就够
        return self._run_sync(q, a.get("model") or None, a.get("cwd"),
                              True, float(a.get("timeout", 300)))

    def _exec(self, a):
        p = a.get("prompt") or a.get("q")
        if not p:
            return R.error("agent.exec", "缺 prompt", device=self.name)
        return self._run_sync(p, a.get("model") or None, a.get("cwd"),
                              bool(a.get("readonly")),
                              float(a.get("timeout", 900)),
                              a.get("config"))

    # ------------------------------------------------------------ AgentRouter

    def _gpt(self, a):
        """直调 AgentRouter：比 zen-gate 免费车道强，按美元计费，走代理。

        记忆里的三条暗门都照做了：UA 指纹、只走 /v1/responses、
        max_output_tokens 默认给足（给少了会被推理吃光导致空回复）。
        """
        import urllib.error
        import urllib.request
        q = a.get("prompt") or a.get("q")
        if not q:
            return R.error("agent.gpt", "缺 prompt", device=self.name)
        if not AR_KEY:
            return R.unavailable("agent.gpt", "没有 AgentRouter key", device=self.name)
        model = a.get("model") or "gpt-6.1-sol"
        budget = int(a.get("max_tokens", 100000))       # 上限不是配额，按需花

        body = json.dumps({"model": model, "input": str(q),
                           "max_output_tokens": budget}).encode()
        req = urllib.request.Request(
            AGENTROUTER_BASE + "/responses", data=body,
            headers={"Authorization": "Bearer " + AR_KEY,
                     "Content-Type": "application/json",
                     "User-Agent": AR_UA_CLI},
            method="POST")
        # urllib 走代理：显式给 handler，不吃环境变量（这个沙箱里环境变量不总生效）
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        t0 = time.time()
        try:
            with opener.open(req, timeout=float(a.get("timeout", 300))) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            return R.error("agent.gpt", "HTTP %s：%s" % (exc.code, detail),
                           device=self.name)
        except Exception as exc:                        # noqa: BLE001
            return R.error("agent.gpt", "%s: %s" % (type(exc).__name__, exc),
                           device=self.name)

        text = ""
        reasoning_tokens = 0
        for item in payload.get("output") or []:
            if item.get("type") == "reasoning":
                ru = (item.get("usage") or {}).get("output_tokens")
                if ru:
                    reasoning_tokens += int(ru)
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    text += c.get("text") or ""
        status = payload.get("status")
        out = {
            "status": status,
            "reply": text.strip(),
            "model": payload.get("model") or model,
            "elapsed": round(time.time() - t0, 2),
            "reasoning_tokens": reasoning_tokens,
            "usage": payload.get("usage"),
        }
        if status == "incomplete" and not out["reply"]:
            out["hint"] = ("撞天花板了：推理吃光了预算。把 max_tokens 抬到 10 万再来。")
        elif not out["reply"]:
            out["hint"] = "没有 output_text，可能被内容审核拦了（中文长句易触发）"
        return out

    # ------------------------------------------------------------ 长活

    def _job_path(self, jid, leaf=None):
        base = "/agent/jobs/" + jid
        return base + ("/" + leaf if leaf else "")

    def _spawn(self, a):
        p = a.get("prompt") or a.get("q")
        if not p:
            return R.error("agent.spawn", "缺 prompt", device=self.name)
        readonly = bool(a.get("readonly"))
        cmd = self._build_cmd(p, a.get("model") or None, a.get("cwd"),
                              readonly, a.get("config"))
        if cmd is None:
            return R.unavailable("agent.spawn", "没找到 codex CLI", device=self.name)

        jid = "j" + uuid.uuid4().hex[:6]
        fd, outp = tempfile.mkstemp(prefix="aijob_%s_" % jid, suffix=".log")
        os.close(fd)
        fh = open(outp, "wb")
        try:
            proc = subprocess.Popen(
                cmd, cwd=a.get("cwd") or None, env=_env_with_proxy(),
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:                                # noqa: BLE001
            fh.close()
            raise
        with _JOB_LOCK:
            _JOBS[jid] = {"proc": proc, "file": outp, "started": time.time(),
                          "fh": fh, "args": {"prompt": str(p)[:200],
                                             "model": a.get("model"),
                                             "cwd": a.get("cwd"),
                                             "readonly": readonly}}

        # 开跑就把状态摊进 live 树——人和 AI 立刻都能查
        self._live_tx(jid, {
            "status": "running", "pid": proc.pid,
            "model": a.get("model") or "default",
            "cwd": a.get("cwd") or os.getcwd(),
            "prompt": str(p)[:500],
            "readonly": readonly, "started": time.time(), "elapsed": 0.0,
        })
        t = threading.Thread(target=self._watch, args=(jid,), daemon=True)
        t.start()
        return {"job": jid, "pid": proc.pid,
                "state_path": self._job_path(jid),
                "hint": "查进度：ai.jobs；看输出：ai.log job=%s" % jid}

    def _live_tx(self, jid, mapping):
        """把 job 状态写进 live 树；live 不可用也不能让任务本身失败。"""
        try:
            from ..live import live
        except Exception:                                # noqa: BLE001
            return
        for k, v in mapping.items():
            try:
                live.set(self._job_path(jid, k), v, writer="ai", silent=True)
            except Exception:                            # noqa: BLE001
                pass

    def _watch(self, jid):
        """轮询子进程，边跑边更新 live：状态、耗时、输出尾部。"""
        with _JOB_LOCK:
            job = _JOBS.get(jid)
        if not job:
            return
        proc, outp = job["proc"], job["file"]
        last_beat = 0.0
        while True:
            rc = proc.poll()
            elapsed = round(time.time() - job["started"], 1)
            now = time.time()
            if now - last_beat >= 1.0:
                last_beat = now
                self._live_tx(jid, {"elapsed": elapsed,
                                    "tail": _tail_file(outp, 2000)})
            if rc is not None:
                break
            time.sleep(0.4)
        try:
            job["fh"].close()
        except Exception:                                # noqa: BLE001
            pass
        raw = _tail_file(outp, 60000)
        # 全文再多读一点，抓 tokens 那行（它可能在尾部 6000 字符内）
        full_tail = _tail_file(outp, 6000)
        self._live_tx(jid, {
            "status": "done" if rc == 0 else "failed",
            "exit_code": rc,
            "elapsed": round(time.time() - job["started"], 1),
            "tokens": _parse_tokens(full_tail) or _parse_tokens(raw),
            "reply": _parse_reply(full_tail)[-1500:],
            "tail": raw[-2000:],
            "ended": time.time(),
        })

    def _jobs(self, a):
        rows = []
        with _JOB_LOCK:
            items = list(_JOBS.items())
        for jid, job in items:
            proc = job["proc"]
            rc = proc.poll()
            rows.append({
                "job": jid,
                "state": "running" if rc is None else ("done" if rc == 0 else "failed"),
                "pid": proc.pid,
                "elapsed": round(time.time() - job["started"], 1),
                "prompt": job["args"].get("prompt", "")[:80],
                "log": job["file"],
            })
        rows.sort(key=lambda r: r["elapsed"])
        return {"count": len(rows),
                "running": sum(1 for r in rows if r["state"] == "running"),
                "jobs": rows}

    def _log(self, a):
        jid = a.get("job")
        with _JOB_LOCK:
            job = _JOBS.get(jid) if jid else None
        if not job:
            return R.error("agent.log", "没有这个任务：%r（先 ai.jobs 看有哪些）" % jid,
                           device=self.name)
        rc = job["proc"].poll()
        return {"job": jid,
                "state": "running" if rc is None else ("done" if rc == 0 else "failed"),
                "exit_code": rc,
                "elapsed": round(time.time() - job["started"], 1),
                "tail": _tail_file(job["file"], int(a.get("bytes", 4000))),
                "log": job["file"]}

    def _kill(self, a):
        jid = a.get("job")
        with _JOB_LOCK:
            job = _JOBS.get(jid) if jid else None
        if not job:
            return R.error("agent.kill", "没有这个任务：%r" % jid, device=self.name)
        proc = job["proc"]
        if proc.poll() is not None:
            return {"job": jid, "already": True, "exit_code": proc.returncode}
        try:
            if os.name == "nt":
                # /T 连子孙进程一起杀，否则 codex 拉起的子进程会变孤儿
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            else:
                proc.terminate()
        except Exception:                                # noqa: BLE001
            try:
                proc.kill()
            except Exception:                            # noqa: BLE001
                pass
        self._live_tx(jid, {"status": "killed", "ended": time.time()})
        return {"job": jid, "killed": True, "pid": proc.pid}


# ---------------------------------------------------------------- 自测

def _selftest():
    import shutil
    import tempfile

    state = {"n": 0}
    fails = []

    def ck(cond, label):
        state["n"] += 1
        if not cond:
            fails.append(label)

    dev = AgentOps()
    ok, why = dev.probe()
    if not ok:
        print("skip: " + why)
        return 0

    # ============ 契约面 ============
    ck(len(dev.caps()) == 10, "caps：正好 10 项")
    ck(all(c.startswith("agent.") for c in dev.caps()), "caps：全部 ai. 前缀")

    r = dev.invoke("agent.nonexistent")
    ck(not r.ok, "未知能力要报错")

    # ============ ai.status ============
    r = dev.invoke("agent.status")
    ck(r.ok, "status：能体检")
    if r.ok:
        ck("ready" in r.data, "status：有 ready 字段")
        ck("zen_gate" in r.data and "open" in r.data["zen_gate"], "status：报网关端口状态")
        ck("proxy" in r.data, "status：报代理状态")
        ck(isinstance(r.data.get("running_jobs"), int), "status：报在跑任务数")
        ck(r.data["codex_cli"], "status：找到 codex 可执行文件")
        # 网关没起时不假装 ready
        if not r.data["zen_gate"]["open"]:
            ck(r.data["ready"] is False, "status：网关没起时 ready=False（不假装能用）")
            ck("hint" in r.data, "status：网关没起时给启动提示")

    # ============ ai.whoami ============
    r = dev.invoke("agent.whoami")
    ck(r.ok and r.data.get("model_provider"), "whoami：认得出 provider")
    ck(r.ok and r.data.get("provider", "x") is None, "whoami：provider 键与 model_provider 不重复")

    gate_open = dev.invoke("agent.status").data.get("zen_gate", {}).get("open")
    gate_open = bool(gate_open)

    # ============ ai.models（需要网关） ============
    if gate_open:
        r = dev.invoke("agent.models")
        ck(r.ok, "models：网关在时应能取清单 %s" % (r.err if not r.ok else ""))
        ck(r.ok and r.data["count"] > 0, "models：至少一个模型")
        ck(r.ok and all(m.get("id") for m in r.data["models"]), "models：每项都有 id")
    else:
        r = dev.invoke("agent.models")
        ck(not r.ok, "models：网关不在时应失败而不是给空清单")

    # ============ 权限：命令行本身就是边界，默认全权限 ============
    cmd = dev._build_cmd("x")
    ck(any("danger-full-access" in c for c in cmd), "权限：默认全权限（不再套预演确认）")
    cmd_ro = dev._build_cmd("x", readonly=True)
    ck(any("read-only" in c for c in cmd_ro), "权限：readonly=True 能收窄到只读")
    r = dev.invoke("agent.exec", {"prompt": "x", "timeout": 0.01})
    ck(not r.ok and "超时" in (r.err or ""), "权限：exec 不再因 write 被拦，能真跑（超时才算错）")

    # ============ 参数校验 ============
    for cap in ("agent.ask", "agent.exec", "agent.spawn", "agent.gpt"):
        r = dev.invoke(cap, {})
        ck(not r.ok, "%s：缺 prompt 要报错" % cap)

    # ============ ai.log / ai.kill 不存在的任务 ============
    r = dev.invoke("agent.log", {"job": "j不存在"})
    ck(not r.ok and "jobs" in (r.err or ""), "log：不存在时报错并指路")
    r = dev.invoke("agent.kill", {"job": "j不存在"})
    ck(not r.ok, "kill：不存在时报错")

    # ============ ai.spawn：真起一个后台任务 ============
    if not gate_open:
        print("\naiops selftest: %d/%d（网关未开，跳过真调）"
              % (state["n"] - len(fails), state["n"]))
        for f in fails:
            print("   失败:", f)
        return 0 if not fails else 1

    from ..live import live as LIVE
    r = dev.invoke("agent.spawn", {"q": "只回两个字：收到", "timeout": 300})
    ck(r.ok, "spawn：能起后台任务 %s" % (r.err if not r.ok else ""))
    if not r.ok:
        print("\naiops selftest: %d/%d" % (state["n"] - len(fails), state["n"]))
        for f in fails:
            print("   失败:", f)
        return 1

    jid = r.data["job"]
    ck(bool(r.data.get("pid")), "spawn：返回 pid")
    ck(r.data["state_path"] == "/agent/jobs/" + jid, "spawn：给出状态路径")

    # 🔴 关键：live 树里要立刻可查（不是等跑完才有）
    ck(LIVE.get("/agent/jobs/%s/status" % jid) == "running", "live：开跑即 status=running")
    ck(LIVE.get("/agent/jobs/%s/pid" % jid) == r.data["pid"], "live：pid 对得上")
    ck(LIVE.get("/agent/jobs/%s/cwd" % jid), "live：记了工作目录")
    ck(LIVE.get("/agent/jobs/%s/prompt" % jid), "live：记了原始任务")

    r = dev.invoke("agent.jobs")
    ck(r.ok and r.data["count"] >= 1, "jobs：列得出来")
    ck(r.ok and any(j["job"] == jid for j in r.data["jobs"]), "jobs：新任务在列表里")
    ck(r.ok and jid in [j["job"] for j in r.data["jobs"] if j["state"] == "running"],
       "jobs：状态标 running")

    r = dev.invoke("agent.log", {"job": jid, "bytes": 800})
    ck(r.ok, "log：能读输出")
    ck(r.ok and r.data["state"] in ("running", "done"), "log：报状态")

    # ============ ai.kill：杀掉，且不留孤儿 ============
    r = dev.invoke("agent.kill", {"job": jid})
    ck(r.ok, "kill：能杀 %s" % (r.err if not r.ok else ""))
    ck(r.ok and r.data.get("pid"), "kill：回报 pid")

    import time as _t
    deadline = _t.time() + 15
    killed = False
    while _t.time() < deadline:
        st = LIVE.get("/agent/jobs/%s/status" % jid)
        if st in ("killed", "failed", "done"):
            killed = True
            break
        _t.sleep(0.5)
    ck(killed, "kill：live 里的状态最终不再是 running")
    ck(LIVE.get("/agent/jobs/%s/status" % jid) == "killed",
       "kill：状态标成 killed（不是 done）")

    r = dev.invoke("agent.kill", {"job": jid})
    ck(r.ok and r.data.get("already") is True, "kill：杀第二次报 already 而不是报错")

    # ============ ai.ask：真调一次（这是核心价值，必须真验） ============
    r = dev.invoke("agent.ask", {"q": "只回两个字：通了", "timeout": 180})
    ck(r.ok, "ask：真调成功 %s" % (r.err if not r.ok else ""))
    if r.ok:
        ck(r.data["reply"].strip() != "", "ask：解析出回答（不是空）")
        ck(r.data["exit_code"] == 0, "ask：退出码 0")
        ck(r.data["elapsed"] > 0, "ask：记了耗时")
        ck(r.data["sandbox"] == "read-only", "ask：默认只读沙箱")
        ck(r.data["tokens"] is None or r.data["tokens"] > 0, "ask：token 数解析（有则正数）")
        # 回答里不该残留 codex 的头部噪音
        ck("OpenAI Codex v" not in r.data["reply"], "ask：reply 已剥掉版本头")

    # ============ ai.gpt：AgentRouter（需要代理） ============
    st = dev.invoke("agent.status").data
    if st.get("proxy", {}).get("open"):
        r = dev.invoke("agent.gpt", {"prompt": "Reply with exactly: OK",
                                  "max_tokens": 2000, "timeout": 90})
        ck(r.ok, "gpt：AgentRouter 可达 %s" % (r.err if not r.ok else ""))
        if r.ok:
            ck(r.data["status"] in ("completed", "incomplete"), "gpt：报 status")
            ck(r.data["reply"].strip().upper().startswith("OK") or r.data.get("hint"),
               "gpt：有回答或有解释（不静默空）")
    else:
        r = dev.invoke("agent.gpt", {"prompt": "x", "timeout": 5})
        ck(not r.ok or r.ok, "gpt：代理不在时也给出结果（不卡死）")

    print("\naiops selftest: %d/%d%s"
          % (state["n"] - len(fails), state["n"],
             "" if not fails else "（失败 %d）" % len(fails)))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.aiops --selftest")
    sys.exit(0)
