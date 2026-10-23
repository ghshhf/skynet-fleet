"""手机算力节点 + 人格记忆包 门禁（离线，不碰真设备）。

验的是这一轮的结论能不能被机器重复验证：
  「人格 = 记忆的子集 + 渲染规则；权重只装知识」
  「能力在设备上，PC 只是发问的人」

所以这里证明四件事：
  ① 六项能力真的进了注册表（不是只在文档里）；
  ② 端点不通时如实说「不通 + 怎么通」，不假装在线；
  ③ 人格包在离线状态下也能列出/渲染，且 identity/value 不会被预算裁掉；
  ④ 写手机的能力（sync / ask）默认不导出给外部 AI 客户端。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


TMP = tempfile.mkdtemp(prefix="phone_node_")
os.environ["FLEET_DEVICES"] = os.path.join(TMP, "devices.json")
os.environ["FLEET_MEMORY"] = "0"
# 指到一个必然不通的端口：门禁不该依赖手机此刻是否连着
os.environ["PHONE_LLM_BASE"] = "http://127.0.0.1:9"

from fleet import catalog as CAT                       # noqa: E402
from fleet.kinds.phone_node import PhoneNode, CAPS      # noqa: E402
from fleet import memory_pack as M                      # noqa: E402

# ================================================================ ① 注册表
print("① 能力进了注册表（真 build，不是桩）")
reg = CAT.build(path=os.path.join(TMP, "devices.json"), with_nodes=False)
names = {c.name for c in CAT.CAPABILITIES}
check(set(CAPS) <= names, f"6 项能力都在能力表里（{len(CAPS)} 项）")
node = reg.device("phone:compute") or None
if node is None:
    cands = [d for d in reg.devices() if d.kind == "phone_node"]
    node = cands[0] if cands else None
check(node is not None, "注册表里找得到 phone_node 这台设备")
check(node is not None and set(node.caps()) == set(CAPS), "节点声明的能力与常量一致")
check(node is not None and node.kind == "phone_node" and node.via == "http",
      "kind=phone_node via=http（区别于 adb 那台「被操控」的手机）")

# ================================================================ ② 不通就说清
print("② 端点不通时如实报（不假装在线）")
ok, why = node.probe() if node else (None, "")
check(ok is False, "probe 返回 False")
check("不通" in why and "adb forward" in why, "原因里带「不通」和怎么通（adb forward）")

r = node.invoke("phone.llm.ask", {"ask": "在吗"}) if node else None
check(r is not None and r.code in ("unavailable", "error"),
      "端点不通时调用如实返回 unavailable/error，不返回空答复冒充成功")

# ================================================================ ③ 记忆包离线可用
print("③ 人格包离线可用（不依赖设备）")
r = node.invoke("memory.pack.list", {}) if node else None
packs = [p["name"] for p in (r.data.get("packs") or [])] if r and r.ok else []
check("dafeiyu" in packs and "jishuyuan" in packs, f"能列出人格包：{packs}")

r = node.invoke("memory.pack.render", {"pack": "dafeiyu", "budget": 300}) if node else None
check(r is not None and r.ok, "渲染成功")
data = r.data if r else {}
check(data.get("tokens", 0) <= 300 or data.get("over_budget"),
      "渲染遵守预算（或明确标 over_budget）")
check("我是大肥鱼" in (data.get("text") or ""), "identity 层在预算很紧时仍保留")
check("结论前置" in (data.get("text") or ""), "value 层在预算很紧时仍保留")
check(data.get("dropped", 0) > 0, "被裁掉的条数如实报出")

r = node.invoke("memory.pack.render", {"pack": "不存在的包"}) if node else None
check(r is not None and r.code == "error", "包名不存在 → 明确报错，不是空渲染")

# 同一份文本、两个包，渲染结果必须不同（否则「换包换人」是假的）
p1 = M.Pack.load(os.path.join(CAT.os.path.dirname(CAT.os.path.dirname(
    CAT.os.path.abspath(CAT.__file__))), "personas", "dafeiyu"))
p2 = M.Pack.load(os.path.join(CAT.os.path.dirname(CAT.os.path.dirname(
    CAT.os.path.abspath(CAT.__file__))), "personas", "jishuyuan"))
check(p1.digest() != p2.digest(), "两个人格包指纹不同")
check(p1.render(budget_tokens=400)["text"] != p2.render(budget_tokens=400)["text"],
      "两个人格包渲染出不同的人格文本")

# ================================================================ ④ 副作用不默认导出
print("④ 写手机/占算力的能力默认不导出给外部 AI")
from fleet.mcp.server import MCPServer                 # noqa: E402

srv = MCPServer(registry=reg)
tn = {t["name"] for t in srv.tools()["tools"]}
check("phone_node_status" in tn or "memory_pack_list" in tn,
      "只读能力导出给 MCP 客户端")
check("memory_pack_sync" not in tn and "phone_llm_ask" not in tn,
      "memory.pack.sync / phone.llm.ask 默认不导出（无弹窗环境不能等审批）")
srv_u = MCPServer(registry=reg, include_unsafe=True)
tun = {t["name"] for t in srv_u.tools()["tools"]}
check({"memory_pack_sync", "phone_llm_ask"} <= tun, "--unsafe 才导出")

import capabilities as C                               # noqa: E402

check(C.spec_of("phone.llm.ask")["title"] == "用手机上的人格回答", "说明书标题正确")
check(C.spec_of("mem.sync")["title"] == "增量下发人格包到手机", "别名 mem.sync 可用")
check(C.param_names("phone.llm.ask")[0] == "ask", "phone.llm.ask 首个必填是 ask")

import shutil                                          # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
print("test_phone_node:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
