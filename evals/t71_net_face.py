"""t71 · 网络层总览门禁（net:face）

这个文件守的不是「能不能跑」，是**这层最容易骗人的五件事**：

  1. 它是不是真的只读 —— 一调就偷偷改注册表/写配置/拉起内核？那它就是个刺客，
     不是总览。判据：调用前后系统代理注册项与内核配置字节级不变。
  2. 它是不是默认不出网 —— 一个「看一眼状态」的动作去打了三个外网站，
     面板刷新就变成流量黑洞。判据：把出口探针换成桩，默认调用不许碰到它。
  3. 缺数据时是不是填了默认值 —— 从没 deploy 过就报 `config_nodes: 0`，
     会被读成「配置在但零节点」，那是另一件事。判据：必须是 None + 原因。
  4. 一侧模块炸了会不会整条塌 —— proxycore 读不到，net.status 还得能报出
     「出向三路」这些它自己就能答的东西。判据：打桩抛异常后 ok=True 且 core.ok=False。
  5. 「此刻通道」和「规则预设」有没有混成一句 —— 内核没起时把规则表的结果
     说成事实，就是骗人。判据：两栏分开 + note 非空。

外加两条结构纪律（都是吃过亏的）：
  · 顶层不许 import 可选依赖 —— 本机没装 sing-box 不该让这台设备哑掉（铁律 10）。
  · 耗时门槛 —— 曾经串行探三个死端口白等 3.3 秒，面板一刷新就卡。
"""
import ast
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fleet import catalog                      # noqa: E402
from fleet import net                          # noqa: E402
from fleet import proxycore as PC               # noqa: E402
from fleet import fp_egress as FE               # noqa: E402

FAIL = []


def check(cond, ok_msg, detail=""):
    """ok_msg 写「通过时成立的事实」；失败时补一句具体哪不成立。"""
    if cond:
        print("  ✓ " + ok_msg)
    else:
        print("  ✗ " + (detail or ok_msg))
        FAIL.append(detail or ok_msg)


def main():
    reg = catalog.build(with_nodes=False)

    # ---- 0. 登记形状
    check(reg.capability("net.status") is not None,
          "net.status 在能力表里", "net.status 没在能力表里（只挂设备不登记 = 模型看不见）")
    check(reg.capability("net.route") is not None,
          "net.route 在能力表里", "net.route 没在能力表里")
    owners = [d.name for d in reg.owners("net.status")]
    check(owners == ["net:face"], "net.status 归属 net:face",
          "net.status 的归属设备不对：%r" % (owners,))

    # ---- 1. 只读：调用前后不留痕
    reg_before = net.reg_proxy_raw()
    cfg_before = None
    if os.path.isfile(PC.CONFIG_PATH):
        with open(PC.CONFIG_PATH, "rb") as f:
            cfg_before = f.read()

    r = reg.call("net.status", args={})
    check(r.ok, "net.status 调用成功（%sms）" % r.ms,
          "net.status 调用失败：%s %s" % (r.code, r.err))
    d = r.data or {}
    check(d.get("egress", {}).get("registry") == reg_before,
          "调用后系统代理注册项没变",
          "net.status 调用后系统代理注册项变了（这层不许改状态）")
    if cfg_before is not None:
        with open(PC.CONFIG_PATH, "rb") as f:
            check(f.read() == cfg_before, "调用后 sing-box 配置字节不变",
                  "net.status 改了 sing-box 配置（这层不许写盘）")

    # ---- 2. 默认不出网
    touched = {"n": 0}
    real_probe = FE.probe

    def stub_probe(*a, **kw):
        touched["n"] += 1
        return {"ok": True, "ip": "203.0.113.7", "country": "CN",
                "asn": "AS64500", "stub": True}

    try:
        FE.probe = stub_probe
        reg.call("net.status", args={})
        check(touched["n"] == 0, "默认调用零外网（没碰出口探针）",
              "默认调用就打了出口探针（应该只在 probe=True 时出网）")
        rp = reg.call("net.status", args={"probe": True})
        check(touched["n"] == 1, "probe=True 触发了一次出口探针",
              "probe=True 没触发出口探针（期望 1 次，实际 %d）" % touched["n"])
        check((rp.data or {}).get("probe", {}).get("ip") == "203.0.113.7",
              "probe=True 的结果落进 data.probe",
              "probe=True 的结果没落进 data.probe")
    finally:
        FE.probe = real_probe

    # ---- 3. 缺数据不许填默认值
    real_cfg = PC.CONFIG_PATH
    try:
        PC.CONFIG_PATH = os.path.join(HERE, "evals", "__no_such_dir__", "config.json")
        r2 = reg.call("net.status", args={})
        c2 = (r2.data or {}).get("core", {})
        check(c2.get("config_nodes") is None,
              "从没 deploy 过时报 None（不是 0）",
              "从没 deploy 过时报了 %r，应该是 None（0 会被读成「配置在但零节点」）"
              % (c2.get("config_nodes"),))
        check(bool(c2.get("config_nodes_why")), "缺节点数时给了原因",
              "缺节点数没给原因（拿不到必须说清为什么）")
        check(c2.get("config_exists") is False, "配置不存在时 config_exists=False",
              "配置不存在时 config_exists 不是 False")
    finally:
        PC.CONFIG_PATH = real_cfg

    # ---- 4. 一侧炸了不许整条塌
    real_find = PC.find_exe

    def boom():
        raise RuntimeError("模拟：sing-box 目录读不了")

    try:
        PC.find_exe = boom
        r3 = reg.call("net.status", args={})
        check(r3.ok, "proxycore 炸了，net.status 整体仍返回成功（降级不塌）",
              "proxycore 读不到时整条 net.status 挂了（应降级而不是全塌）")
        c3 = (r3.data or {}).get("core", {})
        check(c3.get("ok") is False and bool(c3.get("err")),
              "proxycore 的异常被如实写进 core.err",
              "proxycore 异常没被如实报进 core.err")
        check(bool((r3.data or {}).get("egress", {}).get("channels")),
              "proxycore 炸了，出向三路照样报得出（这部分本来就自己算）",
              "proxycore 异常时出向三路也丢了（这部分本来就是自己算的）")
    finally:
        PC.find_exe = real_find

    # ---- 5. 直连通道不许自称活着
    chs = (d.get("egress", {}).get("channels") or [])
    direct = next((c for c in chs if "直连" in c["name"]), None)
    check(direct is not None, "出向通道里包含「直连」兜底通道", "出向通道里没有「直连」这一项")
    check((direct or {}).get("alive") is None,
          "直连通道 alive=None（不探测就不声明它通）",
          "直连通道被打了 alive=True/False（不探测就不许声明它通）")

    # ---- 5b. 半步圆满定义守门（这次修正的灵魂）：不依赖 karing 的真实直连也算达成
    # 防的是将来又把 half_ban 改回「只有 7891 起才算」——那是把 11-01 源头定义弄丢。
    hb = d.get("half_ban") or {}
    de = d.get("direct_egress") or {}
    check(de.get("ok") is not True or hb.get("achieved") is True,
          "半步圆满定义守门：真实直连层通 ⇒ achieved=True（不要求 7891 起）",
          "direct_egress.ok=True 但 half_ban.achieved 不是 True（定义被改窄了）")

    # ---- 6. net.route：两栏分离
    rr = reg.call("net.route", args={"host": "github.com"})
    check(rr.ok, "net.route(github.com) 调用成功",
          "net.route(github.com) 失败：%s" % rr.err)
    rd = rr.data or {}
    check("now" in rd and "if_core_running" in rd,
          "net.route 把「此刻通道」与「规则预设」分成两栏",
          "net.route 没把「此刻通道」与「规则预设」分两栏")
    if rd.get("core_running") is False:
        check(bool(rd.get("note")), "内核没跑时明确提示「规则表只是预设」",
              "内核没在跑却没提示——这就是把预设当事实")

    # 分流预设有区分度才叫分流
    baidu = reg.call("net.route", args={"host": "www.baidu.com"}).data or {}
    gh = rd.get("if_core_running") or {}
    check(baidu.get("if_core_running", {}).get("outbound") == "direct",
          "国内站预设走直连（baidu → direct）",
          "baidu 的预设不是直连（国内直连规则失效）")
    check(gh.get("outbound") == "proxy", "境外站预设走代理（github → proxy）",
          "github 的预设不是代理（兜底规则失效）")

    # ---- 7. 参数校验
    re_ = reg.call("net.route", args={})
    check((not re_.ok) and re_.code == "error", "空参返回 error（不猜 host）",
          "net.route 空参没有报 error 而是 %s" % re_.code)
    url_r = reg.call("net.route", args={"url": "https://www.baidu.com/x"})
    check((url_r.data or {}).get("host") == "www.baidu.com", "能从 url 取到 host",
          "net.route 没从 url 取到 host")

    # ---- 8. 层纪律：顶层不许 import 可选依赖
    src_path = os.path.join(HERE, "fleet", "kinds", "net_face.py")
    with io.open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    banned = {"proxycore", "proxymgr", "fp_egress", "rdv_auto",
              "textual", "socket", "requests", "yaml"}
    top = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
    bad = top & banned
    check(not bad, "顶层只 import 标准库与 ..core（可选依赖全在函数内）",
          "net_face.py 顶层 import 了可选依赖 %r（本机没装就会整台设备哑掉）" % (bad,))

    # ---- 9. 显影面：界面只许从能力取数
    # 防的是「面板自己再算一份」——那就会出现两份真相，主人对着哪份判断都可能错。
    # 终端 TUI 已在 17d9cb5 移除（只留命令行），显影面统一收敛到 GUI 服务端。
    srv_path = os.path.join(HERE, "fleet", "gui", "server.py")
    with io.open(srv_path, "r", encoding="utf-8") as f:
        srv_src = f.read()
    check('"/api/net"' in srv_src, "GUI 服务端有 /api/net 端点",
          "GUI 服务端没有 /api/net —— 图形面板看不见网络层")
    seg = srv_src.split("def _net_panel", 1)
    if len(seg) == 2:
        body = seg[1].split("\n    def ", 1)[0]
        check('call("net.status"' in body,
              "面板网络页从 net.status 取数",
              "面板里没有 call(\"net.status\") —— 网络页要么没接，要么自己算了一份")
        leaked = [w for w in ("is_proxy_alive", "proxycore", "socket.",
                              "resolve_proxy") if w in body]
        check(not leaked, "面板 _net_panel 只画不算（没有直接探口/探模块）",
              "面板 _net_panel 里直接调了 %r —— 界面自己算一份就出现两份真相"
              % (leaked,))
    else:
        check(False, "面板有 _net_panel", "GUI 服务端没有 _net_panel，网络页没接上")

    # 端点逻辑不只是「写了」，要真跑一次（不起服务、不占端口）
    try:
        from fleet.gui import server as GS

        class _FakeFleet:
            registry = reg

        class _FakeSrv:
            fleet = _FakeFleet()

        obj = object.__new__(GS._Handler)   # fleet 是 property → self.server.fleet
        obj.server = _FakeSrv()
        panel = GS._Handler._net_panel(obj)
        check(panel.get("ok") and (panel.get("data") or {}).get("egress", {}).get("channels"),
              "GET /api/net 的载荷含出向三路（%sms）" % panel.get("ms"),
              "_net_panel 吐不出出向三路：%r" % (panel.get("err") or panel,))
    except Exception as e:
        check(False, "", "_net_panel 直接调用炸了：%s: %s" % (type(e).__name__, e))

    # ---- 10. 耗时门槛（串行探死口的回归）
    t0 = time.monotonic()
    reg.call("net.status", args={})
    ms = (time.monotonic() - t0) * 1000
    check(ms < 2500, "耗时 %.0fms（门槛 2500ms）" % ms,
          "net.status 耗时 %.0fms（>2500ms：探活是不是又串行了？）" % ms)

    if FAIL:
        print("FAIL: %d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 网络层总览（只读不留痕/默认不出网/缺数据不填默认/一侧炸不塌/"
          "两栏分离/参数校验/延迟 import/耗时 %.0fms）" % ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
