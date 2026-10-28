#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T32 活线门禁：桌面界面「点下去有没有反应」（死链清零，X0）。

为什么这条必须是活线门禁
------------------------
死链的典型形态不是「按钮没绑事件」——那种肉眼一看就知道。真正常见的是：
  · 点了切换视图，内容却是上一页的（渲染写错对象）；
  · 点了弹 toast 说「还没做」，看着有反应，其实功能是空的；
  · 点了异步请求慢，2 秒还没回，被误判成「没反应」。
这三种静态代码扫不出来，只能在真浏览器里点一遍。

判据（与 tools/uicheck/clickaudit.js 一致，不在这里另立标准）：
  dead = 点了之后**页面指纹没变** 且 **没有任何网络请求发出**。
  被别的层遮住 / disabled / 本来就是当前选中项 ⇒ 记「跳过」，不算死链——
  那不是界面缺陷，是审计自己的姿势问题或正确行为。

环境缺 node / playwright / chromium 时 skip 并说明缺什么，不假装通过。
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UICHECK = os.path.join(ROOT, "tools", "uicheck")
AUDIT = os.path.join(UICHECK, "clickaudit.js")

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


#: 真实卡库的人格指针。门禁跑完必须原样还回去。
_ACTIVE = os.path.join(ROOT, "cards", "active.json")


def _snap_active():
    """备份人格指针。读不到就返回 None——不因为「本来没有」而造一个出来。"""
    try:
        with open(_ACTIVE, "rb") as f:
            return f.read()
    except OSError:
        return None


def _restore_active(snap):
    """还回去。还不上也不许把门禁搞挂，但要吼一声（静默才是真糟糕）。"""
    if snap is None:
        return
    try:
        with open(_ACTIVE, "wb") as f:
            f.write(snap)
    except OSError as e:
        print("  [X ] 人格指针没还回去：%s —— 天网的卡已经不是原来那张了"
              % e, flush=True)


def main():
    """跑之前记下人格指针，跑完必须还回去。

    🔴 2026-10-28 的真事故：本门禁调 tools/uicheck/clickaudit.js 点遍界面，
    而卡库里每张卡都带「切换到这张」按钮——点一次就是**真的换人格**
    （写 cards/active.json）。16 张点完，指针停在列表最后一个「验收者」上。
    于是**每跑一次门禁，天网就从「大肥鱼」变成验收员**：问她"你能干什么"，
    她答"逐条复现 EVIDENCE、跑不起来判 FAIL、交五段式答卷"。
    10-26 修过一次（当时以为是人手切的），又漂回来——根源就是这条门禁，
    没人想到"跑门禁"会改产品状态。

    门禁的纪律是**只读**：可以看界面上有什么，但不能留下痕迹。
    同理，以后往界面加"点一下就有持久影响"的按钮，这里也得跟着兜住。
    """
    snap = _snap_active()
    try:
        return _run()
    finally:
        _restore_active(snap)


def _run():
    check(os.path.exists(AUDIT), "找到 tools/uicheck/clickaudit.js")
    node = os.environ.get("NODE_BIN") or "node"
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20)
        node_ok = v.returncode == 0
    except (OSError, subprocess.SubprocessError):
        node_ok = False
    check(node_ok, "node 在（%s --version 可执行）" % node)
    if not node_ok:
        print("t32_deadlink: SKIP（没 node）", flush=True)
        return 0
    if not os.path.isdir(os.path.join(UICHECK, "node_modules", "playwright")):
        print("t32_deadlink: SKIP（没装 playwright：cd tools/uicheck && npm i）", flush=True)
        return 0

    theme = os.environ.get("FLEET_CLICKAUDIT_THEME", "light")
    only = os.environ.get("FLEET_CLICKAUDIT_ONLY", "")   # 空 = 全跑
    fd, out_json = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    cmd = [node, AUDIT, "--theme", theme, "--json", out_json]
    if only:
        cmd += ["--only", only]
    env = dict(os.environ)
    env["FLEET_PY"] = sys.executable
    print("  跑：" + " ".join(cmd[-4:]), flush=True)

    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    tail = "\n".join(out.strip().splitlines()[-10:])

    check(r.returncode == 0, "审计脚本正常退出（rc=%d）" % r.returncode)
    check(os.path.getsize(out_json) > 100, "产出了结果 JSON")
    if os.path.getsize(out_json) > 100:
        with open(out_json, encoding="utf-8") as f:
            d = json.load(f)
        dead = d.get("dead") or []
        alive = d.get("alive") or []
        skipped = d.get("skipped") or []
        cov = d.get("coverage") or {}
        check(not dead, "死链 0 条（实得 %d）" % len(dead))
        for x in dead[:8]:
            print("      DEAD 「%s」[%s] %s" % (x.get("text"), x.get("phase"),
                                                (x.get("why") or "")[:60]), flush=True)
        # 覆盖率太低说明「死链 0」不可信：没点到自然没死链
        pct = cov.get("pct", 0)
        check(pct >= 90, "覆盖率 ≥90%%（实得 %s%%，枚举 %s / 点了 %s）"
              % (pct, cov.get("enumerated"), cov.get("clicked")))
        check(len(alive) >= 150, "真点活的元素 ≥150（实得 %d）" % len(alive))
        # 跳过项必须每一类都有解释，不许出现「理由为空」的静默跳过
        check(all((x.get("why") or "").strip() for x in skipped),
              "跳过项都带理由（%d 项，无静默跳过）" % len(skipped))
        # 遮罩类跳过必须为 0：审批弹窗是 1 秒轮询，只 hidden 会立刻被翻回来，
        # 那是审计自己的清场没做干净（见 clickaudit 里的注释）。
        masked = [x for x in skipped if "apprmask" in (x.get("why") or "")]
        check(not masked, "没有被审批遮罩误导的假跳过（实得 %d）" % len(masked))
    os.remove(out_json)

    if FAIL:
        print("\nt32_deadlink: FAIL (%d 项)\n%s" % (len(FAIL), tail), flush=True)
        return 1
    print("t32_deadlink: PASS（死链 0 / 覆盖率达标）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
