"""门禁 t7：Windows 图形版（W1）的后端与前端——本地服务真跑一遍。

锁死的是真会发生的形状，不是假想：

  ① **token 鉴权是一道真门**。
     缺 X-Fleet-Token 头 → 401 且带原因；带错 token → 403 且带原因。
     这条必须钉死：一个「能点鼠标键盘的 HTTP 接口」没锁，就是给局域网开了后门。
     （W2 起把「没带凭据」与「凭据不对」分开：前者是调用方忘了加头，后者是
     有人拿着旧令牌来敲——处置不一样，所以状态码也不一样。）

  ② **只绑 127.0.0.1 + 随机端口**。
     固定端口会被同机别的程序撞上或被外部扫到；绑 0.0.0.0 等于把这台机器
     变成局域网里谁都能指挥的遥控器。这里断言 server_address 的地址就是
     127.0.0.1，且端口不是写死的（0 = 让系统分配）。

  ③ **API 是 JSON，契约是定死的**。
     /api/health /api/caps /api/devices /api/persona /api/cards 各自的字段
     都要在；缺字段就是坏了（前端会静默瞎显示）。

  ④ **/api/chat 走 SSE 而且真是流式**。
     不是「等生成完一次性吐出来」——那种实现界面看着像流式，其实中间是死的。
     这里用假 AI 后端（真流式吐 token、带间隔），从裸 socket 上按块读：
     必须先收到 line 事件、再收到 done，且两者之间的时间差 ≥ 后端的吐字间隔。

  ⑤ **/api/call 走注册表闸门**。
     没注入 gate 时，副作用能力必须被拒（fail-closed），且答复里带明确原因；
     只读能力不受影响。

  ⑥ **前端不许有外链**。
     无 CDN、无框架、无 npm、无绝对 URL——断网可用，也就不会偷偷外传。
     同时钉住「前端只走 /api/…，不直接碰设备」：所有 URL 字面量必须是
     本地相对路径。

  ⑦ **token 不许硬编码、不许出现在任何 API 答复里**。

全离线、可复现：AI 后端被打桩（真流式但不联网），角色卡与状态放临时目录，
设备清单用临时账本。不碰真屏幕、不连网、不点真鼠标。

跑法：python evals/t7_gui_backend.py
"""
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fleet.ai_backends as AB          # noqa: E402
from fleet.catalog import build         # noqa: E402
from fleet.gui import server as GS      # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)
    return bool(cond)


# ------------------------------------------------------------------ 假 AI 后端
# 关键：只打桩网络层（ai_backends.backends / ai_backends.chat），
# 聊天链路本身（registry.call → AI._chat → _one → _on_line）全是真的。
# 所以这个门禁验的是「我们的流式管道对不对」，不是「网络通不通」。

STREAM_DELAY = 0.12          # 假后端每行之间的间隔（秒）
STREAM_LINES = ["第一行：我在。", "第二行：正在想。", "第三行：好了。"]

_seen = {"system": None, "prompt": None, "done_at": None, "first_at": None}


def _fake_backends():
    return [{
        "id": "t7-fake", "title": "T7 假后端", "protocol": "openai",
        "base_url": "http://127.0.0.1:0/v1", "model": "t7-fake-model",
        "key_env": None, "need_key": False,
    }]


def _fake_chat(b, prompt, system="", on_token=None, max_tokens=2048, timeout=120):
    """假后端：按行吐 token，间隔 STREAM_DELAY。

    记下 system 与 prompt，门禁要验「人格确实是用 persona_card.build_system 拼的」。
    """
    _seen["system"] = system or ""
    _seen["prompt"] = prompt
    text = ""
    for i, line in enumerate(STREAM_LINES):
        if i:
            time.sleep(STREAM_DELAY)
        if on_token:
            on_token(line + "\n")
        text += line + "\n"
    _seen["done_at"] = time.time()
    return text


AB.backends = _fake_backends
AB.chat = _fake_chat


# ------------------------------------------------------------------ HTTP 小工具
def http_json(url, token=None, method="GET", body=None, timeout=10):
    """打一个 API。返回 (状态码, 解析后的 JSON 或 None, 错误文本)。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Fleet-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, _try_json(raw), ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _try_json(raw), ""
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _try_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ------------------------------------------------------------------ SSE 裸读
def _strip_comments(src):
    """剥掉 JS/CSS/HTML 里的注释，**保留字符串字面量原样**。

    为什么不能用简单正则：字符串里的 "https://" 会被 `//` 误当成行注释
    吃掉——那正好是「引了 CDN」最该被抓到的形态。这里手工扫一遍，
    跟踪引号状态，只在引号外认注释。
    """
    out = []
    i = 0
    n = len(src)
    quote = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("<!--", i):
            j = src.find("-->", i)
            i = n if j < 0 else j + 3
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _code_strings(src):
    """抽出源码里**真正被执行的**字符串字面量（不含文档字符串/注释）。

    为什么不用简单子串匹配：门禁文档里写「不绑 0.0.0.0」是在讲纪律，
    拿子串去查会把纪律说明当成违规。用 AST 只看常量，才查得到真东西。
    """
    import ast
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    # 先标出文档字符串节点（模块/类/函数的第一个字符串常量）
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue          # 文档字符串：说的是纪律，不是代码
            out.add(node.value)
    return out


def _sse_collect(base, token, prompt, timeout=30):
    """用裸 socket 读 /api/chat 的 SSE。

    为什么不用 urllib：urllib 要把整个响应读完才交给我们，那时「是不是真流式」
    已经看不出来了。裸 socket 逐块收，才能看出「第一个 line 什么时候到、done
    什么时候到」——这两者的间隔就是流式的证据（攒完再一次性发的话，间隔≈0）。
    """
    host, _, port = base.replace("http://", "").partition(":")
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = (f"POST /api/chat HTTP/1.1\r\n"
           f"Host: {host}:{port}\r\n"
           f"X-Fleet-Token: {token}\r\n"
           f"Content-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n").encode("utf-8") + body

    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.sendall(req)
    s.settimeout(timeout)
    raw = []
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw.append((time.time(), chunk))
    except socket.timeout:
        pass
    finally:
        s.close()

    # 逐块喂进累加器，拆出 SSE 帧；每帧的到达时间 = 它所在那一块的到达时间。
    events = []
    stamps = []
    acc = b""
    for at, chunk in raw:
        acc += chunk
        while b"\n\n" in acc:
            frame, _, acc = acc.partition(b"\n\n")
            for line in frame.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                try:
                    ev = json.loads(line[5:].decode("utf-8"))
                except ValueError:
                    continue
                events.append(ev)
                stamps.append(at)
    first_line = next((t for t, e in zip(stamps, events) if e.get("type") == "line"), None)
    done_at = next((t for t, e in zip(stamps, events) if e.get("type") == "done"), None)
    gap = (done_at - first_line) if (first_line is not None and done_at is not None) else None
    return events, gap


# ------------------------------------------------------------------ 临时环境
_tmp = tempfile.mkdtemp(prefix="t7_gui_")
_cards_dir = os.path.join(_tmp, "cards")
os.makedirs(_cards_dir, exist_ok=True)

# 一张最小角色卡 + active 指针：人格链路必须是真链路（build_system 真拼），
# 但不能依赖仓库里那张卡（active.json 是本机状态，不入库）。
_card = {"name": "T7小鲸", "nickname": "小鲸",
         "description": "门禁用的假鲸鱼", "personality": "话少",
         "scenario": "在门禁里"}
with open(os.path.join(_cards_dir, "t7.json"), "w", encoding="utf-8") as f:
    json.dump(_card, f, ensure_ascii=False)
with open(os.path.join(_cards_dir, "active.json"), "w", encoding="utf-8") as f:
    json.dump({"file": "t7.json"}, f, ensure_ascii=False)

_persona = GS.Persona(card_dir=_cards_dir,
                      state_path=os.path.join(_tmp, "persona_state.json"))

# 注册表：临时账本、不读编队节点（不碰真设备账）。
reg = build(path=os.path.join(_tmp, "devices.json"), with_nodes=False)

srv = GS.FleetServer(registry=reg, persona=_persona)
srv.start()
BASE = srv.base_url
TOKEN = srv.token
print(f"[t7] 本地服务 {BASE}（测试用）", flush=True)

try:
    # ================================================================ ② 绑定
    check(srv.host == "127.0.0.1", f"只绑 127.0.0.1（实际 {srv.host}）")
    check(srv.port > 0, f"端口由系统随机分配（实际 {srv.port}）")
    _addr = srv._httpd.server_address
    check(_addr[0] == "127.0.0.1", f"server_address 就是回环（实际 {_addr[0]}）")
    srv2 = GS.FleetServer(registry=reg, persona=_persona)
    srv2.start()
    check(srv2.port != srv.port, "两次启动端口不同（随机端口，不是写死的）")
    srv2.stop()

    # ================================================================ ① token
    st, js, _ = http_json(f"{BASE}/api/health")
    check(st == 401, f"不带 token → 401（实际 {st}）")
    check(bool(js and js.get("err")), "401 答复带明确原因（err 非空）")
    check(bool(js and "token" in json.dumps(js, ensure_ascii=False).lower()),
          "401 原因里点明是令牌问题")

    st, js, _ = http_json(f"{BASE}/api/health", token="wrong-token")
    check(st == 403, f"带错 token → 403（实际 {st}）")
    check(bool(js and js.get("err")), "错 token 的 403 也带原因")

    st, js, _ = http_json(f"{BASE}/api/health", token=TOKEN)
    check(st == 200, f"带对 token → 200（实际 {st}）")
    check(bool(js), "health 是 JSON")

    st, js, _ = http_json(f"{BASE}/api/health", token=TOKEN)
    check(bool(js) and TOKEN not in json.dumps(js, ensure_ascii=False),
          "token 不出现在任何 API 答复里")

    # ================================================================ ③ 契约
    st, h, err = http_json(f"{BASE}/api/health", token=TOKEN)
    check(st == 200, f"GET /api/health 200（实际 {st} {err}）")
    if h:
        for k in ("ok", "devices", "caps_count", "backend", "persona"):
            check(k in h, f"health 里有 {k}")
        check(h.get("devices") == len(reg.devices()), "health.devices 与注册表一致")
        check(h.get("caps_count") == len(reg.capabilities()),
              "health.caps_count 与注册表一致")
        check(isinstance(h.get("persona"), dict), "health.persona 是对象")

    st, c, err = http_json(f"{BASE}/api/caps", token=TOKEN)
    check(st == 200, f"GET /api/caps 200（实际 {st} {err}）")
    if c:
        check(isinstance(c.get("caps"), list) and c["caps"], "caps 是非空列表")
        check(isinstance(c.get("domains"), dict) and c["domains"],
              "caps 按 domain 分组（domains 非空）")
        one = c["caps"][0]
        for k in ("name", "title", "side_effect", "approval", "domain"):
            check(k in one, f"cap 条目里有 {k}")
        check(all(isinstance(x.get("name"), str) for x in c["caps"]),
              "cap 名字是字符串（前端拿去当 key 用）")
        check(any(x.get("side_effect") for x in c["caps"]), "清单里标得出副作用能力")

    st, d, err = http_json(f"{BASE}/api/devices", token=TOKEN)
    check(st == 200, f"GET /api/devices 200（实际 {st} {err}）")
    if d:
        check(isinstance(d.get("devices"), list) and d["devices"], "devices 是非空列表")
        one = d["devices"][0]
        for k in ("name", "kind", "via", "caps"):
            check(k in one, f"device 条目里有 {k}")
        check("ok" in one, "device 条目里有可用性（ok）")
        check(all("reason" in x for x in d["devices"]), "不可用的设备也带原因")

    st, p, err = http_json(f"{BASE}/api/persona", token=TOKEN)
    check(st == 200, f"GET /api/persona 200（实际 {st} {err}）")
    if p:
        check(p.get("card_loaded") is True, "persona 说清了卡片已加载")
        check((p.get("card") or {}).get("name") == "T7小鲸",
              "persona 报的是当前选中的卡")

    st, cards, err = http_json(f"{BASE}/api/cards", token=TOKEN)
    check(st == 200, f"GET /api/cards 200（实际 {st} {err}）")
    if cards:
        check(isinstance(cards.get("cards"), list) and cards["cards"], "cards 是非空列表")
        check(any(x.get("name") == "T7小鲸" for x in cards["cards"]), "cards 里能看到那张卡")

    # ================================================================ ⑤ 闸门
    st, res, err = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                              body={"cap": "ai.complete", "args": {"prompt": "x"}})
    check(st == 200, f"POST /api/call 200（实际 {st} {err}）")
    if res:
        check(res.get("ok") is False, "无闸门时副作用能力不成功")
        check(res.get("code") == "denied", f"无闸门 → denied（实际 {res.get('code')}）")
        check(bool(res.get("err")), "denied 带原因")
        check("闸门" in (res.get("err") or ""), "原因里点明是缺审批闸门")

    st, res, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                            body={"cap": "ai.list", "args": {}})
    check(st == 200 and bool(res) and res.get("ok") is True,
          "只读能力（ai.list）不受闸门影响")

    st, res, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                            body={"cap": "host.input.click", "args": {"x": 1, "y": 1}})
    check(bool(res) and res.get("code") == "denied",
          "ask 档能力（host.input.click）无闸门 → denied，绝不真点鼠标")

    st, res, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                            body={"cap": "nope.nope", "args": {}})
    check(st == 404, f"未登记能力 → 404（实际 {st}）")
    check(bool(res and res.get("err")), "404 带原因")

    st, res, _ = http_json(f"{BASE}/api/call", token=TOKEN, method="POST",
                            body={"args": {}})
    check(st == 400, f"缺 cap 字段 → 400（实际 {st}）")
    check(bool(res and res.get("err")), "400 带原因")

    # ================================================================ ④ SSE 流式
    st, res, _ = http_json(f"{BASE}/api/chat", token=TOKEN, method="POST",
                            body={"prompt": ""})
    check(st == 400, f"空 prompt → 400（实际 {st}）")

    events, elapsed = _sse_collect(BASE, TOKEN, "你好呀")
    kinds = [e.get("type") for e in events]
    check("line" in kinds, f"SSE 里有 line 事件（收到 {kinds}）")
    check("done" in kinds, "SSE 以 done 收尾")
    check(kinds.index("line") < kinds.index("done"), "line 先于 done")
    lines = [e for e in events if e.get("type") == "line"]
    check(len(lines) >= len(STREAM_LINES),
          f"收到多行内容（{len(lines)} 行，期望 ≥{len(STREAM_LINES)}）")
    check(all("t7-fake" in (e.get("text") or "") for e in lines),
          "每行都带后端 id 前缀（[t7-fake] …）")

    # 时间差才是「真是流式」的证据：攒完再发的话两者会几乎同时到达。
    check(elapsed is not None and elapsed >= STREAM_DELAY * 0.5,
          f"首个 line 与 done 之间拉开了时间差（{elapsed:.3f}s ≥ "
          f"{STREAM_DELAY * 0.5:.3f}s）——不是攒完再一次性发")

    # ================================================================ ⑥ 人格
    check(_seen["system"] and "T7小鲸" in _seen["system"],
          "system prompt 由 persona_card.build_system 拼出（含卡名）")
    check(_seen["prompt"] == "你好呀", "prompt 原样传到后端")

    # ================================================================ ⑦ 前端
    ui = os.path.join(ROOT, "ui")
    for name in ("index.html", "app.js", "app.css"):
        p = os.path.join(ui, name)
        check(os.path.exists(p) and os.path.getsize(p) > 0, f"前端资源存在且非空：ui/{name}")

    for name in ("index.html", "app.js", "app.css"):
        p = os.path.join(ui, name)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            src = f.read()
        code = _strip_comments(src)
        ext = re.findall(r"""(?:src|href)\s*=\s*["'](https?:)?//[^"']+""", code)
        check(not ext, f"ui/{name} 无 CDN/外链资源（发现 {ext or '无'}）")
        check("//cdn" not in code and "unpkg" not in code and "jsdelivr" not in code,
              f"ui/{name} 代码里无常见 CDN 域名")
        # 只查「代码形态」不查注释措辞：注释里写「不装 npm 包」不该算违规。
        check(not re.search(r"\brequire\s*\(", code) and
              not re.search(r"""^\s*import\s+[\w{*]""", code, re.M),
              f"ui/{name} 没有 require()/import 引入的打包器产物")

    with open(os.path.join(ui, "app.js"), encoding="utf-8") as f:
        js = f.read()
    js_code = _strip_comments(js)
    check("X-Fleet-Token" in js, "app.js 会带 X-Fleet-Token 头")
    check('"X-Fleet-Token": TOKEN' in js_code,
          "app.js 原样把令牌放进请求头（没被加工）")
    check("location.hash" in js, "app.js 从 URL 片段取令牌（不进日志）")
    urls = re.findall(r"""["'](/[^"']*)["']""", js_code)
    bad = [u for u in urls if u.startswith("//") or "://" in u]
    check(not bad, f"app.js 里的 URL 全是本地相对路径（发现 {bad or '无'}）")
    # 剥掉注释再查：注释里写「不要引 CDN」不算引了 CDN，代码里引了才算。
    check(not re.search(r"""https?://""", js_code),
          "app.js 代码里没有绝对 URL（注释不算）")
    # 流式解析必须在（这是「边收边渲染」的代码形态证据）
    check("getReader" in js_code and '"data:"' in js_code,
          "app.js 用 fetch + getReader 逐块解 SSE（不是一次性拿）")
    check("text/event-stream" in open(os.path.join(ROOT, "fleet", "gui", "server.py"),
                                     encoding="utf-8").read(),
          "后端声明的是 text/event-stream")

    with open(os.path.join(ui, "index.html"), encoding="utf-8") as f:
        html = f.read()
    check("/app.js" in html and "/app.css" in html, "index.html 引用本地 app.js / app.css")
    check("<script" in html and "src=\"/app.js\"" in html, "index.html 用本地脚本")

    # id 契约：app.js 里 $("xxx") 拿到的 id 必须在 index.html 里存在。
    # 少一个 id 就是「界面某个角落永远不动」，是最难肉眼发现的那种坏。
    html_ids = set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html))
    used_ids = set(re.findall(r"""\$\(\s*["']([^"']+)["']\s*\)""", js))
    missing = sorted(used_ids - html_ids)
    check(not missing, f"app.js 用到的 id 在 index.html 里都有（缺 {missing or '无'}）")

    # JS 语法：node 在就查一遍（在不在都无所谓，查了更严）。
    import shutil
    import subprocess
    _node = shutil.which("node")
    if _node:
        try:
            r = subprocess.run([_node, "--check", os.path.join(ui, "app.js")],
                               capture_output=True, timeout=30)
            check(r.returncode == 0,
                  f"app.js 语法通过 node --check（{r.stderr.decode('utf-8', 'replace')[:200]}）")
        except Exception as e:
            check(False, f"node --check 跑不起来: {type(e).__name__}: {e}")
        # 前端运行时冒烟：假 DOM 真跑一遍 app.js，验令牌真的流到了请求头
        try:
            r = subprocess.run([_node, os.path.join(ROOT, "evals", "t7_front_smoke.js")],
                               capture_output=True, timeout=60)
            out = r.stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                if line.strip().startswith("ok "):
                    print("  ok", line.strip()[3:])
                elif line.strip().startswith("x "):
                    print("  x ", line.strip()[2:])
            check(r.returncode == 0, "前端运行时冒烟通过（假 DOM 真跑 app.js）")
        except Exception as e:
            check(False, f"前端运行时冒烟跑不起来: {type(e).__name__}: {e}")
    else:
        print("  -- 没装 node，跳过 JS 语法检查与前端冒烟（本机没 node 不算失败）")

    # ================================================================ ⑧ 静态纪律
    with open(os.path.join(ROOT, "fleet", "gui", "server.py"), encoding="utf-8") as f:
        srv_src = f.read()
    # 只查「代码里的字符串字面量」，不查注释/文档字符串的措辞——
    # 文档里写「不绑 0.0.0.0」是在讲纪律，不是在绑它。
    srv_literals = _code_strings(srv_src)
    check("127.0.0.1" in srv_literals, "server.py 绑 127.0.0.1")
    check("0.0.0.0" not in srv_literals, "server.py 代码里没有 0.0.0.0")
    check("secrets" in srv_src and "token_urlsafe" in srv_src,
          "server.py 用 secrets 生成一次性令牌")
    check("compare_digest" in srv_src, "server.py 用常数时间比对令牌")
    check("registry.call" in srv_src or ".call(" in srv_src,
          "server.py 走 registry.call（前端不直接碰设备）")
    check("ThreadingHTTPServer" in srv_src, "server.py 用 stdlib http.server（没引 Flask）")
    check("flask" not in srv_src.lower() and "fastapi" not in srv_src.lower(),
          "server.py 没有 Flask/FastAPI 依赖")
    check(not any("local.env" in s for s in srv_literals),
          "server.py 代码里不读 local.env（令牌只存内存）")

    # token 绝不入库
    _gitignore = ""
    gi = os.path.join(ROOT, ".gitignore")
    if os.path.exists(gi):
        with open(gi, encoding="utf-8") as f:
            _gitignore = f.read()
    check("local.env" in _gitignore, "local.env 在 .gitignore 里（凭据不入库）")

    # 静态资源不许有目录穿越
    st, res, _ = http_json(f"{BASE}/../local.env", token=TOKEN)
    check(st in (400, 404), f"目录穿越被挡住（实际 {st}）")

finally:
    srv.stop()
    try:
        for root, dirs, files in os.walk(_tmp, topdown=False):
            for fn in files:
                os.remove(os.path.join(root, fn))
            for dn in dirs:
                os.rmdir(os.path.join(root, dn))
        os.rmdir(_tmp)
    except OSError:
        pass

print("t7 gui_backend:", "PASS" if not FAIL else f"FAIL ({len(FAIL)} 项)", flush=True)
sys.exit(1 if FAIL else 0)
