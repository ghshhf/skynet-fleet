"""P3（组网）+ P4（单声道）门禁。

判据来源：docs/第一版正式版_改动方案_逐条_2026-10-22.md 的两张表。
原则跟 P1 门禁一样：**每条都要能指向具体证据**，不写「看起来没问题」。

跑法：
    uv run --python 3.12 --no-project --with-requirements requirements.txt \
        python tools/p3_gate.py            # 1/2/3/4/5/8
    ... python tools/p3_gate.py --live     # 再加 6/7（会真的打网络）
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULTS = []


def check(no, what, fn, timeout=45):
    """带超时跑一项。

    为什么要超时：--lan 会把服务绑到 0.0.0.0，Windows 会崩一个防火墙授权弹窗，
    没人点就卡死在那儿。门禁不能因为一个弹窗就永远不结束——卡住就是这项没通过。
    """
    box = {}

    def run():
        try:
            box["ok"], box["ev"] = fn()
        except Exception as e:
            box["ok"], box["ev"] = False, "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        ok, evidence = False, "超过 %d 秒没返回：大概率卡在绑定 0.0.0.0（Windows 防火墙弹窗）或网络" % timeout
    else:
        ok, evidence = box.get("ok", False), box.get("ev", "(无输出)")
    RESULTS.append({"no": no, "what": what, "ok": bool(ok), "evidence": evidence})
    print(("PASS " if ok else "FAIL ") + "#%-2s %s" % (no, what))
    for line in str(evidence).splitlines():
        print("        " + line)
    return ok


def _health(srv, token):
    req = urllib.request.Request(srv.base_url + "/api/health")
    if token is not None:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


# ---------------------------------------------------------------- #1
def t1_lan_no_token():
    """--lan 且不设令牌 → 必须拒绝启动（不是警告）。"""
    from fleet.gui import app
    os.environ.pop("FLEET_GUI_TOKEN", None)
    try:
        app.main(["--lan", "--no-window"])
    except SystemExit as e:
        msg = str(e)
        ok = "必须固定令牌" in msg
        return ok, "SystemExit：%s" % msg.splitlines()[0]
    return False, "没有拒绝启动——这是安全漏洞（0.0.0.0 + 内存随机令牌，手机拿不到）"


# ---------------------------------------------------------------- #2
def t2_lan_with_token():
    """--lan + 固定令牌 → 绑 0.0.0.0，但对外 URL 仍是 127.0.0.1；鉴权 200/401。"""
    from fleet.gui.approval import ApprovalBroker
    from fleet.gui.server import FleetServer
    os.environ["FLEET_GUI_TOKEN"] = "gate-token-0123456789"
    srv = FleetServer(port=0, host="0.0.0.0", approval=ApprovalBroker())
    srv.start()
    try:
        port = srv.port
        ok_code, data = _health(srv, srv.token)
        bad_code, _ = _health(srv, None)
        ev = ["绑定 %s:%d" % (srv.host, port),
              "base_url=%s" % srv.base_url,
              "带令牌 /api/health → %s ok=%s" % (ok_code, (data or {}).get("ok")),
              "不带令牌 /api/health → %s" % bad_code]
        ok = (srv.host == "0.0.0.0" and srv.base_url.startswith("http://127.0.0.1:")
              and ok_code == 200 and bad_code == 401)
        # 令牌固定下来了
        ok = ok and srv.token == "gate-token-0123456789"
        return ok, "\n".join(ev)
    finally:
        srv.stop()
        os.environ.pop("FLEET_GUI_TOKEN", None)


# ---------------------------------------------------------------- #3
def t3_link_no_device():
    """--link 但没设备 → 不崩，明确说「没找到在线设备」。"""
    from fleet.gui import link
    real = link.devices
    link.devices = lambda: []
    try:
        r = link.ensure_reverse(8000)
        buf = []
        old = sys.stdout
        sys.stdout = type("B", (), {"write": lambda self, s: buf.append(s),
                                    "flush": lambda self: None})()
        try:
            link.report(8000, "tok", r)
        finally:
            sys.stdout = old
        text = "".join(buf)
        ok = r.get("empty") and "没找到在线设备" in text
        return ok, text.strip().splitlines()[0] if text.strip() else "(无输出)"
    finally:
        link.devices = real


# ---------------------------------------------------------------- #2b
def t4_link_real():
    """真机：--link 起服务 → 四行配置打印 → adb reverse --list 里真有这条。"""
    from fleet.gui import link
    try:
        devs = link.devices()
    except RuntimeError as e:
        return False, "没有在线设备：%s" % e
    if not devs:
        return False, "没有在线设备（adb devices 为空）"

    port = 8000
    env = dict(os.environ)
    env.pop("FLEET_GUI_TOKEN", None)
    p = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); "
         "from fleet.gui.app import main; main(['--link','--no-window','--link-port','%d'])"
         % (ROOT, port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    got = []

    def reader():
        # readline 会一直阻塞到冒出一行。放到单独的线程里读，主线程才能超时收场。
        while True:
            line = p.stdout.readline()
            if not line:
                return
            got.append(line.decode("utf-8", "replace").rstrip())

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            if len([g for g in got if g.startswith("[fleet-link]")]) >= 6:
                break
            time.sleep(0.2)
        text = "\n".join(got)
        rc, listing = 0, ""
        try:
            out = subprocess.run(["adb", "-s", devs[0], "reverse", "--list"],
                                 capture_output=True, timeout=10)
            listing = (out.stdout + out.stderr).decode("utf-8", "replace")
        except Exception as e:
            listing = "读 reverse --list 失败: %s" % e
        has4 = all(k in text for k in
                   ("隧道端口", "手机端「连接设置」填", "地址 http://127.0.0.1:%d" % port, "令牌"))
        has_rev = bool(re.search(r"tcp:%d\s+tcp:%d" % (port, port), listing))
        ev = ["设备 %s" % devs[0], "--- 服务输出 ---"] + got + [
            "--- adb reverse --list ---", listing.strip() or "(空)"]
        return (has4 and has_rev), "\n".join(ev)
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


# ---------------------------------------------------------------- #5
def t5_default_unchanged():
    """不加任何参数 → 仍只绑 127.0.0.1 + 随机端口 + 非固定令牌。"""
    from fleet.gui.approval import ApprovalBroker
    from fleet.gui.server import FleetServer
    os.environ.pop("FLEET_GUI_TOKEN", None)
    a = FleetServer(port=0, approval=ApprovalBroker())
    a.start()
    try:
        b = FleetServer(port=0, approval=ApprovalBroker())
        b.start()
        try:
            ok = (a.host == "127.0.0.1" and a.port != 0 and b.port != a.port
                  and a.token != b.token and a.token != "gate-token-0123456789")
            return ok, "host=%s port=%d token=%s… / %d token=%s… 两次不同" % (
                a.host, a.port, a.token[:8], b.port, b.token[:8])
        finally:
            b.stop()
    finally:
        a.stop()


# ---------------------------------------------------------------- #8
def t8_unknown_backend():
    """不存在的后端名 → 明确报错，且不许静默回全部后端。"""
    from fleet.gui.approval import ApprovalBroker
    from fleet.gui.server import FleetServer, chat_gate
    srv = FleetServer(port=0, approval=ApprovalBroker())
    r = srv.registry.call("ai.chat",
                          args={"prompt": "hi", "backends": "不存在的后端"},
                          gate=chat_gate)
    d = r.as_dict()
    err = str(d.get("err") or "")
    ok = (not d.get("ok")) and ("不存在" in err) and ("现有可用" in err)
    return ok, "ok=%s err=%s" % (d.get("ok"), err)


# ---------------------------------------------------------------- #6/#7
def t_live():
    """真打网络：不传 backends 回多行；传 pollinations 只回 pollinations。"""
    out = {}
    for tag, body in (("all", {"prompt": "用一个词回答：你好"}),
                      ("one", {"prompt": "用一个词回答：你好", "backends": "pollinations"})):
        from fleet.gui.approval import ApprovalBroker
        from fleet.gui.server import FleetServer
        srv = FleetServer(port=0, approval=ApprovalBroker())
        srv.start()
        try:
            req = urllib.request.Request(
                srv.base_url + "/api/chat",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Fleet-Token": srv.token})
            lines, done = [], None
            with urllib.request.urlopen(req, timeout=180) as r:
                for raw in r:
                    s = raw.decode("utf-8", "replace").strip()
                    if not s.startswith("data: "):
                        continue
                    ev = json.loads(s[6:])
                    if ev.get("type") == "line":
                        lines.append(ev.get("text") or "")
                    elif ev.get("type") in ("done", "error"):
                        done = ev
                        break
            out[tag] = (lines, done)
        finally:
            srv.stop()

    all_lines, _ = out["all"]
    one_lines, one_done = out["one"]
    prefixes = sorted({m.group(1) for ln in one_lines
                       for m in [re.match(r"\[([^\]]+)\]", ln)] if m})
    ok_all = len(all_lines) >= 1
    ok_one = bool(prefixes) and prefixes == ["pollinations"]
    return (ok_all and ok_one), "\n".join([
        "全部后端：%d 行；首行=%.60s" % (len(all_lines), all_lines[0] if all_lines else "(空)"),
        "指定 pollinations：%d 行；出现的前缀=%s；done=%s" % (
            len(one_lines), prefixes or "(无)", (one_done or {}).get("type")),
    ])


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    live = "--live" in sys.argv
    check(1, "--lan 无令牌 → 拒绝启动", t1_lan_no_token)
    check(2, "--lan 有令牌 → 绑 0.0.0.0 但 URL 用 127.0.0.1，鉴权 200/401", t2_lan_with_token)
    check(3, "--link 无设备 → 不崩且说清原因", t3_link_no_device)
    check(4, "--link 真机 → 打印四行 + reverse --list 有这条", t4_link_real)
    check(5, "默认启动（无参数）→ 行为与今天一致", t5_default_unchanged)
    check(8, "未知后端名 → 明确报错", t8_unknown_backend)
    if live:
        check(7, "chat 指定单后端 → 只该后端回（含 6 的零回归）", t_live)
    else:
        print("SKIP #6/#7：要真的打网络，加 --live 才跑")

    bad = [r for r in RESULTS if not r["ok"]]
    print("\n合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
