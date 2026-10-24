# -*- coding: utf-8 -*-
"""逆向工程的 LLM 桥：让「拆解」有人话假设层。

为什么单独一层
--------------
逆向管线是「确定性拆解 + 概率性假设」两段：
  - 拆解（脉冲聚类、AD 结构遍历、strings 分类）是纯计算，答案唯一，不需要模型；
  - 假设（这是什么协议/这是什么设备/这串字符像什么）需要模型读结构化事实再说话。
所以模型只在第二段上场，且**只读事实、不碰决策**——和 webtool.py 那条
「服务端先搜后排，模型只负责读资料+回答」是同一条纪律。

链路（按优先级）：
  1. `~/.workbuddy/models.json` —— 网关池刚同步的选择器文件，★ 最快的排最前，
     「待刷新/已下线/锁区」标记的跳过。它就是「现在有额度」的活的真相源。
  2. `llm.complete()` —— 项目自带的免费多端链（pollinations/ovh/llm7），
     models.json 不在时兜底。
全断时报错必须说清是**暂时状态会自己恢复**（额度按窗口滚动），别写成永久不通。
"""
import json
import os
import time
import urllib.error
import urllib.request

MODELS_JSON = os.path.expanduser(
    os.environ.get("FLEET_MODELS_JSON", "~/.workbuddy/models.json"))

# 本机地址必须绕开代理
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 调用预算：逆向假设不需要长篇大论，但要够放下结构化解剖
MAX_TOKENS = int(os.environ.get("RE_LLM_MAX_TOKENS", "700"))
TIMEOUT = float(os.environ.get("RE_LLM_TIMEOUT", "90"))


def _live_entries():
    """models.json 里当前可用的端点（按文件内顺序，★ 已在最前）。

    标记规则（gateway_pool.sync 写的）：名字里带「待刷新 / 已下线 / 锁区 /
    待验」的都是**暂时或已知不可用**，这轮跳过——但它们明天可能就活了，
    所以这里只是「本轮不调」，不是「删除」。
    """
    try:
        with open(MODELS_JSON, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("models") or []
    except (OSError, ValueError):
        return []
    bad = ("待刷新", "已下线", "锁区", "待验")
    out = []
    for r in rows:
        name = r.get("name") or ""
        if any(b in name for b in bad):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        out.append({"name": name, "url": url,
                    "key": r.get("apiKey") or "",
                    "model": r.get("id") or ""})
    return out


def available():
    """现在有没有可用的 LLM。返回 (ok, 说明)。"""
    es = _live_entries()
    if es:
        return True, "%d 个端点可用（最快：%s）" % (len(es), es[0]["name"])
    if os.path.exists(MODELS_JSON):
        return False, ("models.json 在但当前无可用端点：多半是额度窗口打满，"
                       "按窗口滚动会自己恢复，别当成永久不通")
    return False, "models.json 不存在（跑一次 gateway_pool.py sync 生成）"


def _call_one(e, prompt, system):
    body = {
        "messages": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    if e.get("model"):
        body["model"] = e["model"]
    headers = {"Content-Type": "application/json"}
    if e.get("key"):
        headers["Authorization"] = "Bearer " + e["key"]
    req = urllib.request.Request(e["url"], json.dumps(body).encode("utf-8"),
                                 headers)
    with _OPEN.open(req, timeout=TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    txt = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    # 剥思考模型漏进正文的 <think>…</think>（与 acquire._strip_think 同规）
    while True:
        t = txt.lstrip()
        if t.startswith("</think>"):
            txt = t[len("</think>"):]
            continue
        if t.startswith("<think>"):
            e = txt.find("</think>")
            if e < 0:
                break
            txt = txt[e + len("</think>"):]
            continue
        break
    return txt.strip()


def ask(prompt, system="", n_try=3):
    """问一句，返回 (text, used_name)。全断抛 RuntimeError（带自愈说明）。"""
    entries = _live_entries()
    if not entries:
        # 回退项目自带免费链（pollinations/ovh/llm7…）
        try:
            from llm import complete
            return complete(prompt, system=system), "llm.complete"
        except Exception as e:  # noqa: BLE001
            ok, why = available()
            raise RuntimeError("LLM 桥全断：%s；%s" % (str(e)[:80], why))

    last = ""
    for e in entries[:max(1, n_try)]:
        try:
            t = _call_one(e, prompt, system)
            if t:
                return t, e["name"]
            last = "%s 空内容" % e["name"]
        except urllib.error.HTTPError as ex:
            last = "%s HTTP%s" % (e["name"], ex.code)
        except Exception as ex:  # noqa: BLE001
            last = "%s %s" % (e["name"], str(ex)[:50])
    raise RuntimeError(
        "LLM 桥这轮没出字（试了 %d 个）。最后：%s。"
        "这多半是额度窗口打满——按窗口滚动会自己恢复，不是永久不通"
        % (min(n_try, len(entries)), last))


# ---------------------------------------------------------------- 自测
def _selftest():
    ok = fail = 0

    def chk(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [X] %s" % what)

    # 1) 假的 models.json：活/待刷新/下线/无 key 混合
    import tempfile
    global MODELS_JSON
    old = MODELS_JSON
    tmp = tempfile.mkdtemp()
    MODELS_JSON = os.path.join(tmp, "models.json")
    with open(MODELS_JSON, "w", encoding="utf-8") as f:
        json.dump({"models": [
            {"name": "★ A · 1M", "url": "http://127.0.0.1:1/v1/chat/completions",
             "apiKey": "", "id": "a"},
            {"name": "B · 250k · 待刷新", "url": "http://127.0.0.1:2/v1",
             "apiKey": "k", "id": "b"},
            {"name": "C · 128k · 已下线", "url": "http://127.0.0.1:3/v1",
             "apiKey": "", "id": "c"},
            {"name": "D · 锁区", "url": "http://127.0.0.1:4/v1",
             "apiKey": "", "id": "d"},
        ]}, f)
    es = _live_entries()
    chk(len(es) == 1 and es[0]["name"].startswith("★"),
        "只留活的 A，待刷新/下线/锁区被跳过（但不删文件里的）")
    okav, why = available()
    chk(okav and "1 个端点" in why, "available() 报数对")

    # 2) 全不可用时的说法要带「会自己恢复」
    with open(MODELS_JSON, "w", encoding="utf-8") as f:
        json.dump({"models": [
            {"name": "B · 待刷新", "url": "http://127.0.0.1:2/v1",
             "apiKey": "k", "id": "b"}]}, f)
    okav2, why2 = available()
    chk((not okav2) and "自己恢复" in why2, "无可用端点时说明带自愈提示")

    # 3) 真调一个本地假端点（起个一次性 HTTP 服务，验证请求体+解析）
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got["body"] = json.loads(self.rfile.read(n).decode())
            got["auth"] = self.headers.get("Authorization")
            out = {"choices": [{"message": {
                "content": "<think>先想一下</think>这是假设：NEC 协议。"}}]}
            b = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with open(MODELS_JSON, "w", encoding="utf-8") as f:
        json.dump({"models": [
            {"name": "★ 假端点", "url": "http://127.0.0.1:%d/v1/chat/completions" % port,
             "apiKey": "k123", "id": "fake-1"}]}, f)
    txt, used = ask("这像什么协议", system="你是逆向工程师")
    chk(txt == "这是假设：NEC 协议。" and "假端点" in used,
        "ask() 打通假端点并剥掉 <think> 标签")
    chk(got["auth"] == "Bearer k123" and got["body"]["model"] == "fake-1",
        "请求带了 apiKey 与 model id")
    chk(got["body"]["max_tokens"] == MAX_TOKENS, "预算用 RE 档（不饿死思考型）")
    srv.shutdown()

    MODELS_JSON = old
    print("re_llm 自测：%d 过 %d 挂" % (ok, fail))
    return fail == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    ok, why = available()
    print("LLM 桥：%s —— %s" % ("可用" if ok else "不可用", why))
    if ok and len(sys.argv) > 1:
        t, used = ask(" ".join(sys.argv[1:]))
        print("[%s] %s" % (used, t))
