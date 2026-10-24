"""证据分级 authority——把「读到的」和「猜的」强制分开。

🔴 **这个模块为什么存在**（两个真实误判逼出来的）：
  ① discovery 里一台开 554 的 Windows 被判成摄像头（端口投票），
     实际 mDNS 自报 Microsoft-Windows + iVCam——设备自报远比端口投票可靠；
  ② recall.py 只会跟模型说「只能引用，不要编造」，但**引用物本身没有分级**，
     模型分不清哪句是账本原文、哪句是自己的润色。

⇒ 口头铁律升级成数据结构：每条结论都带 authority 标 + 可引用的证据 id。

**四级（照 netforce `reverse.py` 的形状，不搬代码——license 纪律同
`netforce_skynet.py`：只 import 不复制）**：

| 级 | 含义 | 天网侧的对应物 |
|---|---|---|
| `shipped-artifact` | 实物读到 | 页面真出现的字符串 / 设备自报的 mDNS 名字 |
| `controlled-replay` | 自己跑一遍看到 | `probe()` L4 解析校验通过 / 自测复现 |
| `historical-reference` | 外部已知 | 账本里的记录、搜索引擎给的摘要 |
| `analyst-inference` | 我们猜的 | 端口投票推断的设备类别、OUI 查不到的厂商 |

**最该抄的形状**：证据 id 对 (facet, claim, value, authority) 取 sha256 前
16 位 ⇒ 同一结论同一 id。跨调用、跨进程、跨机器，同一条事实指向同一个 id，
去重与引用才有锚点。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: 四级常量。取值必须是这四个之一——门禁校验，写别的值当场炸。
AUTHORITY = {
    "shipped-artifact": "目标实物里直接读到的（字节/字符串/端点）——最硬，可复核",
    "controlled-replay": "我们自己跑一遍观察到的（探测/抓取/重放）——可复现，但依赖当时环境",
    "historical-reference": "外部已知信息（账本记录/引擎摘要）——需复核，可能已过时",
    "analyst-inference": "我们推断的（端口投票/组合特征）——最软，只当线索不当结论",
}


@dataclass
class Evidence:
    """一条带来源的结论。

    `authority` 是这个类的灵魂：它把「我读到的」和「我猜的」强制分开。
    agent 拿到的结果里只要混进了 inference 却当 shipped 用，就是这个字段没起作用。
    """
    id: str                       # sha256 前 16 位，便于引用与去重
    facet: str                    # 观察面：web.search / web.fetch / discovery.port_vote / ...
    claim: str                    # 结论（人读的）
    value: str                    # 值（截断到 400 字符，避免吃上下文）
    authority: str                # 见 AUTHORITY
    subject: str = ""             # 目标标识（URL / IP / query）
    limitations: list = field(default_factory=list)   # 显式列出「我没看到什么」

    def to_dict(self) -> dict:
        return {"id": self.id, "facet": self.facet, "claim": self.claim,
                "value": self.value, "authority": self.authority,
                "subject": self.subject, "limitations": self.limitations}


def mk_evidence(facet: str, claim: str, value, authority: str,
                subject: str = "", limitations=None) -> Evidence:
    """造一条证据。id 对 (facet, claim, value, authority) 取 sha256 —— 同一结论同一 id。

    `limitations` 照 reverse.py：逼你写「我没看到什么」。报告里最贵的谎言不是
    假话，是**省略**。

    🔴 authority 传了非法值直接 ValueError——不静默容忍。容忍的后果是四级
    悄悄退化成一锅粥，比没有分级更坏。
    """
    if authority not in AUTHORITY:
        raise ValueError(
            f"authority 必须是 {sorted(AUTHORITY)} 之一，收到 {authority!r}")
    v = str(value)[:400]
    eid = hashlib.sha256(
        f"{facet}|{claim}|{v}|{authority}".encode("utf-8", "replace")).hexdigest()[:16]
    return Evidence(id=eid, facet=facet, claim=claim, value=v, authority=authority,
                    subject=subject, limitations=list(limitations or []))


# --------------------------------------------------------------------------
# 判定助手：每个数据源「这东西有多硬」一次说清，别让每个调用方各自猜
# --------------------------------------------------------------------------

def authority_of_hit(rec):
    """搜索结果的一条命中有多硬。

    标题/URL 是搜索引擎结果页里真出现的字符串，但**摘要与相关度是搜索方
    生成的**，且描述的是第三方页面而非我们读到的目标 ⇒ 整体按
    `historical-reference`（外部已知，需复核）。cross 引擎交叉只提高可信度，
    不改变 authority——那是权重，不是来源。
    """
    return "historical-reference"


def authority_of_fetch(rec):
    """取正文的结果有多硬。

    抓成功了 ⇒ 页面 HTML 是目标实物直接读到的字节（`shipped-artifact`）；
    失败了 ⇒ 失败本身是观察，但「为什么失败」多半是我们的推断（墙了？超时？
    链路截断？）⇒ `analyst-inference`，只当线索不当结论。
    """
    return "shipped-artifact" if rec.get("ok") else "analyst-inference"


def authority_of_probe(rec):
    """四级判定的结果有多硬。

    probe 是我们主动跑一遍的探测行为 ⇒ `controlled-replay`（可复现，依赖
    当时环境）。verdict 是 unknown 说明压根没探到东西，此时任何结论都是猜。
    """
    if not rec.get("alive") or rec.get("verdict") == "unknown":
        return "analyst-inference"
    return "controlled-replay"


def authority_of_structure(rec):
    """站结构探测的结果有多硬。

    robots / sitemap 是站点主动发布的实物字节 ⇒ `shipped-artifact`。
    ⚠️ 站点声明 ≠ 真实存在（sitemap 里可以有死链）——那是引用时的事，
    不影响「这行字是站点自己写的」这个事实。
    """
    return "shipped-artifact"


def authority_of_dns(rec):
    """DNS 污染判据的结果有多硬。

    系统解析与 DoH 的对比是我们自己跑出来的 ⇒ `controlled-replay`；
    判 suspicious 是两边无交集的推断 ⇒ 附带的 verdict 是 analyst-inference。
    这里按观察记（可复现），判词另说。
    """
    return "controlled-replay" if rec.get("system") or rec.get("doh") else "analyst-inference"


def category_authority(evidence, cat, scores=None):
    """discovery 的类别结论有多硬——README 口头铁律的代码化。

    铁律原文：「设备自报的型号/服务名远比端口组合可靠」（554 的 Windows
    被判摄像头、5555 被判手机，两个真实误判都因缺这条标注）。

    判定（按证据分桶，不按总分一锅端）：
      · 自报（ssdp/mdns/http scope 的规则）有贡献 ⇒ `shipped-artifact`
        （README 铁律：设备自报远比端口组合可靠 ⇒ **自报出现即压过端口投票**，
        不存在「端口分更高所以听端口的」这个选项）
      · 自报 0 贡献，类别分只来自端口权重/组合特征 ⇒ `analyst-inference`
      · 只有 OUI 厂商命中（外部已知的注册库）⇒ `historical-reference`
      · 网关走路由表，是查出来的不是投出来的 ⇒ `shipped-artifact`

    `scores` 可传 `score_by_source()` 的结果；不传就现算。
    """
    buckets = scores if scores is not None else score_by_source(evidence)
    b = buckets.get(cat, {"self": 0, "ports": 0, "oui": 0})
    if b["self"] > 0:
        return "shipped-artifact"
    if b["ports"] > 0:
        return "analyst-inference"
    if b["oui"] > 0:
        return "historical-reference"
    return "analyst-inference"


def score_by_source(evidence):
    """把每类得分按来源分桶：{"类别": {"self": 自报分, "ports": 端口分, "oui": 厂商分}}。

    与 `discovery.score_categories` 同一套规则，只是不把分数揉成一锅——
    分桶才能回答「这个类别是读到的还是猜的」。
    """
    scores = {}
    blob_all = " ".join(str(evidence.get(k, "")) for k in ("ssdp", "mdns", "http"))
    for cat, pat, w, scope in _category_rules():
        target = blob_all if scope == "all" else str(evidence.get(scope, ""))
        if not target:
            continue
        if _search(pat, target):
            b = scores.setdefault(cat, {"self": 0, "ports": 0, "oui": 0})
            b["self"] += w
    for p in evidence.get("ports", []) or []:
        hit = _port_category().get(p)
        if hit:
            b = scores.setdefault(hit[0], {"self": 0, "ports": 0, "oui": 0})
            b["ports"] += hit[1]
    vendor = str(evidence.get("oui", ""))
    if vendor:
        for cat, w in _vendor_rules(vendor):
            b = scores.setdefault(cat, {"self": 0, "ports": 0, "oui": 0})
            b["oui"] += w
    return scores


# --- 规则表：延迟 import discovery，避免循环依赖（discovery 要 import 本模块）---

def _category_rules():
    import discovery
    return discovery.CATEGORY_RULES


def _port_category():
    import discovery
    return discovery.PORT_CATEGORY


def _vendor_rules(vendor):
    """厂商加分规则（与 discovery.score_categories 同一张表，抽成函数防两份走偏）。"""
    if "Xiaomi" in vendor:
        return [("sensor", 2)]
    if "Espressif" in vendor:
        return [("sensor", 3)]
    if "Raspberry" in vendor:
        return [("hub", 3)]
    if vendor in ("Apple", "Samsung", "Huawei"):
        return [("phone", 1)]
    if "Hikvision" in vendor or "Dahua" in vendor:
        return [("camera", 6)]
    if vendor in ("Midea", "Gree", "Haier"):
        return [("ac", 5)]
    if "TP-Link" in vendor:
        return [("router", 2)]
    return []


def _search(pat, target):
    import re
    return bool(re.search(pat, target, re.I))


def attest(rec, facet, claim, subject, authority, value=None, limitations=None):
    """给一个结果 dict 补上 authority + evidence 两个字段（原地改，返回 rec）。

    所有 web.* 能力与 discovery 设备条目统一走这个出口——字段名统一，
    门禁才好一刀切校验。
    """
    ev = mk_evidence(facet, claim,
                     value if value is not None else claim,
                     authority, subject=subject, limitations=limitations)
    rec["authority"] = authority
    rec["evidence"] = ev.to_dict()
    return rec


def _selftest():
    """离线验证：id 稳定性 / 四级取值合法 / 投票判定 / attest 落字段。"""
    # ① 同一结论两次生成 id 相同
    a = mk_evidence("web.search", "搜索引擎命中", "https://x.com", "historical-reference")
    b = mk_evidence("web.search", "搜索引擎命中", "https://x.com", "historical-reference")
    assert a.id == b.id, "同一结论 id 必须相同"
    # ② 非法 authority 当场炸
    try:
        mk_evidence("f", "c", "v", "made-up")
        raise AssertionError("非法 authority 必须抛 ValueError")
    except ValueError:
        pass
    # ③ 端口投票必须是 analyst-inference；自报必须是 shipped-artifact
    ev_ports = {"ssdp": "", "mdns": "", "http": "", "oui": "", "ports": [554]}
    ev_self = {"ssdp": "HIKVISION DS-2CD", "mdns": "", "http": "", "oui": "", "ports": [554]}
    assert category_authority(ev_ports, "camera") == "analyst-inference", \
        "纯端口投票必须标 analyst-inference"
    assert category_authority(ev_self, "camera") == "shipped-artifact", \
        "设备自报必须标 shipped-artifact"
    # ④ attest 落字段且取值合法
    rec = attest({"url": "u"}, "web.fetch", "取正文", "u", authority_of_fetch({"ok": True}))
    assert rec["authority"] == "shipped-artifact" and "evidence" in rec
    print("authority selftest OK")


if __name__ == "__main__":
    _selftest()
