"""门禁：PC「遥控手机」页的后端通道（离线：假 MCP 客户端 + 真 GUI HTTP 服务）。

钉三件事：

  ① `/api/phone/config` 只在带 GUI 令牌时回默认连接参数；
  ② `/api/phone/mcp` 只放行 `self_*` / `mcp__*` —— 这一页要的是「看屏 + 点按」，
     不是一台可以顺手调本机能力的万能代理；
  ③ MCP 回包归一化：文字、图片（base64 + mime）、isError → 502 都要说得清，
     并且调用失败时把坏连接丢掉，下一次重建（不复用半死的连接）。

跑法：python evals/test_phone_gui_remote.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.gui import server as GS          # noqa: E402

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 假注册表
# 遥控页不该碰本机能力：碰了就是走错路，直接炸出来。
class _Reg:
    def availability(self, ttl=None):
        return []

    def call(self, cap, args=None, gate=None, device=""):
        raise AssertionError("遥控页不该调本机能力：" + cap)


class _FakePhone:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.closed = False

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        r = self.replies[name]
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


def _get(port, path, token=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    if token:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post(port, path, body, token=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    phone = _FakePhone({
        "self_status": {"content": [{"type": "text",
                                     "text": "通道：在线\n屏幕：1080×2400 @420dpi"}]},
        "self_ui_screenshot": {"content": [
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"},
            {"type": "text", "text": "/sdcard/shot.png"},
        ]},
        "self_ui_tap_at": {"content": [{"type": "text", "text": "点到了"}]},
        "self_ui_text": {"content": [{"type": "text", "text": "输入框没开"}],
                         "isError": True},
        "mcp__peer_tool": {"content": [{"type": "text", "text": "远程工具回了"}]},
        "mcp__broken": RuntimeError("对端连接断了"),
    })
    dropped = []
    old_client, old_drop = GS._phone_client, GS._phone_drop
    GS._phone_client = lambda url, room, secret: phone
    GS._phone_drop = lambda url, room, secret: dropped.append((url, room, secret))

    srv = GS.FleetServer(registry=_Reg(), port=0)
    port = srv.start()
    tok = srv.token
    cfg = {"url": "ws://127.0.0.1:9/pair", "room": "r1", "secret": "s1"}
    try:
        print("① 默认连接参数：要令牌，且只读环境变量")
        st, _ = _get(port, "/api/phone/config")
        check(st == 401, "无 token → 401")
        st, d = _get(port, "/api/phone/config", tok)
        check(st == 200 and d.get("ok") is True, "带 token → 200 且 ok")
        check(all(k in d for k in ("url", "room", "secret")),
              "回包里有 url / room / secret 三个键")

        print("② 代理入口：先鉴权，再白名单")
        st, _ = _post(port, "/api/phone/mcp", {"tool": "self_status"})
        check(st == 401, "无 token → 401（遥控页没有绕开鉴权的后门）")
        st, d = _post(port, "/api/phone/mcp", dict(cfg), tok)
        check(st == 400 and "缺 tool" in (d.get("err") or ""),
              "缺 tool → 400 并说明缺什么")
        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="pc.mouse"), tok)
        check(st == 400 and "self_" in (d.get("err") or ""),
              "非 self_/mcp__ 工具 → 400（不是万能代理）")
        check(phone.calls == [], "被白名单挡掉的请求一次都没打到手机")
        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="self_status", args=[1]), tok)
        check(st == 400, "args 不是对象 → 400")

        print("③ 正常回包：文字 / 图片都要原样出来")
        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="self_status"), tok)
        check(st == 200 and d.get("ok") is True, "self_status → 200 ok")
        check("屏幕：1080×2400" in (d.get("text") or ""),
              f"状态文本原样透传（{d.get('text')!r}）")
        check(d.get("image") == "", "纯文本结果不带图片字段")

        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="self_ui_screenshot"), tok)
        check(st == 200 and d.get("image") == "aGVsbG8=", "截图 base64 原样透传")
        check(d.get("mime") == "image/jpeg", "mime 原样透传")
        check("/sdcard/shot.png" in (d.get("text") or ""),
              "图片后的文字说明仍在")

        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="mcp__peer_tool"), tok)
        check(st == 200 and "远程工具回了" in (d.get("text") or ""),
              "mcp__* 工具同样放行")

        print("④ 失败要报清楚，并丢掉坏连接")
        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="self_ui_text", args={"text": "hi"}), tok)
        check(st == 502 and d.get("ok") is False, "手机端 isError → 502 + ok:false")
        check("输入框没开" in (d.get("err") or ""), "失败原因原样带回")

        st, d = _post(port, "/api/phone/mcp",
                      dict(cfg, tool="mcp__broken"), tok)
        check(st == 502 and d.get("ok") is False, "客户端异常 → 502 + ok:false")
        check("对端连接断了" in (d.get("err") or ""), "异常信息没被吃掉")
        check((cfg["url"], cfg["room"], cfg["secret"]) in dropped,
              "调用失败后丢弃这条连接（下一次重建）")

        print("⑤ 「断开」要真断：关掉池里的连接，不是只改界面状态")
        st, d = _post(port, "/api/phone/mcp", dict(cfg, close=True), tok)
        check(st == 200 and d.get("ok") is True, "close=true → 200")
        check(dropped.count((cfg["url"], cfg["room"], cfg["secret"])) >= 2,
              "close 也走丢弃连接（池里那条被真关掉）")

        print("⑥ 结果归一化：resource 文本也算文字")
        p = GS._mcp_result_payload({"content": [
            {"type": "resource", "resource": {"text": "资源文本"}}]})
        check(p.get("ok") is True and p.get("text") == "资源文本",
              "resource.text 被收进 text")
        p = GS._mcp_result_payload(None)
        check(p.get("ok") is True and p.get("text") == "",
              "空结果不崩（ok:true、text:''）")
    finally:
        srv.stop()
        GS._phone_client, GS._phone_drop = old_client, old_drop

    print("test_phone_gui_remote:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
