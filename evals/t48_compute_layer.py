"""t48 —— 算力层：能接、能测、能路由，而且 key 不外泄。

为什么这条门禁必须存在（2026-10-29）
----------------------------------
「算力」在这之前是散在四个 env 里的实现细节：决策绑 `AT_DECIDER_URL`、
对话绑 `ai_backends`、看图绑 `providers`、语音另有一处。桌面上一个都看不见。
结果「本地小模型 58 选 1 要 52.9 秒」这个**物理限制**被当成**能力上限**，
一路去抠模型、抠分域、抠提示词。

现在有了 `fleet/compute.py` + 四个 GUI 端点。这条门禁守四件事：

  ① 能接：任何 OpenAI 兼容端点都能加进来、存住、改回来
  ② 能测：probe 说「通了」必须真的算过 —— 不是「有回答」
  ③ 能路由：某个用途钉死哪个端点，钉死的端点不可用时不静默换别的顶上
  ④ 密钥不漏：公开视图、HTTP 回包、日志里都不能出现完整 key

第 ② 条是本门禁的重点：`HTTP 200 ≠ 发生了一件事`。
llama-server 模板编译失败时不返回 500，而是 200 + 把 jinja 错误当回答。
所以下面用三个假端点分别复现三种情况：

  假端点 A：200 + 错误文本（模板编译失败的样子）      → 必须判 fail
  假端点 B：200 + 合法 JSON，但 prompt_tokens 恒 18    → 必须判 shallow
  假端点 C：200 + 合法 JSON + 真读到了 system 里的数   → 才判 ok

**不碰外网、不碰真实 ~/.fleet**：全部走临时目录 + 本地假端点，
所以这条门禁在断网机器上、在有人正起着服务的机器上都照样可信。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import compute as C                      # noqa: E402

OK = 0
FAIL = 0
NOTES = []


def check(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok   %s" % msg)
    else:
        FAIL += 1
        print("  FAIL %s" % msg)


def section(t):
    print()
    print("== %s ==" % t)


# ---------------------------------------------------------------------------
# 假端点：三种行为，复现三种真实故障
# ---------------------------------------------------------------------------

class _FakeAI(BaseHTTPRequestHandler):
    mode = "ok"          # ok | template_broken | no_usage

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._j({"object": "list", "data": [{"id": "fake"}]})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        sysmsg = ""
        for m in body.get("messages") or []:
            if m.get("role") == "system":
                sysmsg = m.get("content") or ""

        if _FakeAI.mode == "template_broken":
            # llama-server 模板编译失败的样子：HTTP 200 + finish_reason=stop
            # + 正文是 jinja 错误。上游看到的是「模型答了句废话」。
            return self._j({
                "model": "fake",
                "choices": [{"finish_reason": "stop", "message": {
                    "content": "error: Invalid template variable name: "
                               "'qwen3_thinkblock', expecting 'end'"}}],
                "usage": {"prompt_tokens": 18, "completion_tokens": 9},
            })

        if _FakeAI.mode == "no_usage":
            # prompt 根本没进模型：tokens 恒等于 18，回答也是空的
            return self._j({
                "model": "fake",
                "choices": [{"finish_reason": "stop",
                             "message": {"content": ""}}],
                "usage": {"prompt_tokens": 18, "completion_tokens": 0},
            })

        # ok：真的从 system 里把那个三位数抠出来回话
        num = ""
        for tok in sysmsg.replace("。", " ").replace("，", " ").split():
            if tok.isdigit() and len(tok) == 3:
                num = tok
                break
        return self._j({
            "model": "fake-good",
            "choices": [{"finish_reason": "stop",
                         "message": {"content": num or "(没读到)"}}],
            "usage": {"prompt_tokens": len(sysmsg) // 2 + 20,
                      "completion_tokens": 4},
        })

    def _j(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def start_fake(mode):
    _FakeAI.mode = mode
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    tmp = tempfile.mkdtemp(prefix="t48_compute_")
    # 把算力层的落盘位置整个搬进临时目录，绝不碰真实的 ~/.fleet
    C._DIR = tmp
    C.CONF_PATH = os.path.join(tmp, "compute.json")
    C.KEYS_PATH = os.path.join(tmp, "compute_keys.json")

    fakes = []
    try:
        section("① 注册表：内置清单覆盖四种来源")
        eps = C.list_all()
        ids = {e["id"] for e in eps}
        check(len(eps) >= 8, "内置端点 %d 个" % len(eps))
        check("pollinations" in ids, "有免 key 端点（开箱可用）")
        check(any(e["need_key"] for e in eps), "有需要自己 key 的端点（付费正路）")
        check(any("127.0.0.1" in e["base_url"] for e in eps), "有本地端点（断网兜底）")
        check(all(e["base_url"].endswith(("/v1", "/v4"))
                  for e in eps),
              "base_url 都写到版本层（拼 chat 时不会再叠一层 /v1）")

        section("② 能接：加 / 改 / 不洗 key / 删")
        pub, err = C.upsert({"id": "mine", "title": "我的中转",
                             "base_url": "https://relay.test/v1",
                             "model": "m1", "need_key": True,
                             "key": "sk-abcdefgh1234"})
        check(not err and pub["id"] == "mine", "加一个自定义端点")
        check(C.get("mine") is not None, "存下来能读回")

        pub2, _ = C.upsert({"id": "mine", "base_url": "https://relay.test/v1",
                            "model": "m2"})
        check(pub2["has_key"], "只改模型名不会把 key 洗掉（实得 has_key=%r）"
              % pub2["has_key"])
        pub3, _ = C.upsert({"id": "mine", "base_url": "https://relay.test/v1",
                            "key": ""})
        check(not pub3["has_key"], "显式传空 key 才清空")

        check(not C.remove("pollinations")[0], "内置端点不许删（只能停用）")
        check(C.remove("mine")[0], "自定义端点可删")
        check(C.remove("mine")[0] is False, "重复删会明确报错，不静默成功")

        section("③ 密钥不外泄")
        C.upsert({"id": "sec", "title": "密钥测试",
                  "base_url": "https://relay.test/v1", "need_key": True,
                  "key": "sk-SECRETSECRET9999"})
        pub = C.get("sec").to_public()
        blob = json.dumps(pub, ensure_ascii=False)
        check("sk-SECRETSECRET9999" not in blob, "公开视图里没有完整 key")
        check(pub["key_hint"] == "***9999", "只回末四位（实得 %r）" % pub["key_hint"])
        conf_txt = ""
        if os.path.exists(C.CONF_PATH):
            conf_txt = open(C.CONF_PATH, encoding="utf-8").read()
        check("sk-SECRETSECRET9999" not in conf_txt,
              "compute.json（配置文件）里没有 key")
        keys_txt = open(C.KEYS_PATH, encoding="utf-8").read()
        check("sk-SECRETSECRET9999" in keys_txt,
              "key 落在单独的 keys 文件里（明确落盘，不假装）")
        C.remove("sec")

        section("④ probe：HTTP 200 不等于算过了（三种假端点）")

        srv_ok, port_ok = start_fake("ok")
        fakes.append(srv_ok)
        C.upsert({"id": "fake-ok", "base_url": "http://127.0.0.1:%d/v1" % port_ok})
        r = C.probe("fake-ok", timeout=10)
        check(r.get("ok") is True and r.get("level") == "ok",
              "真读到 system 里的数 → level=ok（实得 %r）" % (r.get("level"),))
        check(r.get("prompt_tokens", 0) > 0,
              "拿到了 usage.prompt_tokens=%s" % r.get("prompt_tokens"))
        C.remove("fake-ok")

        srv_bad, port_bad = start_fake("template_broken")
        fakes.append(srv_bad)
        C.upsert({"id": "fake-tpl", "base_url": "http://127.0.0.1:%d/v1" % port_bad})
        r = C.probe("fake-tpl", timeout=10)
        check(r.get("ok") is False,
              "模板报错当回答吐回来 → 判 fail（实得 ok=%r）" % r.get("ok"))
        check("组件报错" in (r.get("err") or ""),
              "原因说清是「组件报错」而不是「模型不会」（实得 %r）"
              % (r.get("err") or "")[:60])
        C.remove("fake-tpl")

        srv_ns, port_ns = start_fake("no_usage")
        fakes.append(srv_ns)
        C.upsert({"id": "fake-ns", "base_url": "http://127.0.0.1:%d/v1" % port_ns})
        r = C.probe("fake-ns", timeout=10)
        check(r.get("ok") is True and r.get("level") == "shallow",
              "prompt_tokens 恒等、没回出那个数 → shallow 不算真通（实得 %r）"
              % (r.get("level"),))
        C.remove("fake-ns")

        section("⑤ probe：连不上时给的是人话")
        # 占一个端口再立刻放掉，拿一个「刚空出来的」端口 —— 比硬编码
        # 一个数字更不依赖环境（万一那端口正被别的服务占着）。
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        C.upsert({"id": "dead", "base_url": "http://127.0.0.1:%d/v1" % dead})
        r = C.probe("dead", timeout=5)
        check(r.get("ok") is False, "端口没开 → fail")
        check("被拒" in (r.get("err") or "") or "端口没开" in (r.get("err") or ""),
              "原因说清是端口没开（实得 %r）" % (r.get("err") or "")[:60])
        C.remove("dead")

        section("⑥ 路由：钉谁走谁，钉的不可用时不悄悄换人")
        C.upsert({"id": "pin", "base_url": "https://relay.test/v1",
                  "need_key": True})       # 没 key
        check(C.set_route("decide", "pin")[0], "钉死决策走 pin")
        check(C.route("decide") is None,
              "pin 没 key 时 route() 返回 None，而不是拿别的端点顶上")
        C.upsert({"id": "pin", "base_url": "https://relay.test/v1",
                  "key": "sk-x"})
        got = C.route("decide")
        check(got is not None and got.id == "pin", "填了 key 之后才走它")
        check(C.set_route("decide", "")[0], "可以取消钉死")
        got2 = C.route("decide")
        check(got2 is not None and not got2.need_key,
              "取消后回到免 key 端点（%s）" % (got2.id if got2 else "None"))
        # 本机网关 + 有 key 要压过「免 key 的海外端点」。
        # 这条是踩出来的：一开始按「免 key 优先」排，结果用户配好了本机
        # zen-gate（1.9s）却仍然走海外 pollinations（7~8s）—— 配了却用不上，
        # 比不配更让人困惑。
        C.upsert({"id": "local-gw", "title": "本机网关",
                  "base_url": "http://127.0.0.1:8787/v1",
                  "model": "m", "need_key": True, "key": "sk-local"})
        pick = C.route("decide")
        check(pick is not None and pick.id == "local-gw",
              "本机有 key 的网关优先于免 key 端点（实得 %s）"
              % (pick.id if pick else "None"))

        # 但「测通」不能跨档夺权。踩过：点一下「测一下」，pollinations
        # （10.2 秒）立刻把 zen-gate（1.9 秒）挤掉了 —— 因为前者进了
        # 「探测通过」那一档，而后者这个进程里还没测过。
        # 测通的语义是「它可用」，不是「选它当默认」；选默认要去下拉框钉死。
        C._LAST_PROBE["pollinations"] = (True, 50)
        pick2 = C.route("decide")
        check(pick2 is not None and pick2.id == "local-gw",
              "免 key 端点测通之后，仍然不压过本机网关（实得 %s）"
              % (pick2.id if pick2 else "None"))
        C._LAST_PROBE.pop("pollinations", None)

        # 同档内，探测结果才起作用：两个远端付费端点，测通且快的胜
        C.upsert({"id": "r-slow", "title": "远端A",
                  "base_url": "https://a.test/v1", "need_key": True,
                  "key": "sk-a"})
        C.upsert({"id": "r-fast", "title": "远端B",
                  "base_url": "https://b.test/v1", "need_key": True,
                  "key": "sk-b"})
        C.set_route("chat", "r-slow")
        got_slow = C.route("chat")
        check(got_slow is not None and got_slow.id == "r-slow", "钉死优先")
        C._LAST_PROBE["r-fast"] = (True, 30)
        got_fast = C.route("chat")
        check(got_fast is not None and got_fast.id == "r-slow",
              "钉死的仍然优先（探测结果不推翻钉死）")
        C.set_route("chat", "")
        got_auto = C.route("chat")
        check(got_auto is not None and got_auto.id == "local-gw",
              "取消钉死后仍按档位走本机网关（实得 %s）"
              % (got_auto.id if got_auto else "None"))
        C._LAST_PROBE.pop("r-fast", None)
        C.remove("r-slow")
        C.remove("r-fast")
        C.remove("local-gw")
        check(C.route("decide").id == "pollinations",
              "删掉本机网关后回到免 key 端点")
        check(C.set_route("不存在的用途", "")[0] is False, "未知用途被拒")
        check(C.set_route("decide", "根本没这个")[0] is False, "钉不存在的端点被拒")
        C.remove("pin")

        section("⑦ 删端点时把指着它的路由一起清掉（别留悬空引用）")
        C.upsert({"id": "tmproute", "base_url": "https://relay.test/v1",
                  "key": "sk-y"})
        C.set_route("chat", "tmproute")
        check(C.routes()["chat"]["endpoint"] == "tmproute", "先钉上")
        C.remove("tmproute")
        check(C.routes()["chat"]["endpoint"] != "tmproute",
              "删掉后被清掉，没留悬空 id（实得 %r）"
              % C.routes()["chat"]["endpoint"])
        C.set_route("chat", "")

        section("⑧ GUI 端点：带上真实 HTTP 层")
        import urllib.error
        import urllib.request
        from fleet.gui.approval import ApprovalBroker
        from fleet.gui.server import FleetServer

        srv = FleetServer(port=0, approval=ApprovalBroker())
        port = srv.start()
        token = srv.page_url("light").split("#t=", 1)[1].split("&")[0]
        base = srv.base_url

        def call(path, body=None, tok=None):
            url = base + path
            hdr = {"Content-Type": "application/json"}
            if tok is not None:
                hdr["X-Fleet-Token"] = tok
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=hdr,
                                         method="POST" if data else "GET")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read().decode("utf-8"))
                except Exception:
                    return e.code, {}

        try:
            code, d = call("/api/compute", tok="wrong-token")
            check(code == 401, "错 token 回 401（实得 %s）" % code)

            code, d = call("/api/compute", tok=token)
            check(code == 200 and d.get("ok") is True, "GET /api/compute 通")
            check(isinstance(d.get("endpoints"), list) and d["endpoints"],
                  "回包里带端点清单（%d 个）" % len(d.get("endpoints") or []))
            check("keys_path" in d, "明确回报 key 落在哪个文件")
            blob = json.dumps(d, ensure_ascii=False)
            check("sk-abcdefgh1234" not in blob, "回包里没有完整 key")

            code, d = call("/api/compute/save",
                           {"id": "via-http", "title": "走 HTTP 加的",
                            "base_url": "http://127.0.0.1:%d/v1" % port_ok},
                           tok=token)
            check(code == 200 and d.get("ok"), "POST save 能加端点")

            code, d = call("/api/compute/probe", {"id": "via-http"}, tok=token)
            check(code == 200 and d.get("ok") is True,
                  "POST probe 请求本身成功（外层 ok）")
            check(d.get("result", {}).get("ok") is True,
                  "内层 result.ok=True 表示端点真算过了")

            code, d = call("/api/compute/probe", {"id": "via-http"}, tok=None)
            check(code == 401, "不带 token 的 probe 回 401（实得 %s）" % code)

            code, d = call("/api/compute/route",
                           {"purpose": "decide", "id": "via-http"}, tok=token)
            check(code == 200 and d.get("routes", {}).get("decide", {})
                  .get("endpoint") == "via-http", "POST route 能钉")
            code, d = call("/api/compute/route",
                           {"purpose": "decide", "id": ""}, tok=token)
            check(code == 200, "POST route 能取消钉")

            code, d = call("/api/compute/delete", {"id": "via-http"}, tok=token)
            check(code == 200 and d.get("ok"), "POST delete 能删")

            code, d = call("/api/compute/probe", {"id": "根本没这个"}, tok=token)
            check(d.get("result", {}).get("ok") is False,
                  "探一个不存在的 id → result.ok=False，而不是 500")
        finally:
            srv.stop()

        section("⑨ 变异测试：把 key 遮挡拆掉，门禁必须变红")
        # 自证这条门禁不是空的：临时让 to_public 把完整 key 吐出来，
        # 上面「公开视图里没有完整 key」那条断言就该 FAIL。
        orig = C.Endpoint.to_public

        def leaky(self):
            d = orig(self)
            d["key_hint"] = self.key          # 故意泄
            return d

        C.Endpoint.to_public = leaky
        try:
            C.upsert({"id": "mut", "base_url": "https://relay.test/v1",
                      "need_key": True, "key": "sk-LEAKCHECK777"})
            pub = C.get("mut").to_public()
            leaked = "sk-LEAKCHECK777" in json.dumps(pub, ensure_ascii=False)
            check(leaked, "变异真的泄漏了 key（否则这条自证没意义）")
            check(pub["key_hint"] != "***777",
                  "此时 key_hint 不再是遮的形态 → 原断言会 FAIL（自证有效）")
            C.remove("mut")
        finally:
            C.Endpoint.to_public = orig

        # 复原后必须重新遮住
        C.upsert({"id": "mut2", "base_url": "https://relay.test/v1",
                  "need_key": True, "key": "sk-LEAKCHECK888"})
        pub = C.get("mut2").to_public()
        check("sk-LEAKCHECK888" not in json.dumps(pub, ensure_ascii=False),
              "复原后重新遮住")
        C.remove("mut2")

    finally:
        for s in fakes:
            try:
                s.shutdown()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 52)
    print("t48_compute_layer: %d ok / %d fail" % (OK, FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
