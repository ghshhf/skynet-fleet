#!/usr/bin/env python3
"""手机节点运行时（fleet-node / phone）。

设计前提：**手机是一等节点，不是谁的配件。**
  · 进程跑在手机本地的 Debian 沙箱里（proot-rootfs），不依赖任何外部主机；
  · 自己监听端口、自己访问外网、自己执行任务；
  · 电脑只是它的一个客户端（可选），不是它的运行前提。

能力分三类：
  · local.*   沙箱内执行（shell / 文件 / 进程）
  · termux.*  手机系统能力（相机 / 传感 / 短信 / 通知 / 剪贴板 / TTS …），
              走 Termux:API，不需要 adb、不需要 root
  · llm.*     转发手机本地 llama-server（默认 127.0.0.1:8080）

刻意零第三方依赖：只用标准库。容器里 `apt install python3` 就够跑。

注：下面这些是**节点本地协议名**（`shell.exec` / `fs.read` / `llm.chat`…），
与控制面 `fleet/catalog.py` 里的域名前缀（`phone.*`）不是同一套命名。
映射由调用方负责，节点这边不跟着 catalog 改名——否则每加一条能力都要动两端。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
NODE_ID = os.environ.get("FLEET_NODE_ID", "phone")
TOKEN = os.environ.get("FLEET_TOKEN", "")
PORT = int(os.environ.get("FLEET_PORT", "8770"))
BIND = os.environ.get("FLEET_BIND", "0.0.0.0")
# 手机上有两个推理服务（由 start-node.sh 拉起）：
#   8080 = fast（Qwen3-0.6B-Q8_0，约 42 tok/s，适合打标/短答）
#   8082 = acc （Qwen3-1.7B-Q4_K_M，约 17 tok/s，质量优先）
# 默认走 fast，需要更稳的判断时显式传 tier="acc"。
LLM_FAST = os.environ.get("FLEET_LLM_FAST", "http://127.0.0.1:8080")
LLM_ACC = os.environ.get("FLEET_LLM_ACC", "http://127.0.0.1:8082")
LLM_URL = LLM_FAST  # 兼容旧调用
STARTED = time.time()

# 沙箱边界：这些路径对外可见（其余一律拒绝，防止被当成越狱跳板）
SANDBOX_ROOT = os.environ.get("FLEET_SANDBOX", "/root/work")


# ---------------------------------------------------------------- 工具

def run(argv, timeout=60, cwd=None, env=None):
    """跑一个进程，异常一律转成结构化失败，不往外抛。"""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, env=env)
        return {"rc": p.returncode, "out": p.stdout[-16000:], "err": p.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"rc": -9, "out": "", "err": "timeout after %ss" % timeout}
    except FileNotFoundError as e:
        return {"rc": -2, "out": "", "err": "not found: %s" % e}
    except Exception as e:  # noqa: BLE001
        return {"rc": -1, "out": "", "err": "%s: %s" % (type(e).__name__, e)}


def sh(cmd, timeout=60, cwd=None):
    return run(["/bin/sh", "-c", cmd], timeout=timeout, cwd=cwd)


def have(binary):
    return shutil.which(binary) is not None


# ---------------------------------------------------------------- 能力

def cap_sys_info(args):
    """这台手机自己是什么、在哪、还剩多少。"""
    info = {
        "node_id": NODE_ID,
        "version": VERSION,
        "uptime_s": round(time.time() - STARTED, 1),
        "python": sys.version.split()[0],
        "hostname": run(["hostname"]).get("out", "").strip(),
        "kernel": run(["uname", "-r"]).get("out", "").strip(),
        "distro": "",
        "cpu": run(["nproc"]).get("out", "").strip(),
        "mem": "",
        "disk": "",
        "addrs": [],
    }
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["distro"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    info["mem"] = line.split()[1] + " kB"
                    break
    except OSError:
        pass
    d = sh("df -h / | tail -1")
    if d["rc"] == 0:
        info["disk"] = " ".join(d["out"].split()[1:4])
    # 容器最小镜像没有 iproute2，所以三条路依次退：hostname -I → /proc/net/fib_trie → 空
    ip = sh("hostname -I 2>/dev/null")
    if ip["rc"] != 0 or not ip["out"].strip():
        ip = sh("ip -4 addr show 2>/dev/null | grep 'inet ' | awk '{print $2}'")
    if ip["out"].strip():
        info["addrs"] = [x for x in ip["out"].split() if x and "." in x]
    return info


def cap_shell_exec(args):
    """在沙箱里执行一条 shell 命令。这是「手机自己能动手」的最底层。"""
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return {"error": "cmd 不能为空"}
    timeout = min(int(args.get("timeout") or 60), 600)
    cwd = args.get("cwd") or SANDBOX_ROOT
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
    r = sh(cmd, timeout=timeout, cwd=cwd)
    r["ok"] = r["rc"] == 0
    return r


def safe_path(path):
    """把路径钉在沙箱根内。越界一律拒绝——手机 Agent 的可写面必须收窄，
    否则一条 fs.write 就能改到容器外面去（那这道沙箱就白建了）。"""
    if not path:
        raise ValueError("path 不能为空")
    p = os.path.realpath(os.path.join(SANDBOX_ROOT, path)
                         if not os.path.isabs(path) else path)
    root = os.path.realpath(SANDBOX_ROOT)
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("路径越出沙箱: %s（沙箱根 %s）" % (p, root))
    return p


def cap_fs_read(args):
    limit = min(int(args.get("limit") or 65536), 1 << 20)
    try:
        path = safe_path(args.get("path") or "")
        with open(path, "rb") as f:
            b = f.read(limit)
        return {"ok": True, "path": path, "size": len(b),
                "text": b.decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def cap_fs_write(args):
    text = args.get("text") or ""
    try:
        path = safe_path(args.get("path") or "")
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"ok": True, "path": path, "bytes": len(text.encode())}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def cap_fs_list(args):
    try:
        path = safe_path(args.get("path") or "")
        items = []
        for name in sorted(os.listdir(path))[:400]:
            p = os.path.join(path, name)
            try:
                st = os.stat(p)
                items.append({"name": name, "dir": os.path.isdir(p),
                              "size": st.st_size, "mtime": int(st.st_mtime)})
            except OSError:
                items.append({"name": name, "dir": False, "size": -1, "mtime": 0})
        return {"ok": True, "path": path, "items": items}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def cap_sandbox_info(args):
    """沙箱是什么、边界在哪、里面有什么。让「隔离」这件事可被查证，而不是靠嘴说。"""
    root = os.path.realpath(SANDBOX_ROOT)
    du = sh("du -sh %s 2>/dev/null" % root)
    return {
        "ok": True,
        "root": root,
        "size": (du.get("out") or "").split()[0] if du.get("out") else "",
        "writable_scope": "fs.* 只能在这个根内；shell.exec 在本根内执行",
        "isolation": "proot 用户态 chroot：独立 Debian rootfs，共享 Android 内核",
        "limits": "非内核级隔离，不防恶意代码越权；需要强隔离应上 AVF/虚拟机",
        "os": (open("/etc/os-release").read().split("PRETTY_NAME=")[1].split("\n")[0].strip('"')
               if os.path.exists("/etc/os-release") else ""),
    }


# Termux:API —— 手机系统能力的入口。容器 PATH 里带着 Termux 的 bin，
# 所以这些命令直接可用；缺哪个就报哪个，不假装成功。
TERMUX_CAPS = {
    "termux.battery": ["termux-battery-status"],
    "termux.camera": ["termux-camera-photo"],
    "termux.sensor": ["termux-sensor"],
    "termux.location": ["termux-location"],
    "termux.notification": ["termux-notification"],
    "termux.clipboard.get": ["termux-clipboard-get"],
    "termux.clipboard.set": ["termux-clipboard-set"],
    "termux.toast": ["termux-toast"],
    "termux.tts": ["termux-tts-speak"],
    "termux.vibrate": ["termux-vibrate"],
    "termux.torch": ["termux-torch"],
    "termux.wifi": ["termux-wifi-connectioninfo"],
    "termux.wifi.scan": ["termux-wifi-scaninfo"],
    "termux.cell": ["termux-telephony-cellinfo"],
    "termux.sms.list": ["termux-sms-list"],
    "termux.sms.send": ["termux-sms-send"],
    "termux.calllog": ["termux-call-log"],
    "termux.volume": ["termux-volume"],
    "termux.brightness": ["termux-brightness"],
    "termux.ir": ["termux-infrared-transmit"],
    "termux.info": ["termux-info"],
}


def cap_termux(cap, args):
    argv = list(TERMUX_CAPS[cap])
    if cap == "termux.camera":
        argv += ["-c", str(args.get("camera", 0)), args.get("out", "/root/work/cam.jpg")]
    elif cap == "termux.sensor":
        argv += ["-s", args.get("sensor", "accelerometer"), "-n", str(args.get("count", 1))]
    elif cap == "termux.notification":
        if args.get("title"):
            argv += ["--title", str(args["title"])]
        if args.get("content"):
            argv += ["--content", str(args["content"])]
    elif cap == "termux.clipboard.set":
        argv += [str(args.get("text", ""))]
    elif cap == "termux.toast":
        argv += [str(args.get("text", ""))]
    elif cap == "termux.tts":
        argv += [str(args.get("text", ""))]
    elif cap == "termux.torch":
        argv += ["on" if args.get("on") else "off"]
    elif cap == "termux.sms.send":
        argv += ["-n", str(args.get("number", "")), str(args.get("text", ""))]
    elif cap == "termux.brightness":
        argv += [str(args.get("value", 128))]
    elif cap == "termux.volume":
        if not args.get("stream"):
            return {"ok": True, "rc": 0, "out": sh("termux-volume")["out"], "err": ""}
        argv += ["-s", str(args["stream"]), str(args.get("volume", 5))]
    elif cap == "termux.ir":
        argv += [str(args.get("freq", "")), json.dumps(args.get("pattern") or [])]
    elif cap == "termux.location":
        argv += ["-p", args.get("provider", "network"), "-r", args.get("request", "once")]
    r = run(argv, timeout=int(args.get("timeout") or 30))
    r["ok"] = r["rc"] == 0
    return r


def cap_llm_chat(args):
    """转发到手机本地的 llama-server —— 推理在手机上完成，不经过任何外部主机。

    两个坑（都实测踩过）：
      · Qwen3 是思考模型，默认把整段输出塞进 reasoning_content，content 是空串。
        只取 content 会得到「ok=true 但答案是空」的假成功，还白烧 token。
      · max_tokens 太小时思考还没结束就被截断，同样只剩空 content。
    所以这里给 think 开关，并且拿不到 content 就把 reasoning 一并回传。
    """
    import urllib.request
    payload = {
        "messages": args.get("messages") or [{"role": "user",
                                              "content": str(args.get("prompt", ""))}],
        "temperature": args.get("temperature", 0.7),
        "max_tokens": args.get("max_tokens", 512),
        "stream": False,
    }
    if args.get("think") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    base = LLM_ACC if str(args.get("tier", "")).lower() in ("acc", "accurate", "slow") else LLM_FAST
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.get("timeout", 120)) as r:
            d = json.loads(r.read().decode())
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        out = {"ok": True, "text": text, "reasoning": reasoning, "tier": base,
               "usage": d.get("usage")}
        if not text:
            # 明确报「为什么空」，不拿空串冒充成功
            out["ok"] = False
            out["error"] = ("模型只产出了思考内容、没给出正文（max_tokens=%s 可能太小，"
                            "或该模型需要 think=false）" % payload["max_tokens"])
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def cap_llm_models(args):
    """把手机上的推理服务都列出来，标清哪台是 fast、哪台是 acc。"""
    import urllib.request
    out = {}
    for tier, base in (("fast", LLM_FAST), ("acc", LLM_ACC)):
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/v1/models", timeout=10) as r:
                d = json.loads(r.read().decode())
            ids = [m.get("id") or m.get("model") for m in (d.get("data") or d.get("models") or [])]
            out[tier] = {"url": base, "up": True, "models": ids}
        except Exception as e:  # noqa: BLE001
            out[tier] = {"url": base, "up": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "tiers": out}


HANDLERS = {
    "sys.info": cap_sys_info,
    "sandbox.info": cap_sandbox_info,
    "shell.exec": cap_shell_exec,
    "fs.read": cap_fs_read,
    "fs.write": cap_fs_write,
    "fs.list": cap_fs_list,
    "llm.chat": cap_llm_chat,
    "llm.models": cap_llm_models,
}


def cap_list():
    names = sorted(HANDLERS)
    names += sorted(TERMUX_CAPS)
    return names


def dispatch(cap, args):
    if cap in HANDLERS:
        return HANDLERS[cap](args)
    if cap in TERMUX_CAPS:
        return cap_termux(cap, args)
    return {"error": "未知能力: %s" % cap, "caps": cap_list()}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-node/" + VERSION

    def log_message(self, fmt, *a):  # 静音；日志走事件流，不打到 stdout
        return

    def _auth(self):
        if not TOKEN:
            return True
        got = self.headers.get("X-Fleet-Token", "")
        if got == TOKEN:
            return True
        self._json({"ok": False, "error": "unauthorized"}, 401)
        return False

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._json({"ok": True, "node": NODE_ID, "version": VERSION,
                               "uptime_s": round(time.time() - STARTED, 1)})
        if not self._auth():
            return None
        if self.path == "/caps":
            return self._json({"ok": True, "node": NODE_ID, "caps": cap_list()})
        if self.path == "/info":
            return self._json({"ok": True, "info": cap_sys_info({})})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8 << 20:
            return self._json({"ok": False, "error": "payload too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": "bad json: %s" % e}, 400)
        cap = body.get("cap") or ""
        args = body.get("args") or {}
        t0 = time.time()
        try:
            out = dispatch(cap, args)
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        out = dict(out or {})
        out.setdefault("ok", "error" not in out)
        out["cap"] = cap
        out["node"] = NODE_ID
        out["took_ms"] = round((time.time() - t0) * 1000)
        self._json(out)


def main(argv=None):
    argv = argv or sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    os.makedirs(SANDBOX_ROOT, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print("fleet-node %s listening on %s:%s (sandbox=%s, llm=%s)"
          % (VERSION, BIND, PORT, SANDBOX_ROOT, LLM_URL), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def selftest():
    fails, checks = [], 0

    def ck(cond, what):
        nonlocal checks
        checks += 1
        if not cond:
            fails.append(what)

    r = cap_sys_info({})
    ck(r.get("distro", "").startswith("Debian"), "sys.info 拿不到发行版：%r" % r.get("distro"))
    ck(r.get("python"), "sys.info 没有 python 版本")

    r = cap_shell_exec({"cmd": "echo hi"})
    ck(r.get("ok") and "hi" in r.get("out", ""), "shell.exec 跑不通：%r" % r)
    r = cap_shell_exec({"cmd": "exit 3"})
    ck(r.get("rc") == 3, "shell.exec 没把退出码透出来：%r" % r.get("rc"))
    r = cap_shell_exec({"cmd": "sleep 5", "timeout": 1})
    ck(r.get("rc") == -9, "shell.exec 超时没被拦住：%r" % r)

    r = cap_fs_write({"path": SANDBOX_ROOT + "/_st.txt", "text": "abc"})
    ck(r.get("ok"), "fs.write 失败：%r" % r)
    r = cap_fs_read({"path": SANDBOX_ROOT + "/_st.txt"})
    ck(r.get("text") == "abc", "fs.read 读不回：%r" % r)
    r = cap_fs_read({"path": "/nonexistent/xx"})
    ck(not r.get("ok"), "fs.read 对不存在的文件没报错")

    # 沙箱边界：这条必须守住，破了整道隔离就是摆设
    r = cap_fs_read({"path": "/etc/passwd"})
    ck(not r.get("ok"), "fs.read 越出沙箱根却成功了（%r）" % r.get("path"))
    r = cap_fs_write({"path": "/tmp/evil.txt", "text": "x"})
    ck(not r.get("ok"), "fs.write 越出沙箱根却成功了（%r）" % r.get("path"))
    r = cap_fs_read({"path": "../../etc/shadow"})
    ck(not r.get("ok"), "fs.read 用 ../ 穿越出沙箱却成功了")

    r = dispatch("不存在的能力", {})
    ck("error" in r, "未知能力没报错")

    # 能力表必须每条都能被 dispatch 认领（防「列了但调不了」）
    for name in cap_list():
        if name in HANDLERS or name in TERMUX_CAPS:
            continue
        fails.append("能力 %s 没有归口" % name)

    print("selftest: %d 项检查，%d 项失败" % (checks, len(fails)))
    for f in fails:
        print("  ✗ " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
