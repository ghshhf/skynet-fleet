#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T59 IPv6 入站放行自动化：把「地址有、对端进不来」的最后一道墙补上。

T56 把 IPv6 拆成四档、T57 打通地址簿、T58 让地址自己上报并做墙外自检。
T58 在真机上跑出来的状态是 `unverified`，原因写着：

    「没有可探测端口（候选的 port 都是 0）：先在本机开一个监听口，
      再来验入站。这不是「进不来」，是「还没法测」」

也就是说，管线全通了，但**最后一步没人做**：v6 没有 NAT，主机防火墙
对 v6 入站的默认策略是**全拦**。这一步不做，前面所有努力都停在
「地址报上去了但没人连得进来」。

本门禁验九件事：

  ① **规则名**：端口必须进名字。不带端口的名字事后没人知道对应哪个口，
     撤销就变成「全清」或者「不敢清」，两种都不是好结果。
  ② **端口校验**：0 / 负数 / 超范围 / 超上限一律拒绝，重复去重。
     防火墙是全局状态，一个长列表就是一个能被远程触发的洞。
  ③ **只对 v6 源生效**：生成出来的命令必须带 IPv6 前缀约束。
     不限源的入站放行会在 v4 上同时敞开同一个端口 ——
     用户以为开了一个 v6 的洞，实际开了两个。
  ④ **幂等**：已存在的规则不重建、不报错。这个方法会被后台循环反复调用，
     不幂等的版本几十轮之后会在机器上堆出几百条一模一样的放行。
  ⑤ **部分成功不许折叠成 ok**：一半建成时报 ok 会让上层以为入站已通，
     然后去等一个永远连不进来的连接。
  ⑥ **撤销看的是「这一批」，不是「全局剩几条」**：机器上只要还留着别的端口，
     用全局计数当判据就会把「删对了」报成「删除失败」。这个 bug 本轮真的踩到过。
  ⑦ **没权限就如实说 need_admin**：不把 PowerShell 的「拒绝访问」
     翻译成「开失败」，后者会让人以为防火墙坏了。
  ⑧ **状态建议四档**：v6 不可用 / 防火墙没开 / 已有规则 / 该开而没开，
     每档给的话必须是能直接照做的，不是「建议检查防火墙配置」。
  ⑨ **真机**：有管理员权限就真开一条真删一条，验证规则里确实是 v6 源前缀。

跑法：python evals/t59_pinhole.py
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import tunnel as T                              # noqa: E402
from fleet import rendezvous as RV                          # noqa: E402
from fleet.kinds.tunnel_layer import TunnelLayer            # noqa: E402

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ✓", msg)
    else:
        print("  ✗", msg, flush=True)
        FAIL.append(msg)


def skip(msg):
    print("  ⊘", msg)
    SKIP.append(msg)


# ------------------------------------------------------------ 假防火墙
class FakeFirewall:
    """把 PowerShell 换成一个内存集合，逻辑一字不改地跑一遍。

    为什么值得写这么一层：本门禁要在任何机器上都能跑，而真改防火墙
    既需要管理员、又会污染别人的系统。假后端让「幂等 / 部分失败 /
    精确撤销」这三条纯逻辑缺陷能被稳定复现 —— 它们跟真实防火墙无关，
    只跟我们怎么组织那批命令行有关。
    """

    def __init__(self):
        self.rules = {}          # name -> {"proto":..., "port":...}
        self.scripts = []
        self.fail_add = set()    # 这些名字在"新建"时抛错，用来模拟部分失败
        self.deny = False        # 模拟没有管理员权限

    # -- 让 pinhole_open / close / list 无感地接到这里
    def __call__(self, script, timeout=20):
        self.scripts.append(script)

        if "New-NetFirewallRule" in script:
            out = []
            for proto, port in re.findall(r"p='(\w+)';port=(\d+)", script):
                name = "%s%s.%s" % (T.PINHOLE_PREFIX, proto, port)
                if name in self.rules:
                    out.append({"name": name, "port": int(port), "proto": proto,
                                "ok": True, "existed": True})
                elif name in self.fail_add:
                    out.append({"name": name, "port": int(port), "proto": proto,
                                "ok": False, "existed": False, "err": "假故障"})
                else:
                    self.rules[name] = {"proto": proto, "port": int(port)}
                    out.append({"name": name, "port": int(port), "proto": proto,
                                "ok": True, "existed": False})
            return 0, json.dumps(out, ensure_ascii=False)

        m = re.search(r"Remove-NetFirewallRule -DisplayName @\((.*?)\)", script)
        if m:
            for name in re.findall(r"'([^']*)'", m.group(1)):
                self.rules.pop(name, None)
            return 0, ""

        # 精确查一批名字在不在
        m = re.search(r"Get-NetFirewallRule -DisplayName @\((.*?)\)", script)
        if m:
            names = re.findall(r"'([^']*)'", m.group(1))
            return 0, "\n".join(n for n in names if n in self.rules)

        # 结构化列表（pinhole_list 走的这条）。必须排在「通配列全表」之前：
        # 两条脚本里都含 pinhole 前缀通配，先匹配通配就会把 JSON 查询
        # 当成纯文本查询答，结果是「规则明明在、列表却是空的」。
        if "ConvertTo-Json" in script:
            data = [{"name": n, "enabled": "True", "action": "Allow",
                     "dir": "Inbound", "proto": v["proto"],
                     "port": str(v["port"]),
                     "remote": list(T.PINHOLE_V6_ANY)}
                    for n, v in sorted(self.rules.items())]
            return 0, json.dumps(data, ensure_ascii=False)

        # 通配列全表（撤销时点名用的那条）
        if T.PINHOLE_PREFIX + "*" in script:
            return 0, "\n".join(sorted(self.rules))
        return 0, ""


class Patched:
    """临时替换模块级函数，退出时无条件还原。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(T, k)
            setattr(T, k, v)
        T._PINHOLE_CACHE.update({"t": 0.0, "v": None})
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(T, k, v)
        T._PINHOLE_CACHE.update({"t": 0.0, "v": None})
        return False


def _scan_autolink_leaks(root=None):
    """扫 evals/ 下所有 `AutoLink(` 调用，找出「带真端口、却没注入 pinhole_fn」的。

    为什么这条要进硬门禁：`AutoLink` 现在会**自动改防火墙**。
    一段带真实监听端口的门禁用例如果忘了注入假实现，跑一次门禁就会在
    用户机器上留下一条 `skynet.pinhole.TCP.<随机高位端口>` 规则 ——
    而它长得跟正常配置一模一样，事后没人会想到去删。
    本轮就真的踩到了（t58 的 v6 自检段）。

    这个检查本身不该写成一个更麻烦的东西：解析 `AutoLink(` 那对括号里的
    文本，看有没有 `port=` 且不是 0，再看有没有 `pinhole_fn`。
    """
    ev = root or os.path.dirname(os.path.abspath(__file__))
    bad = []
    for fn in sorted(os.listdir(ev)):
        if not fn.endswith(".py"):
            continue
        # 跳过自己：本文件里那几段「漏注入样本」是写在临时文件里的字符串，
        # 扫到自己等于把自证样本当成真漏注入。检查器不检查自己不是偷懒，
        # 是因为它没有 AutoLink 调用，只有样本文本。
        if fn == os.path.basename(os.path.abspath(__file__)):
            continue
        path = os.path.join(ev, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception:
            continue
        for m in re.finditer(r"AutoLink\(", src):
            i, depth = m.end(), 1
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            seg = src[m.end():i]
            pm = re.search(r"port\s*=\s*([A-Za-z0-9_\.]+)", seg)
            if not pm:
                continue
            val = pm.group(1)
            if val == "0":
                continue                    # 没端口就不会去开洞
            if "pinhole_fn" in seg or "auto_pinhole=False" in seg:
                continue
            bad.append("%s:%d(port=%s)" % (fn, src[:m.start()].count("\n") + 1, val))
    return bad


def main():
    print("T59 IPv6 入站放行自动化")
    print("=" * 60)

    # ---------------- ① 规则名
    print("\n【1】规则名必须带端口：撤销要能点名")
    n1 = T.pinhole_rule_name(8080, "tcp")
    check(n1 == "skynet.pinhole.TCP.8080", "名字 = 前缀 + 协议 + 端口")
    check(T.pinhole_parse(n1) == ("TCP", 8080), "能反解出 (协议, 端口)")
    check(T.pinhole_parse("skynet.pinhole.8080") is None, "没有协议的名字解析为 None，不猜")
    check(T.pinhole_parse("firefox.rule.8080") is None, "别人的规则不误认")
    check(T.pinhole_parse("skynet.pinhole.SCTP.8080") is None, "不认识的协议拒绝")

    # ---------------- ② 端口校验
    print("\n【2】端口校验：不合法的一律不进命令")
    fw = FakeFirewall()
    with Patched(_ps=fw, is_admin=lambda: True, _win_firewall_state=lambda: {"public": "on"}):
        r = T.pinhole_open([0, -1, 70000, "abc", 45970, 45970])
        check(r["ok"] is True, "混进一堆非法值也能建成合法的那个")
        check(r["ports"] == [45970], "0/负数/超范围/非数字被剔掉，重复去重")
        check(len(fw.rules) == 2, "只建了 TCP+UDP 两条（协议不能合并成一条）")
        bad = T.pinhole_open(list(range(1000, 1010)))
        check(bad["ok"] is False and "最多" in bad["err"],
              "超过上限直接拒绝：不给「批量开端口」留一个能被远程触发的按钮")
        check(len(fw.rules) == 2, "被拒绝时一条规则都没多建")

    # ---------------- ③ 只对 v6 源生效
    print("\n【3】只对 v6 源生效：v4 侧同端口不能被顺手敞开")
    with Patched(_ps=FakeFirewall(), is_admin=lambda: True):
        T.pinhole_open([45971])
        script = T._PINHOLE_CACHE  # 占位，下面用真 fake 的记录
    fw2 = FakeFirewall()
    with Patched(_ps=fw2, is_admin=lambda: True):
        T.pinhole_open([45971])
    sc = fw2.scripts[-1]
    check("'::/1','8000::/1'" in sc.replace(", ", ","),
          "命令里带 ::/1 + 8000::/1（等价 ::/0，v6 全空间）")
    check("-Profile any" in sc, "三个防火墙档位都覆盖（不确定当前算哪档）")
    check("-Direction Inbound" in sc and "-Action Allow" in sc, "方向与动作写死为入站放行")

    # ---------------- ④ 幂等
    print("\n【4】幂等：后台循环会反复调，不能越调越多")
    fw3 = FakeFirewall()
    with Patched(_ps=fw3, is_admin=lambda: True):
        a = T.pinhole_open([45972])
        b = T.pinhole_open([45972])
        c = T.pinhole_open([45972])
        check(a["created"] and not a["existed"], "第一次是建")
        check(not b["created"] and len(b["existed"]) == 2, "第二次全部命中已存在")
        check(b["ok"] is True, "已存在也算成功（不是「什么都没做」）")
        check(len(fw3.rules) == 2, "三轮之后仍然只有两条规则")

    # ---------------- ⑤ 部分成功不许折叠
    print("\n【5】部分成功不许折叠成 ok")
    fw4 = FakeFirewall()
    fw4.fail_add.add("skynet.pinhole.UDP.45973")
    with Patched(_ps=fw4, is_admin=lambda: True):
        r = T.pinhole_open([45973])
        check(r["ok"] is False, "一条建成一条失败 → ok=False")
        check(r["partial"] is True, "同时标 partial，让上层知道「通了一半」")
        check([f["name"] for f in r["failed"]] == ["skynet.pinhole.UDP.45973"],
              "failed 里点名说清是哪一条")

    # ---------------- ⑥ 撤销判据
    print("\n【6】撤销看「这一批」，不看「全局剩几条」")
    fw5 = FakeFirewall()
    with Patched(_ps=fw5, is_admin=lambda: True):
        T.pinhole_open([45974])
        T.pinhole_open([45975])
        r = T.pinhole_close([45974])
        check(r["ok"] is True and r["still"] == [],
              "删 A 时 B 还在 → 仍然算成功（用全局计数会把这里误报成失败）")
        check(r["left"] == 2, "left 如实报出全局还剩 2 条")
        check(sorted(fw5.rules) == ["skynet.pinhole.TCP.45975",
                                   "skynet.pinhole.UDP.45975"], "删的确实是 A，没误伤 B")
        r2 = T.pinhole_close([45974])
        check(r2["ok"] is True and r2["count"] == 0, "重复删同一个端口：幂等成功")
        r3 = T.pinhole_close()
        check(r3["ok"] is True and not fw5.rules, "不给端口 = 全清本模块规则")
        check(T.pinhole_close()["ok"] is True, "本来就空的时候也报成功，不报错")

    # ---------------- ⑦ 权限
    print("\n【7】没权限就说 need_admin，不说「失败」")
    fw6 = FakeFirewall()
    called = []
    with Patched(_ps=lambda s, timeout=20: (called.append(1), (0, ""))[1],
                 is_admin=lambda: False):
        r = T.pinhole_open([45976])
        check(r["need_admin"] is True, "标 need_admin")
        check(r["ok"] is False, "ok=False（确实没做成）")
        check("管理员" in r["err"], "话里直接点明要提权")
        check(not called, "一次 PowerShell 都没起：没权限就别去撞墙")
        c = T.pinhole_close([45976])
        check(c["need_admin"] is True, "撤销同样如实报 need_admin")

    # ---------------- ⑧ 状态建议
    print("\n【8】状态建议：每档都能直接照做")
    fw7 = FakeFirewall()
    with Patched(_ps=fw7, is_admin=lambda: True,
                 _win_firewall_state=lambda: {"public": "on"},
                 ipv6_usable=lambda timeout=4.0, force=False: {"usable": False,
                                                               "verdict": "egress-fail"}):
        s = T.pinhole_status(deep=True)
        check(s["need"] is False and "egress-fail" in s["advice"],
              "v6 出不去时明确说「开 pinhole 没意义」，并点出档位")

    with Patched(_ps=FakeFirewall(), is_admin=lambda: True,
                 _win_firewall_state=lambda: {"public": "off"},
                 ipv6_usable=lambda timeout=4.0, force=False: {"usable": True,
                                                               "verdict": "ready"}):
        s = T.pinhole_status(deep=True)
        check(s["need"] is False and "不需要" in s["advice"],
              "防火墙没开时说不必开（开了也是空转）")

    fw8 = FakeFirewall()
    with Patched(_ps=fw8, is_admin=lambda: True,
                 _win_firewall_state=lambda: {"public": "on"},
                 ipv6_usable=lambda timeout=4.0, force=False: {"usable": True,
                                                               "verdict": "ready"}):
        s = T.pinhole_status(deep=True)
        check(s["need"] is True, "v6 通+防火墙开+无规则 → need=True")
        check("头号原因" in s["advice"], "把「地址有、对端进不来」直接归因到这道墙")
        T.pinhole_open([45977])
        s2 = T.pinhole_status(deep=True)
        check(s2["need"] is False and s2["count"] == 2, "开了之后 need 变 False 并报出条数")
        check(s2["ports"] == [{"proto": "TCP", "port": 45977},
                              {"proto": "UDP", "port": 45977}],
              "ports 里点名协议+端口，不是只说「有 2 条」")

    # ---------------- ⑨ 能力层
    print("\n【9】能力层：三条能力在表里，缺参数不猜")
    L = TunnelLayer()
    for cap in ("tun.pinhole", "tun.pinhole.open", "tun.pinhole.close"):
        check(cap in L.caps(), "%s 已注册" % cap)
    fw9 = FakeFirewall()
    with Patched(_ps=fw9, is_admin=lambda: True,
                 _win_firewall_state=lambda: {"public": "on"},
                 ipv6_usable=lambda timeout=4.0, force=False: {"usable": True,
                                                               "verdict": "ready"}):
        r = L.invoke("tun.pinhole.open", {})
        check(r.ok is False and "不猜" in (r.err or ""),
              "不给端口就拒绝：不猜该开哪个口")
        r = L.invoke("tun.pinhole.open", {"port": 45978})
        check(r.ok is True and r.side_effect is True, "成功且标了 side_effect")
        check((r.data or {}).get("v6_any") == list(T.PINHOLE_V6_ANY),
              "返回里带上「只对 v6 生效」的凭据")
        r = L.invoke("tun.pinhole.close", {"ports": [45978]})
        check(r.ok is True and r.side_effect is True, "撤销也标 side_effect")
        check(not fw9.rules, "能力层撤销真的清干净了")

    # ---------------- ⑩ 真机
    print("\n【10】真机：真开一条、真查、真删")
    if os.name != "nt":
        skip("非 Windows，跳过真机验证")
    elif not T.is_admin():
        skip("当前进程不是管理员，跳过真机验证（这一条不算失败）")
    else:
        port = 45979
        try:
            r = T.pinhole_open([port], ["TCP"])
            check(r.get("ok") is True, "真开成功")
            L2 = T.pinhole_list(force=True)
            hit = [x for x in L2["rules"] if x.get("name") == T.pinhole_rule_name(port, "TCP")]
            check(len(hit) == 1, "真列表里查得到这条规则")
            if hit:
                remote = hit[0].get("remote") or []
                check(set(remote) == set(T.PINHOLE_V6_ANY),
                      "规则里的源地址确实是 v6 全空间，不是 Any（%s）" % (remote,))
                check(hit[0].get("proto") == "TCP" and str(hit[0].get("port")) == str(port),
                      "协议与端口都对")
            r2 = T.pinhole_close([port])
            check(r2.get("ok") is True, "真删成功")
            check(not [x for x in T.pinhole_list(force=True)["rules"]
                       if x.get("name") == T.pinhole_rule_name(port, "TCP")],
                  "删完真的不在列表里了")
        finally:
            try:
                T.pinhole_close([port])
            except Exception:
                pass

    # ---------------- ⑪ 门禁自身不许污染系统
    print("\n【11】门禁纪律：带真端口的 AutoLink 必须注入假 pinhole")
    leaks = _scan_autolink_leaks()
    check(not leaks, "所有带 port= 的 AutoLink 都注入了 pinhole_fn（%s）"
          % ("发现 %d 处漏注入：%s" % (len(leaks), leaks) if leaks else "没有漏"))

    # 自证这条检查不是摆设：造一个漏注入的样本，它必须报出来
    import tempfile
    probe_dir = tempfile.mkdtemp(prefix="t59_scan_")
    try:
        p = os.path.join(probe_dir, "leaky.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("loop = RA.AutoLink(rdv=x, origin=y, port=p6)\n")
        check(bool(_scan_autolink_leaks(probe_dir)), "能抓到漏注入的样本（检查本身有效）")
        with open(p, "w", encoding="utf-8") as f:
            f.write("loop = RA.AutoLink(rdv=x, origin=y, port=0)\n")
        check(not _scan_autolink_leaks(probe_dir), "port=0 不算漏（没端口就不会去开洞）")
        with open(p, "w", encoding="utf-8") as f:
            f.write("loop = RA.AutoLink(rdv=x, port=p6,\n"
                    "    pinhole_fn=lambda ports, protos: {})\n")
        check(not _scan_autolink_leaks(probe_dir), "注入了 pinhole_fn 就不算漏")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    # ---------------- ⑫ 回环识别
    print("\n【12】回环识别：中继在本机时不许给出「墙外可达」")
    S = RV._src_is_local
    check(S("127.0.0.1") is True, "回环 v4 → 本地")
    check(S("10.1.2.3") is True, "私网 v4 → 本地")
    check(S("192.168.1.9") is True, "家用网段 → 本地")
    check(S("fe80::1") is True, "链路本地 v6 → 本地")
    check(S("240e:404:1900:c780:f8f1:be2e:8deb:68dc",
            "240e:404:1900:c780:b0d8:292b:cdb1:fdba") is True,
          "与目标同一个 /64 → 同一条链路，判本地（单看回环/私网抓不到这种情况）")
    check(S("240e:404:1900:c780::1", "240e:404:1900:c781::1") is False,
          "不同 /64 的公网 v6 → 真外部")
    check(S("8.8.8.8") is False, "公网 v4 → 真外部")
    check(S("") is None and S("不是地址") is None, "报不出/看不懂 → None，不猜")
    H = RV._host_is_local
    check(H("http://127.0.0.1:8080") is True, "origin 是回环 → 本地")
    check(H("http://localhost:9/") is True, "localhost 也算本地")
    check(H("http://192.168.1.5:9") is True, "私网 origin → 本地")
    check(H("https://skynet-wsrelay-abcd.onrender.com/tunnel") is False,
          "域名判不了 → 不算本地（所以还得靠 /probe 报源地址）")

    print()
    if SKIP:
        print("跳过 %d 项（环境不具备，不算失败）" % len(SKIP))
    if FAIL:
        print("T59 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T59 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
