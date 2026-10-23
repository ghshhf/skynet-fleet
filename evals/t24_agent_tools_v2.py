"""t24：工具面 v2 —— Sky MCP over HTTP + 权限三档 + 多轮缰绳。

一次真事故一个脚本。本脚本防的是这三类回归：

① **两条传输同一份派发逻辑**（2026-10-24 新增 HTTP 传输）。
   stdio 是「一行一个 JSON」，HTTP 是「一个 body 一个 JSON」。
   若以后有人只改 stdio 不改 HTTP（或反之），协议行为就会分叉——
   这类分叉在真机上表现为「手机连上了但工具永远空」，极难定位。
   所以这里断言：同一请求走两条路径，**响应必须逐字节一致**。

② **权限是白名单制，且在执行层再查一次**。
   模型说什么不代表就该让它做什么：决策层被诱导/幻觉出一个未登记的工具名时，
   执行层必须 forbid，而不是「试试看」。未知工具 = forbid（不是 auto）。

③ **多轮的缰绳在代码里，不在模型嘴里**。
   BFCL V4：4B 多轮 22%、本机 1.7B 多轮必崩——小模型没有「够了」的自觉。
   所以：轮次硬上限 2、总预算 60s、同工具不重复、首轮失败不再往下试。
   这四条任何一条被改松，模型就会开始自我循环并胡说。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.registry import Capability
from fleet.mcp import server as S
import fleet.agent_tools as AT


FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


class FakeResult:
    def __init__(self, ok, data=None, err="", code="ok"):
        self.ok = ok
        self.data = data or {}
        self.err = err
        self.code = code

    def get(self, k, d=None):
        if k == "data":
            return self.data
        return self.data.get(k, d)

    def brief(self):
        return self.err or "ok"


class FakeReg:
    def __init__(self):
        self._caps = [Capability("ai.list", "列出后端", side_effect=False)]

    def capabilities(self):
        return list(self._caps)

    def call(self, cap, device="", args=None, gate=None):
        if cap == "ai.list":
            return FakeResult(True, {"backends": [{"id": "pollinations"}]})
        return FakeResult(False, err="no such", code="unsupported")


# ---------------------------------------------------------------- ① HTTP 传输
print("① Sky MCP over HTTP：两条传输同一份派发逻辑")
reg = FakeReg()
req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
       "params": {"name": "ai_list", "arguments": {}}}
via_stdio = S.MCPServer(registry=reg).handle(req)
via_http = S.handle_json(req, registry=reg)
check(via_stdio == via_http,
      "同一请求 stdio 与 HTTP 响应一致（协议只有一份）")

batch = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
         {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
out = S.handle_json(batch, registry=reg)
check(isinstance(out, list) and len(out) == 2 and
      out[0]["result"]["protocolVersion"] == S.PROTOCOL_VERSION and
      any(t["name"] == "ai_list" for t in out[1]["result"]["tools"]),
      "批请求逐条处理且顺序不变")

note = [{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}]
check(S.handle_json(note, registry=reg) is None,
      "批里全是通知 → None（HTTP 层据此回 204）")

bad = S.handle_json({"jsonrpc": "1.0", "id": 3, "method": "tools/list"})
check(bad["error"]["code"] == S.INVALID_REQUEST,
      "jsonrpc 版本不对 → -32600（不静默放行）")

# ---------------------------------------------------------------- ② 权限三档
print("② 权限三档：白名单制 + 执行层复查")
check(AT.permission_for("shell_exec") == AT.PERM_FORBID,
      "未知工具 → forbid（不是 auto，不是默认放行）")
check(AT.permission_for("app_launch") == AT.PERM_ASK, "app_launch 默认 ask")
check(AT.permission_for("notify_read") == AT.PERM_AUTO, "只读工具默认 auto")

custom = AT.load_tiers("app_launch=auto,wifi_survey=forbid,bogus=auto,wifi_survey=break")
check(custom["app_launch"] == AT.PERM_AUTO, "环境变量可提权（显式配置才生效）")
check(custom["wifi_survey"] == AT.PERM_FORBID,
      "非法档位（=break）被忽略，前一个合法值 forbid 保持生效")

# 未登记工具即使被外部 tiers 说成 auto，也不该进执行——
# 白名单是「工具名」这一层的，不由档位层开口子。
label, text, ok = AT.execute("shell_exec", "rm -rf /", lambda c, a: None,
                             tiers={"shell_exec": AT.PERM_AUTO})
check((not ok) and "forbid" in text, "未登记工具即使被配成 auto 也禁（名字不认）")
label, text, ok = AT.execute("shell_exec", "rm -rf /", lambda c, a: None)
check(not ok, "forbid 工具根本不进执行（caller 不会被调用）")

AT.execute("notify_read", "", lambda c, a: {"ok": True, "code": "ok",
                                                  "data": {"count": 0}})
check(os.path.exists(AT.AUDIT_PATH),
      "审计日志落盘（~/.fleet/tool_audit.log）")

# ---------------------------------------------------------------- ③ 多轮缰绳
print("③ 多轮缰绳：轮次/预算/去重/失败即止")


class Scripted:
    """假决策模型：按顺序回，并记下每次被问了什么。"""
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def __call__(self, question, url=None, timeout=None):
        self.seen.append(question)
        return self.script.pop(0) if self.script else {"tool": "none"}


good_notify = {"ok": True, "code": "ok", "data": {"count": 1, "notifications": [
    {"title": "微信", "text": "在吗", "when": "刚刚"}]}}
good_thermal = {"ok": True, "code": "ok", "data": {"battery": {"level": 70}}}
bad_call = {"ok": False, "code": "unavailable", "err": "adb 无设备", "data": {}}

_od = AT.decide


def _use(script):
    sd = Scripted(script)
    AT.decide = sd
    return sd


try:
    # (a) 复合句 → 两轮各拿一个工具，且第二轮只喂后半句
    sd = _use([{"tool": "notify_read", "arg": ""},
               {"tool": "device_status", "arg": ""}])
    got = AT.run_tools("看看通知，顺便告诉我电量",
                       lambda c, a: good_notify if c == "notify.read" else good_thermal)
    check(len(got) == 2 and got[0]["round"] == 1 and got[1]["round"] == 2,
          "复合句两轮各拿一个不同工具")
    check(len(sd.seen) == 2 and "顺便" in sd.seen[1] and "看看通知" not in sd.seen[1],
          "第二轮只把后半句喂给决策（不塞对话史——小模型见长上下文就崩）")

    # (b) 单句 → 不问第二轮（真机实测：问了 10/10 回 none，白搭 3.6s）
    sd2 = _use([{"tool": "notify_read", "arg": ""}])
    AT.run_tools("看看通知", lambda c, a: good_notify)
    check(len(sd2.seen) == 1, "单句不问第二轮（规则门挡住，省一次决策）")

    # (c) 首轮失败即止
    sd3 = _use([{"tool": "notify_read", "arg": ""}])
    got2 = AT.run_tools("看看通知", lambda c, a: bad_call)
    check(len(got2) == 1 and not got2[0]["ok"], "首轮失败即止（不拿失败结果再赌一轮）")

    # (d) 模型重复要同一个工具 → 收手
    sd4 = _use([{"tool": "notify_read", "arg": ""}] * 3)
    got3 = AT.run_tools("看看通知，再读一遍通知",
                        lambda c, a: good_notify, max_rounds=9)
    check(len(got3) == 1, "模型重复要同一个工具 → 收手（不信模型的自觉）")

    # (e) 预算：0 预算一轮都不该跑
    sd5 = _use([{"tool": "notify_read", "arg": ""}] * 3)
    got4 = AT.run_tools("看看通知", lambda c, a: good_notify, budget_s=0)
    check(len(got4) == 0, "预算耗尽立刻收手（0 预算 = 不跑工具）")
finally:
    AT.decide = _od

# 拆句本身：连接词要留在后半句里（16/16 那套决策对「顺便告诉我电量」本来就稳）
first, second = AT.split_compound("看看通知，顺便告诉我电量")
check(second.startswith("顺便"), "拆句把连接词留在后半句（不切干净，保住语义）")
check(AT.split_compound("看看通知")[1] == "", "单句拆不出后半句 → 不进第二轮")

# ---------------------------------------------------------------- ④ 资料块
print("④ 资料块与限长")
blk = AT.render_blocks([{"tool": "notify_read", "label": "notify.read",
                         "text": "共 1 条通知：微信｜在吗"}])
check("工具 notify.read 的实时结果" in blk and "标题不算事实" in blk,
      "多轮结果拼成资料块且带引用纪律")
check(len(AT.execute("notify_read", "", lambda c, a: {
    "ok": True, "code": "ok",
    "data": {"notifications": [{"title": "x" * 5000}]}})[1]) <= AT.MAX_RESULT_CHARS + 60,
      "超长结果被限到 1200 字符以内（31 万字撑爆上下文的事故不再可能）")

# ---------------------------------------------------------------- ⑤ 真 HTTP 端到端
# 前面四组都是纯函数，这一组**真起一个 FleetServer 走一遍 HTTP**——
# 「MCP 挂上了」和「MCP 能被手机连上」是两件事，只有真起服务才能证明后者。
print("⑤ Sky MCP 真 HTTP 端到端（起临时 FleetServer）")
import tempfile
import urllib.request
import urllib.error

from fleet.catalog import build as _build
import fleet.gui.server as GS

_tmp = tempfile.mkdtemp(prefix="t24_")
try:
    _reg = _build(path=os.path.join(_tmp, "devices.json"), with_nodes=False)
    _srv = GS.FleetServer(registry=_reg)
    _srv.start()
    BASE, TOKEN = _srv.base_url, _srv.token

    def post_mcp(payload, token=TOKEN):
        data = json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Fleet-Token"] = token
        req = urllib.request.Request(BASE + "/api/mcp", data=data, headers=h,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    st, js = post_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      token=None)
    check(st == 401, f"不带凭据调 /api/mcp → 401（实际 {st}）")

    st, js = post_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18",
                                  "clientInfo": {"name": "phone"}}})
    check(st == 200 and js["result"]["protocolVersion"] == S.PROTOCOL_VERSION,
          "initialize 走通（HTTP 传输与 stdio 同一个 server 对象）")

    st, js = post_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in js["result"]["tools"]}
    check(st == 200 and len(names) > 0, f"tools/list 列出 {len(names)} 个工具")
    check("pcctl_sysinfo" in names, "只读的 pcctl.sysinfo 对 MCP 客户端可见")
    check("pcctl_proc_kill" not in names,
          "动手的 pcctl.proc.kill 默认不导出（手机最多只能看）")

    st, js = post_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "pcctl_sysinfo", "arguments": {}}})
    ok_call = js["result"].get("isError") is not True
    check(st == 200 and ok_call, "只读能力经 HTTP 真调通（sysinfo 有数据回来）")

    st, js = post_mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "pcctl_proc_kill", "arguments": {}}})
    txt = js["result"]["content"][0]["text"]
    check(js["result"].get("isError") is True and "没有导出" in txt,
          "未导出的动手能力 → isError 且说清「没导出」（不假装成功）")

    # 通知：HTTP 层用 204 表达「收到了但按 JSON-RPC 规矩不回」
    req = urllib.request.Request(
        BASE + "/api/mcp",
        data=json.dumps([{"jsonrpc": "2.0", "method":
                          "notifications/initialized"}]).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Fleet-Token": TOKEN}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        check(r.status == 204, f"纯通知 → 204（实际 {r.status}）")
    _srv.stop()
except Exception as e:
    check(False, f"HTTP 端到端起服务失败：{type(e).__name__}: {e}")

print("t24 agent_tools_v2:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
