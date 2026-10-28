"""环境自检（doctor）—— 提前扫一遍，告诉你「该装什么」。

解决的根因：之前能力只在被调用时才 try-import 报「未装 xxx」，等于没有自检。
psutil 那次就是典型——能力「存在但不可用」，谁都不知道，直到真跑才炸。

本模块把 deps.py 的声明变成可消费的报告：
  · 能力级：哪些因缺依赖不可用(fail) / 降级(warn) / 正常(ok) / 无依赖(na)
  · 项目级 / GUI 级：rich、pyyaml、pywebview 等
  · 聚合：缺哪些 pip 包（可一键装）、哪些 exe/repo（给命令）
  · fix：只 pip install 声明过的白名单 pip 包，绝不碰 exe/repo（不乱下二进制）

checker 可注入：门禁用假 checker 跑逻辑，不依赖真实环境。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.request

from .deps import CAP_DEPS, PROJECT_DEPS, GUI_DEPS, Dep


def _default_checker(d: Dep):
    if d.kind == "pip":
        name = d.import_name or d.name
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:
            return False
    if d.kind == "exe":
        return shutil.which(d.exe or d.name) is not None
    if d.kind == "repo":
        p = os.environ.get(d.env, "")
        return bool(p) and os.path.isdir(p)
    if d.kind == "sys":
        return True
    return True


def _match_deps(cap_name):
    """精确匹配，否则最长前缀匹配（key 含 '*'）。"""
    if cap_name in CAP_DEPS:
        return CAP_DEPS[cap_name]
    best = None
    for k, v in CAP_DEPS.items():
        if k.endswith("*") and cap_name.startswith(k[:-1]):
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    return best[1] if best else ()


def scan(caps=None, checker=None, deep=False):
    checker = checker or _default_checker
    if caps is None:
        from .catalog import CAPABILITIES
        caps = list(CAPABILITIES)

    cap_rows, miss_pips, miss_exes, miss_repos = [], set(), {}, {}
    for c in caps:
        deps = _match_deps(c.name)
        missing = [d for d in deps if not checker(d)]
        if not deps:
            status = "na"
        elif not missing:
            status = "ok"
        else:
            status = "fail" if any(d.hard for d in missing) else "warn"
        for d in missing:
            if d.kind == "pip":
                miss_pips.add(d.pip_install)
            elif d.kind == "exe":
                miss_exes[d.name] = d.as_dict()
            elif d.kind == "repo":
                miss_repos[d.name] = d.as_dict()
        cap_rows.append({
            "name": c.name, "status": status,
            "missing": [d.as_dict() for d in missing],
            "deps": [d.as_dict() for d in deps],
        })

    proj_rows = [_group_row(d, checker) for d in PROJECT_DEPS]
    gui_rows = [_group_row(d, checker) for d in GUI_DEPS]

    return {
        "caps": cap_rows,
        "project": proj_rows,
        "gui": gui_rows,
        "miss_pips": sorted(miss_pips),
        "miss_exes": sorted(miss_exes.values(), key=lambda x: x["name"]),
        "miss_repos": sorted(miss_repos.values(), key=lambda x: x["name"]),
        "summary": _summary(cap_rows, proj_rows, gui_rows),
        "health": _health(cap_rows, deep),
    }


def _group_row(d, checker):
    ok = checker(d)
    return {
        "name": d.name,
        "status": "ok" if ok else ("fail" if d.hard else "warn"),
        "missing": [] if ok else [d.as_dict()],
        "deps": [d.as_dict()],
    }


def _summary(caps, proj, gui):
    def cnt(rows):
        d = {"ok": 0, "warn": 0, "fail": 0, "na": 0}
        for r in rows:
            d[r["status"]] = d.get(r["status"], 0) + 1
        return d
    return {"caps": cnt(caps), "project": cnt(proj), "gui": cnt(gui)}


def _probe_decider(base=None, timeout=2.0):
    """探决策端点（agent_tools.decide 用的那个）活不活。

    为什么值得单列一项：`decide()` 失败时一律返回 None，调用方只看得到
    「她没用工具 / 她时灵时不灵」——这和「模型答错」长得一模一样。没有这项，
    判断「决策层到底起没起」只能靠手动 netstat + curl，等于把最容易踩的坑
    留给现场排查。而它恰恰是**每台机器都要单独配**的东西（要 llama-server
    和 gguf 模型），换台机器就没了。

    先探 /health（llama.cpp 自带，最便宜），没有再退 /v1/models
    （OpenAI 兼容网关通吃，zen-gate 这类就这么探）。
    """
    base = (base or os.environ.get("AT_DECIDER_URL") or "").rstrip("/")
    if not base:
        try:                                    # 没显式传就跟随 agent_tools 的默认
            from . import agent_tools as T
            base = str(T.DECIDER_URL).rstrip("/")
        except Exception as e:
            return {"url": "", "alive": False,
                    "why": "读不到默认地址：%s" % type(e).__name__}
    why = ""
    for path in ("/health", "/v1/models"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as r:
                raw = r.read(65536).decode("utf-8", "replace")
            if path == "/health":
                st = "ok"
                try:
                    st = (json.loads(raw) or {}).get("status") or "ok"
                except Exception:
                    st = raw.strip()[:40] or "ok"
                return {"url": base, "alive": True, "via": "/health", "status": st}
            ms = (json.loads(raw) or {}).get("data") or []
            return {"url": base, "alive": True, "via": "/v1/models", "models": len(ms)}
        except Exception as e:
            why = "%s: %s" % (type(e).__name__, str(e)[:80])
    return {"url": base, "alive": False, "why": why or "两条探路都没响应"}


def _health(cap_rows, deep=False):
    """体检结论：能力可用率 +（可选）网关真实状态。

    deep=False 只算本地能算的（毫秒级，纯内存）；deep=True 才去探网关
    （1–2 秒，走网络）。为什么分开：GUI 每次打开都全扫会让界面卡在
    「体检中」；网络探测应该由用户主动触发，不该在渲染路径上偷偷跑。

    判据口径：`na`（无第三方依赖、纯逻辑能力）算「可用」——它本来就不需要环境。
    一个网关都不活 = fail，比缺几个 pip 包严重：依赖缺了能力还在，网关死了
    整个 agent 就只会回「请求失败」。
    """
    tot = len(cap_rows)
    ok = sum(1 for r in cap_rows if r["status"] == "ok")
    na = sum(1 for r in cap_rows if r["status"] == "na")
    fail = sum(1 for r in cap_rows if r["status"] == "fail")
    usable = ok + na
    h = {"caps_total": tot, "caps_ok": ok, "caps_na": na, "caps_fail": fail,
         "caps_usable_pct": int(round(usable * 100.0 / tot)) if tot else 0}
    verdict = "ok" if fail == 0 else ("warn" if usable * 100 >= tot * 60 else "fail")
    if deep:
        try:
            from . import providers as P
            provs = P.probe_all()
            alive = [p for p in provs if getattr(p, "alive", False)]
            h["gateways"] = {
                "alive": len(alive), "total": len(provs),
                "models": sum(len(getattr(p, "models", None) or []) for p in alive),
            }
            if not alive:
                verdict = "fail"
        except Exception as e:
            h["gateways"] = {"err": "%s: %s" % (type(e).__name__, e)}
        # 决策端点单独报一项。口径：死了**不升级成 fail**——网关活着她还能对话，
        # 只是用不了工具；但也不能算 ok，否则「一句工具都调不出来」会被体检说成
        # 一切正常（原来就是这个后果）。所以往下压一级到 warn。
        h["decider"] = _probe_decider()
        if not h["decider"].get("alive") and verdict == "ok":
            verdict = "warn"
    h["verdict"] = verdict
    return h


def text_report(rep):
    lines = ["== 天网环境自检 =="]
    h = rep.get("health") or {}
    if h:
        gw = h.get("gateways")
        extra = ""
        if gw and "alive" in gw:
            extra = " · 网关 %d/%d 活（%d 模型）" % (
                gw["alive"], gw["total"], gw.get("models", 0))
        lines.append("体检：%s · 能力可用 %d%%（%d/%d）%s" % (
            h.get("verdict", "?"), h.get("caps_usable_pct", 0),
            h.get("caps_ok", 0) + h.get("caps_na", 0), h.get("caps_total", 0), extra))
        dc = h.get("decider")
        if dc:
            if dc.get("alive"):
                tail = ("（%d 模型）" % dc["models"]) if "models" in dc else \
                       ("（%s）" % dc.get("status", "ok"))
                lines.append("决策模型：活 · %s%s" % (dc.get("url", ""), tail))
            else:
                # 说清后果，不只说「连不上」：用户看到「决策模型死了」不知道意味着什么。
                lines.append("决策模型：✗ 没起来 · %s —— %s" % (
                    dc.get("url") or "（未配置）",
                    dc.get("why") or "无响应"))
                lines.append("  ⇒ 后果：她还能对话，但**一个工具都调不出来**"
                             "（表现为「时灵时不灵」）。起法见 start-decider.bat，"
                             "或设 AT_DECIDER_URL 指向别的 OpenAI 兼容端点。")
    s = rep["summary"]
    lines.append("能力 %d（ok %d / warn %d / fail %d / 无依赖 %d）"
                 % (len(rep["caps"]), s["caps"]["ok"], s["caps"]["warn"],
                    s["caps"]["fail"], s["caps"]["na"]))
    for r in rep["caps"]:
        if r["status"] in ("ok", "na"):
            continue
        tag = "✗" if r["status"] == "fail" else "△"
        miss = "，".join(d["name"] for d in r["missing"])
        lines.append("  %s %s —— 缺：%s" % (tag, r["name"], miss))

    if rep["miss_pips"]:
        lines += ["", "缺的 pip 包（可一键装）："]
        lines += ["  %s" % cmd for cmd in rep["miss_pips"]]
    if rep["miss_exes"]:
        lines += ["", "缺的外部程序（手动装，不自动下）："]
        lines += ["  %s：%s" % (d["name"], d["fix"]) for d in rep["miss_exes"]]
    if rep["miss_repos"]:
        lines += ["", "缺的仓库（设环境变量 / 克隆）："]
        lines += ["  %s：%s" % (d["name"], d["fix"]) for d in rep["miss_repos"]]

    for grp, label in ((rep["project"], "项目依赖"), (rep["gui"], "GUI 依赖")):
        bad = [r for r in grp if r["status"] != "ok"]
        if bad:
            lines += ["", "%s：" % label]
            for r in bad:
                m = r["missing"][0]
                fix = m.get("fix") or m.get("pip_install") or ""
                lines.append("  %s %s：%s"
                             % ("✗" if r["status"] == "fail" else "△", r["name"], fix))

    if not (rep["miss_pips"] or rep["miss_exes"] or rep["miss_repos"]
            or any(r["status"] != "ok" for r in rep["project"] + rep["gui"])):
        lines.append("环境齐全，无缺失。")
    return "\n".join(lines)


def fix_pips(rep, runner=None):
    """只装声明过的 pip 包（白名单）。返回 (cmd, result)。"""
    pkgs = [c.split("install", 1)[1].strip() for c in rep["miss_pips"]]
    if not pkgs:
        return [], None
    cmd = [sys.executable, "-m", "pip", "install"] + pkgs
    if runner is None:
        runner = lambda c: subprocess.run(c, capture_output=True, text=True)
    return cmd, runner(cmd)


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return
    rep = scan(deep="--deep" in argv)
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    elif "--fix" in argv:
        cmd, out = fix_pips(rep)
        if cmd:
            print("安装命令：", " ".join(cmd))
            rc = getattr(out, "returncode", None)
            print("返回码：", rc)
        else:
            print("没有可一键安装的 pip 包缺失。")
    else:
        print(text_report(rep))


def _selftest():
    class C:
        def __init__(self, n):
            self.name = n

    caps = [C("host.monitor"), C("host.input.click"), C("web.papers"),
            C("adb.wifi.list"), C("pure.logic")]
    rep = scan(caps=caps, checker=lambda d: False)   # 全缺失

    hm = next(r for r in rep["caps"] if r["name"] == "host.monitor")
    assert hm["status"] == "fail", "host.monitor 缺 psutil 应 fail"
    assert any(d["kind"] == "pip" for d in hm["missing"]), "应报缺 pip 包"

    hi = next(r for r in rep["caps"] if r["name"] == "host.input.click")
    assert hi["status"] == "fail" and hi["missing"][0]["name"] == "pyautogui", \
        "前缀匹配 host.input.* 应命中 pyautogui"

    wp = next(r for r in rep["caps"] if r["name"] == "web.papers")
    assert wp["status"] == "fail" and wp["missing"][0]["kind"] == "repo", \
        "web.papers 缺 repo"

    pl = next(r for r in rep["caps"] if r["name"] == "pure.logic")
    assert pl["status"] == "na", "无依赖能力应 na"

    assert any("psutil" in c for c in rep["miss_pips"]), "miss_pips 应含 psutil"
    assert any("pyautogui" in c for c in rep["miss_pips"]), "miss_pips 应含 pyautogui"

    # fix 白名单：只装 pip，绝碰 exe/repo
    captured = []
    fix_pips(rep, runner=lambda c: captured.append(c) or _ok())
    assert len(captured) == 1, "fix 应只发一次 pip install"
    cmd = captured[0]
    assert "install" in cmd, "应含 install"
    assert not any("bettercap" in a or "adb" in a for a in cmd), \
        "fix 绝不能装 exe/repo"

    #  checker 全 True → 全 ok
    rep2 = scan(caps=caps, checker=lambda d: True)
    assert all(r["status"] in ("ok", "na") for r in rep2["caps"]), \
        "全有依赖不应有 fail/warn"

    # ---- 体检：deep=False 绝不碰网络（门禁必须全离线）----
    h = rep["health"]
    assert h["caps_total"] == len(caps), "体检要数到全部能力"
    assert h["verdict"] in ("ok", "warn", "fail"), "体检结论必须是三档之一"
    assert h["caps_fail"] >= 1, "全缺失时应有 fail"
    assert "gateways" not in h, "deep=False 不许去探网关（门禁全离线）"
    assert "decider" not in h, "deep=False 也不许探决策端点（同样要全离线）"
    h2 = rep2["health"]
    assert h2["verdict"] == "ok", "全有依赖时体检应为 ok，实得 %r" % h2["verdict"]

    # ---- 决策端点探针：只探本机死端口，毫秒级 refused，不算出网 ----
    d = _probe_decider("http://127.0.0.1:1", timeout=0.3)
    assert d["alive"] is False, "死端口必须报 alive=False"
    assert d["url"] == "http://127.0.0.1:1", "要如实回报探的是哪个地址"
    assert d["why"], "失败要给原因，不能只说 false"
    d2 = _probe_decider("", timeout=0.3)     # 空地址：跟随默认，仍应是死
    assert d2["alive"] is False and d2["url"], "空地址要回落到默认地址并如实报"
    # 报错文案必须点明后果——只说「连不上」用户不知道这意味着什么
    _fake = {"decider": {"url": "http://127.0.0.1:18080", "alive": False,
                         "why": "refused"}, "verdict": "warn", "caps_total": 1,
                 "caps_ok": 1, "caps_na": 0, "caps_fail": 0, "caps_usable_pct": 100}
    _txt = text_report({"health": _fake, "caps": [],
                        "summary": _summary([], [], []),
                        "miss_pips": [], "miss_exes": [], "miss_repos": [],
                        "project": [], "gui": []})
    assert "决策模型" in _txt and "工具" in _txt, \
        "报告必须说清「决策端点死了 = 工具调不出来」"

    txt = text_report(rep)
    assert "host.monitor" in txt and "psutil" in txt, "报告应含能力名与包名"
    assert "体检：" in txt, "文本报告要有体检行"
    print("fleet.doctor selftest ok")


def _ok():
    return type("O", (), {"returncode": 0})()


if __name__ == "__main__":
    main(sys.argv[1:])
