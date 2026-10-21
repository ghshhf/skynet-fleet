#!/usr/bin/env python
"""门禁 t14：MCP 出口 + 技能装载 + 渲染能力。

**这个门禁钉的是四件真事**：

  ① **MCP 必须按规范应答，不许「差不多」**。
     协议版本（2025-06-18）、三个方法、JSON-RPC 错误码，
     一个都不能错。错了 = 外部客户端（Claude Desktop / Cursor）接不上。

  ② **动手能力在 MCP 路径上照样过闸门**（本仓红线 3）。
     stdio 进程没有 GUI 弹窗可等 ⇒ 等不到人就是没人授权。
     所以：默认**不导出**副作用能力；即使 include_unsafe=True，
     没给 gate 时注册表自己 fail-closed 拒绝。
     ⚠ 这条最容易被「为了跑通」而放松 ⇒ 单独钉死。

  ③ **不许静默失败**。
     「压根没这个工具」和「这个工具有但因为是动手的没导出」是两种情况，
     处置完全不同，混成一句「没有这个工具」就是坑人。
     未知方法回 -32601、脏行回 -32700 且**不许退出**。

  ④ **技能装载不许假造**。
     SKILL.md 的 frontmatter 坏了要带**行号**报错（不然用户不知道改哪行）；
     目录里没 SKILL.md 就不是技能。技能是「怎么做」，能力是「能做什么」，
     两者不许混——混了就会出现「技能要不要审批」这种没法答的问题。

渲染能力这一层：
  · 引擎在不在都要**如实**（不在 ⇒ probe 说清楚装法，不许假装在线）；
  · 真渲染单帧是**有引擎就跑、没引擎就跳过并明说**——
    引擎没装不该让门禁变红（那是环境问题不是代码问题），
    但引擎在而单帧渲染炸了 = 代码坏了，必须红。

**全离线**：不起真网络、不调真模型。渲染只用本地引擎与本地 ffmpeg。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def skip(msg):
    print("  --", msg)
    SKIP.append(msg)


def _dec(b):
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp936", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ================================================================ ① MCP 协议

print("== MCP 协议 ==")
from fleet.mcp.server import (MCPServer, PROTOCOL_VERSION, PARSE_ERROR,
                               METHOD_NOT_FOUND, INVALID_REQUEST, INVALID_PARAMS)
from fleet.mcp.schema import to_mcp_name, from_mcp_name, is_valid_mcp_name, ToolIndex
from fleet.core import Capability, Param, Registry, BaseDevice


class _Fake(BaseDevice):
    kind = "fake"
    via = "local"

    def __init__(self, name="fake:up", caps=("probe.echo", "probe.write", "probe.burn"), online=True):
        super().__init__(name, via="local")
        self._caps = tuple(caps)
        self._online = online

    def probe(self):
        return (self._online, "" if self._online else "假设备下线了")

    def caps(self):
        return self._caps

    def invoke(self, cap, args=None):
        from fleet.core import DeviceResult as R
        if cap == "probe.echo":
            return R.success(cap, self.name, via=self.via, data={"echo": (args or {}).get("v")})
        if cap == "probe.write":
            return R.success(cap, self.name, via=self.via, data={"wrote": True})
        if cap == "probe.burn":
            return R.success(cap, self.name, via=self.via, data={"burned": True})
        return R.error(cap, "这个假设备不会这个", device=self.name, via=self.via)


def _fake_reg(**kw):
    reg = Registry(path=os.path.join(tempfile.gettempdir(), "fleet_t14_fake.json"), ttl=0, **kw)
    reg.register(Capability("probe.echo", "回声", params=(Param("v", "str"),)))
    reg.register(Capability("probe.write", "动手", side_effect=True, approval="ask"))
    reg.register(Capability("probe.burn", "烧掉", side_effect=True, destructive=True))
    reg.add(_Fake())
    return reg


# --- initialize ---
srv = MCPServer(_fake_reg())
resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"clientInfo": {"name": "t14"}}})
check(resp.get("result", {}).get("protocolVersion") == PROTOCOL_VERSION,
      "initialize 报协议版本 %s" % PROTOCOL_VERSION)
check(resp.get("result", {}).get("serverInfo", {}).get("name") == "device-fleet",
      "initialize 报自己的名字")
check("tools" in resp.get("result", {}).get("capabilities", {}),
      "initialize 声明 tools 能力")
check(resp.get("jsonrpc") == "2.0" and resp.get("id") == 1, "响应带 jsonrpc 与 id")

# --- tools/list ---
resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = resp.get("result", {}).get("tools", [])
names = [t["name"] for t in tools]
check("probe_echo" in names, "tools/list 里有只读工具 probe_echo")
check("probe_write" not in names, "默认不导出副作用工具 probe_write")
check("probe_burn" not in names, "默认不导出破坏性工具 probe_burn")
for t in tools:
    check_t = t.get("name")
    if not is_valid_mcp_name(check_t):
        check(False, "工具名不符合 MCP 规范: %r" % check_t)
        break
else:
    check(True, "所有工具名符合 ^[a-zA-Z0-9_-]{1,64}$")
echo_t = [t for t in tools if t["name"] == "probe_echo"][0]
check("inputSchema" in echo_t and echo_t["inputSchema"].get("type") == "object",
      "工具带 JSON Schema 输入")
check(echo_t.get("annotations", {}).get("readOnlyHint") is True,
      "只读能力标 readOnlyHint=true")

# --- tools/call（只读，真调通）---
resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "probe_echo", "arguments": {"v": "hi"}}})
res = resp.get("result", {})
check(res.get("isError") is not True, "tools/call 只读能力成功")
check("hi" in json.dumps(res, ensure_ascii=False), "tools/call 返回了真数据")

# ================================================================ ② 闸门

print("== 动手能力过闸门 ==")
# 默认不导出
resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "probe_write", "arguments": {}}})
txt = json.dumps(resp.get("result", {}), ensure_ascii=False)
check(resp.get("result", {}).get("isError") is True, "调用没导出的动手工具 ⇒ 报错不是假装成功")
check("没有导出" in txt, "错误文案说清「存在但没导出」而不是「没这个工具」")
check("probe_write" in txt, "错误文案点名是哪个能力")

# 真压根没有
resp = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "nope_nope", "arguments": {}}})
txt = json.dumps(resp.get("result", {}), ensure_ascii=False)
check(resp.get("result", {}).get("isError") is True, "调用不存在的工具 ⇒ 报错")
check("没有这个工具" in txt, "错误文案说清「压根没这个工具」")
check("没有导出" not in txt, "两种情况不混成一句话")

# include_unsafe=True 也没 gate ⇒ 注册表 fail-closed 拒绝
srv_unsafe = MCPServer(_fake_reg(), include_unsafe=True, gate=None)
names = [t["name"] for t in srv_unsafe.tools()["tools"]]
check("probe_write" in names, "include_unsafe=True 时副作用工具才导出")
resp = srv_unsafe.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                          "params": {"name": "probe_write", "arguments": {}}})
res = resp.get("result", {})
check(res.get("isError") is True, "include_unsafe=True 但没 gate ⇒ 仍然拒绝（fail-closed）")
check("闸门" in json.dumps(res, ensure_ascii=False), "拒绝时带原因说明要审批闸门")

# 给了放行闸门 ⇒ 真的过
srv_ok = MCPServer(_fake_reg(), include_unsafe=True,
                   gate=lambda cap, dev, args, level: (True, "门禁放行"))
resp = srv_ok.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "probe_write", "arguments": {}}})
check(resp.get("result", {}).get("isError") is not True, "给了放行闸门 ⇒ 动手能力真能过")

# 破坏性能力要 level=2（双确认）
seen = {}


def _gate(cap, dev, args, level):
    seen["level"] = level
    return (True, "ok")


srv_lv = MCPServer(_fake_reg(), include_unsafe=True, gate=_gate)
srv_lv.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "probe_burn", "arguments": {}}})
check(seen.get("level") == 2, "破坏性能力 ⇒ 闸门收到 level=2（要双确认）")

# ================================================================ ③ 错误码

print("== JSON-RPC 错误码 ==")
resp = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "teleport"})
check(resp.get("error", {}).get("code") == METHOD_NOT_FOUND, "未知方法 ⇒ -32601")
resp = srv.handle({"jsonrpc": "1.0", "id": 10, "method": "tools/list"})
check(resp.get("error", {}).get("code") == INVALID_REQUEST, "jsonrpc 版本错 ⇒ -32600")
resp = srv.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {}})
check(resp.get("error", {}).get("code") == INVALID_PARAMS, "tools/call 缺 name ⇒ -32602")
resp = srv.handle({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                    "params": {"name": "probe_echo", "arguments": "not-an-object"}})
check(resp.get("error", {}).get("code") == INVALID_PARAMS, "arguments 不是对象 ⇒ -32602")
resp = srv.handle({"jsonrpc": "2.0", "id": 13, "method": "notifications/initialized"})
check(resp is None, "通知（没有 id）不回复")

# stdio：脏行回 -32700 但不退出
inp = "这不是 JSON\n" + json.dumps({"jsonrpc": "2.0", "id": 14, "method": "tools/list"}) + "\n"
r = subprocess.run([sys.executable, "-m", "fleet.mcp.server"], input=inp,
                   capture_output=True, text=True, cwd=ROOT, timeout=120)
lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
check(len(lines) >= 2, "脏行之后还能继续处理后续请求（一条坏数据不该让 server 死）")
if lines:
    try:
        first = json.loads(lines[0])
        check(first.get("error", {}).get("code") == PARSE_ERROR, "脏行 ⇒ -32700")
    except Exception as e:
        check(False, "脏行的响应不是合法 JSON: %s" % e)

# ================================================================ ④ 工具名映射

print("== 工具名映射 ==")
check(to_mcp_name("pc.state") == "pc_state", "点号 → 下划线")
check(from_mcp_name("pc_state") == "pc.state", "下划线 → 点号（可逆的那部分）")
check(is_valid_mcp_name("pc_state") and not is_valid_mcp_name("pc.state"),
      "合法名校验认得下划线不认得点号")
check(is_valid_mcp_name("a" * 64) and not is_valid_mcp_name("a" * 65), "工具名长度上限 64")

# 撞名必须当场炸（a.b 与 a_b 都映射成 a_b）
# 注意：冲突是在**构造索引**时炸，不是 resolve 时——早先版本把它推迟到
# resolve，等于「第一次调用才炸」，已经建好的索引带着雷。
try:
    ToolIndex([Capability("a.b", "x"), Capability("a_b", "y")])
    check(False, "撞名的工具名居然没报错")
except ValueError as e:
    check("冲突" in str(e), "撞名当场抛错并说清冲突")

# ================================================================ ⑤ 技能装载

print("== 技能装载 ==")
from fleet.skills import (parse_frontmatter, load_skill, list_skills, scan_dir,
                          SkillError)

meta, body = parse_frontmatter("---\nname: demo\ndescription: 一个演示技能\n---\n正文", "t")
check(meta.get("name") == "demo", "frontmatter 解析出 name")
check(meta.get("description") == "一个演示技能", "frontmatter 解析出 description")
check(body.strip() == "正文", "frontmatter 之后的正文被分离出来")

try:
    parse_frontmatter("---\nname: x\ndescription: y\n", "t")
    check(False, "frontmatter 没闭合居然没报错")
except SkillError as e:
    check("闭合" in str(e), "没闭合 ⇒ 报错说清「没有闭合」")

try:
    parse_frontmatter("---\nname: x\nthis line is broken\n---\n", "t")
    check(False, "坏行居然没报错")
except SkillError as e:
    check("行" in str(e), "坏行 ⇒ 报错带行号")

# 目录里没 SKILL.md 就不是技能
tmp = tempfile.mkdtemp(prefix="fleet_t14_sk_")
os.makedirs(os.path.join(tmp, "not_a_skill"), exist_ok=True)
with open(os.path.join(tmp, "not_a_skill", "readme.md"), "w", encoding="utf-8") as f:
    f.write("x")
found = scan_dir(tmp)
check(len(found) == 0, "没有 SKILL.md 的目录不算技能")

# 有 SKILL.md 就是
ok_dir = os.path.join(tmp, "good_skill")
os.makedirs(ok_dir, exist_ok=True)
with open(os.path.join(ok_dir, "SKILL.md"), "w", encoding="utf-8") as f:
    f.write("---\nname: good_skill\ndescription: 好技能\n---\n\n# 正文\n")
found = scan_dir(tmp)
check(len(found) == 1 and found[0]["name"] == "good_skill", "有 SKILL.md 的目录被识别为技能")
sk = load_skill(os.path.join(ok_dir, "SKILL.md"))
check(sk["body_len"] > 0, "技能正文被读出来（body_len>0）")
check(sk["source"] in ("repo", "user"), "技能带来源标记")

# 真机上应该有真技能（不许造假卡）
real = list_skills()
check(len(real) > 0, "本机扫到真技能 %d 个" % len(real))
check(all(s.get("name") for s in real), "每个技能都有名字")

# ================================================================ ⑥ 渲染能力

print("== 渲染能力 ==")
from fleet.kinds.render import Render, engine_dir, ffmpeg_path

r = Render()
ok, reason = r.probe()
if ok:
    check(True, "渲染引擎就位：%s" % engine_dir())
else:
    check(True, "渲染引擎没装（按环境问题处理，不断言为代码坏）：%s" % reason[:80])
    skip("渲染真跑（引擎没装）")
    check(ffmpeg_path() != "" or True, "ffmpeg 查找不崩")

caps = r.caps()
check("render.styles" in caps and "render.frame" in caps and "render.reel" in caps,
      "渲染设备声明三项能力")

# 引擎不在时，调用必须带原因，不许崩
if not ok:
    res = r.invoke("render.styles", {})
    check(not res.ok and bool(res.err), "引擎没装 ⇒ 调能力返回带原因的失败")

# 能力已登记进注册表
from fleet.catalog import build, CAPABILITIES
reg = build(with_nodes=False)
cap_names = [c.name for c in reg.capabilities()]
check("render.styles" in cap_names, "render.styles 已登记进注册表")
check("render.frame" in cap_names, "render.frame 已登记进注册表")
check("render.reel" in cap_names, "render.reel 已登记进注册表")

# render.reel 必须是要审批的（写文件）
reel_cap = [c for c in CAPABILITIES if c.name == "render.reel"][0]
check(reel_cap.side_effect and reel_cap.approval == "ask",
      "render.reel 是副作用且要审批（写 MP4 ⇒ 过闸门）")
frame_cap = [c for c in CAPABILITIES if c.name == "render.frame"][0]
check(frame_cap.side_effect, "render.frame 也算副作用（写 PNG）")
styles_cap = [c for c in CAPABILITIES if c.name == "render.styles"][0]
check(not styles_cap.side_effect, "render.styles 是只读")

# 没 gate ⇒ render.reel 被拒（fail-closed）
reg_nogate = build(with_nodes=False)
res = reg_nogate.call("render.reel", device="render:local", args={"outdir": "D:/tmp/x"})
check(not res.ok and "闸门" in (res.err or ""), "没闸门 ⇒ render.reel 被拒（fail-closed）")

# MCP 路径上 render.frame 也不该默认导出（副作用）
srv_real = MCPServer(reg)
tnames = [t["name"] for t in srv_real.tools()["tools"]]
check("render_styles" in tnames, "MCP 工具表里有只读的 render_styles")
check("render_frame" not in tnames, "MCP 工具表里没有副作用的 render_frame")
check("render_reel" not in tnames, "MCP 工具表里没有审批档的 render_reel")
check("render_html" not in tnames, "MCP 工具表里没有审批档的 render_html")

# 真渲染单帧（引擎在才跑）
if ok:
    outdir = tempfile.mkdtemp(prefix="fleet_t14_reel_")
    res = r.invoke("render.frame", {"at": "1:1.20", "workdir": outdir, "keep": True})
    if res.ok:
        p = res.data.get("path", "")
        check(os.path.isfile(p) and os.path.getsize(p) > 1000,
              "render.frame 真渲出 PNG（%d 字节）" % (os.path.getsize(p) if os.path.isfile(p) else 0))
        try:
            from PIL import Image
            with Image.open(p) as im:
                check(im.size[0] >= 1280, "单帧尺寸 %dx%d" % im.size)
        except Exception:
            pass
    else:
        # 引擎在而单帧炸了 = 代码坏了，必须红
        check(False, "render.frame 真渲染失败：%s" % (res.err or "")[:300])

# ================================================================ 结论

print("")
if SKIP:
    print("跳过 %d 项：%s" % (len(SKIP), "；".join(SKIP)))
if FAIL:
    print("FAILED %d 项：" % len(FAIL))
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("t14 MCP + 技能 + 渲染 全绿")
