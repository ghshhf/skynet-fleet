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


def scan(caps=None, checker=None):
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


def text_report(rep):
    lines = ["== 天网环境自检 =="]
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
    rep = scan()
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

    txt = text_report(rep)
    assert "host.monitor" in txt and "psutil" in txt, "报告应含能力名与包名"
    print("fleet.doctor selftest ok")


def _ok():
    return type("O", (), {"returncode": 0})()


if __name__ == "__main__":
    main(sys.argv[1:])
