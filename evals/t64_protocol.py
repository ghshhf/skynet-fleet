"""协议层门禁：锁死「造身份 → 注册 → 拿会话 → 复用」这条链路（本地靶场，全离线）。

为什么用本地靶场而不是真站：
  1. 真站注册会留下垃圾账号，门禁不能每天去污染别人；
  2. 门禁要能重复跑，外部站会改版/加验证码/宕机，一变就红等于没有门禁；
  3. 判定要精确到「哪一步没做对」，靶场可以故意设陷阱（隐藏字段、缺字段、无表单）。

覆盖：
  1. 表单解析：action / method / 字段集合 / hidden 的值都要对
  2. 自动认字段：email / password / confirm 认得出来
  3. recon 判「怎么进」：本地靶场必须判成 register，且不许误判有反爬
  4. 【核心】协议注册端到端：填表 → 提交 → 回填码 → 拿到会话 cookie
  5. 会话 save / load：数量一致
  6. 会话检活：带 cookie 打 /me 得 200 且进会员区（行为证据，不是看格式）
  7. 反向证据：不存在的会话不许 probe 成功
  8. 认不出字段时如实报错，不许瞎猜（猜错会在目标站留垃圾账号）
"""
import http.server
import os
import sys
import threading
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
_CODE = "246810"

_FORM_HTML = """<html><head><title>注册 - 靶场</title></head><body>
<h1>创建一个账号</h1>
<form action="/submit" method="post">
  <input type="hidden" name="csrf_token" value="tok123">
  <input type="email" name="email" placeholder="邮箱" required>
  <input type="password" name="password" placeholder="密码" required>
  <input type="password" name="confirm_password" placeholder="再输一次" required>
  <button type="submit">注册</button>
</form></body></html>"""

_VAGUE_HTML = """<html><body>
<form action="/submit" method="post"><input name="a"><input name="b"></form>
</body></html>"""


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def _gate(cap, dev, args, level):
    """有副作用的能力在这条门禁里全放行。

    安全前提：本门禁打的**全是 127.0.0.1 上的本地靶场**，不碰任何真实站点。
    真跑线上时靠 agent_server 的审批闸门，不靠这里。
    """
    return True


class _Target(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/register"):
            self._send(200, _FORM_HTML)
        elif self.path.startswith("/vague"):
            self._send(200, _VAGUE_HTML)
        elif self.path.startswith("/me"):
            if "session=fleet_ok" in (self.headers.get("Cookie") or ""):
                self._send(200, "MEMBER_AREA")
            else:
                self._send(401, "NEED_LOGIN")
        else:
            self._send(404, "NOT_FOUND")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if self.path.startswith("/submit"):
            ok = bool(form.get("email")) and bool(form.get("password"))
            self._send(200 if ok else 400, "验证码已发送" if ok else "缺字段")
        elif self.path.startswith("/verify"):
            if (form.get("code") or [""])[0] == _CODE:
                self._send(200, "注册成功",
                           {"Set-Cookie": "session=fleet_ok; Path=/; HttpOnly"})
            else:
                self._send(400, "验证码错误")
        else:
            self._send(404, "NOT_FOUND")


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    from fleet import catalog
    reg = catalog.build(gate=_gate)

    # ---- 1. 表单解析
    r = reg.call("protocol.form", args={"url": base + "/register"})
    forms = (r.data or {}).get("forms") or []
    check(r.ok and len(forms) == 1, "表单没解析出来（forms=%d, err=%s）" % (len(forms), r.err))
    if forms:
        f = forms[0]
        check(f["action"] == base + "/submit", "action 解析错: %r" % (f["action"],))
        check(f["method"] == "post", "method 解析错: %r" % (f["method"],))
        names = sorted(x["name"] for x in f["fields"])
        check(names == ["confirm_password", "csrf_token", "email", "password"],
              "字段集合错: %r" % (names,))
        hidden = [x for x in f["fields"] if x["type"] == "hidden"]
        check(len(hidden) == 1 and hidden[0]["value"] == "tok123",
              "隐藏字段没取到值: %r" % (hidden,))

    # ---- 2. 自动认字段
    guess = ((r.data or {}).get("guess") or [{}])[0]
    check(guess.get("email_field") == "email", "没认出邮箱字段: %r" % (guess.get("email_field"),))
    check(guess.get("password_field") == "password",
          "没认出密码字段: %r" % (guess.get("password_field"),))

    # ---- 3. recon 判「怎么进」
    r2 = reg.call("protocol.recon", args={"url": base + "/register"})
    d2 = r2.data or {}
    check(r2.ok, "recon 失败: %s" % r2.err)
    check("register" in (d2.get("plan") or []),
          "recon 没判成 register（plan=%r）" % (d2.get("plan"),))
    check((d2.get("antibot") or {}).get("level") == "none",
          "本地靶场被误判有反爬: %r" % (d2.get("antibot"),))

    # ---- 4. 【核心】协议注册端到端
    r3 = reg.call("protocol.register", args={
        "url": base + "/register",
        "verify_url": base + "/verify",
        "code": _CODE,
        "identity": {"email": "gate@example.test", "password": "Pw123456!Aa9"},
    })
    d3 = r3.data or {}
    steps = {s.get("step"): s for s in (d3.get("steps") or [])}
    check(r3.ok, "注册链路失败：%s / 走到 %r" % (r3.err, list(steps)))
    check(steps.get("form", {}).get("email_field") == "email",
          "注册时没认出邮箱字段: %r" % (steps.get("form"),))
    check(steps.get("submit", {}).get("status") == 200,
          "提交没成功（status=%r）" % (steps.get("submit", {}).get("status"),))
    check(steps.get("verify", {}).get("status") == 200,
          "回填验证码没成功（status=%r, snippet=%r）"
          % (steps.get("verify", {}).get("status"), (steps.get("verify") or {}).get("snippet")))
    check("session" in (d3.get("cookie_names") or []),
          "没拿到会话 cookie: %r" % (d3.get("cookie_names"),))

    # ---- 5. 会话 save / load
    name = "gate_t64"
    r4 = reg.call("protocol.session", args={"op": "save", "name": name, "url": base,
                                            "cookies": d3.get("cookies") or []})
    check(r4.ok, "会话保存失败: %s" % r4.err)
    r5 = reg.call("protocol.session", args={"op": "load", "name": name})
    check(r5.ok and (r5.data or {}).get("loaded", 0) >= 1,
          "会话读回失败: %r" % (r5.data,))

    # ---- 6. 检活：行为证据（带 cookie 必须能进会员区）
    r6 = reg.call("protocol.session", args={"op": "probe", "name": name, "url": base + "/me"})
    check((r6.data or {}).get("status") == 200,
          "带 cookie 检活没通（status=%r）" % ((r6.data or {}).get("status"),))
    check("MEMBER_AREA" in ((r6.data or {}).get("snippet") or ""),
          "检活拿到的不是会员区内容: %r" % ((r6.data or {}).get("snippet"),))

    # ---- 7. 反向证据：不存在的会话不许 probe 成功
    r7 = reg.call("protocol.session", args={"op": "probe", "name": "gate_t64_absent",
                                            "url": base + "/me"})
    check(not r7.ok, "不存在的会话竟然 probe 成功（判据坏了）")

    # ---- 8. 认不出字段要如实报错，不许瞎猜
    r8 = reg.call("protocol.register", args={"url": base + "/vague",
                                             "identity": {"email": "x@example.test"}})
    check(not r8.ok, "字段认不出的页面竟然注册成功（在瞎猜）")
    check("认不出" in (r8.err or ""), "报错没说清是「认不出字段」: %r" % (r8.err,))

    # ---- 9. 打不动的地址要报 unavailable，不是 success
    r9 = reg.call("protocol.register", args={"url": base + "/nothing",
                                             "identity": {"email": "x@example.test"}})
    check(not r9.ok, "没有表单的页面竟然注册成功")

    srv.shutdown()

    # 清掉本次落盘的会话，不给下次留脏
    try:
        from fleet.kinds.protocol_device import _sess_path
        for n in (name,):
            p = _sess_path(n)
            if os.path.isfile(p):
                os.remove(p)
    except Exception:
        pass

    if FAIL:
        print("FAIL: %d 项" % len(FAIL))
        for x in FAIL:
            print("  - " + x)
        return 1
    print("PROTOCOL GATE OK: 表单解析 / 认字段 / 侦察判定 / 协议注册端到端 / "
          "会话存取与检活 / 不猜字段 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
