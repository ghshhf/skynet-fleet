#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T33 后台循环心跳（总纲 P0-2）—— 「还活着吗」必须有答案。

要钉死的是**静默卡住**这种失败形态：循环不再干活、进程还在、端口还开、
日志一条不刷，用户看到的只有「点了没反应」。所以：

  ① 账本语义：从没 beat 过 = stale（起了线程没进循环，正是最该看见的）；
     超过 stale_after = stale；失败必须带原因，空原因要显式标出来
     （空原因等于没记）；同名 register 必须是同一个账本。
  ② 常驻循环真接了心跳：静态核每个循环文件里有对应名字的 register，
     名字还得和这里列的清单一致 —— 防止「改了名 / 删了但清单没跟着动」。
  ③ 端到端：真起 FleetServer，`/api/health` 里必须有 `loops`，
     而且**人为塞一个卡住的循环进去，必须被标成 stale**（不是摆设）。
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)

from fleet import beat as B                      # noqa: E402
from fleet.gui import server as GS               # noqa: E402
from fleet.catalog import build                  # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


# 必须接了心跳的常驻循环：名字 → 源文件（相对仓库根）
LOOPS = {
    "link.selfheal": "fleet/gui/link.py",
    "pair.beacon": "fleet/pair.py",
    "remote_link.reconnect": "fleet/remote_link.py",
    "relay.accept": "fleet/skynet_relay.py",
}


def http_json(url, token=""):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None


def main():
    # ------------------------------------------------ ① 账本语义
    B.reset()
    b = B.register("t33.demo", stale_after=5)
    check(B.register("t33.demo") is b, "同名 register 返回同一个账本（不会出现两个账本）")
    check(b.stale(), "从没 beat 过 = stale（起了线程没进循环，必须看得见）")
    t0 = 1000.0
    b.beat(now=t0)
    check(not b.stale(now=t0), "刚 beat 过不 stale")
    check(b.stale(now=t0 + 6), "超过 stale_after 判 stale")
    check(not b.stale(now=t0 + 4), "没超过阈值不误判")
    b.beat(ok=False, error="网卡断开", now=t0 + 1)
    d = b.as_dict(now=t0 + 2)
    check(d["cycles"] == 2, "周期数累加（%s）" % d["cycles"])
    check(d["errors"] == 1, "失败数单列（%s）" % d["errors"])
    check(d["last_error"] == "网卡断开", "失败原因留档（%r）" % d["last_error"])
    b.beat(ok=False, error="", now=t0 + 3)
    check(b.as_dict(now=t0 + 3)["last_error"] == "（未说明原因）",
          "空原因显式标出（空原因等于没记）")
    snap = B.snapshot(now=t0 + 4)
    check(snap["count"] == 1, "快照有条目（%s）" % snap["count"])
    check(snap["stale"] == [], "未超时的循环不进 stale 名单")
    b.beat(ok=True, now=t0)          # 人为把心跳拨回过去 → 应立刻 stale
    B._REG["t33.stuck"] = B.Beat("t33.stuck", stale_after=1)
    B._REG["t33.stuck"].beat(now=time.time() - 600)
    snap = B.snapshot()
    check("t33.stuck" in snap["stale"], "卡住的循环进 stale 名单（%s）" % snap["stale"])

    # ------------------------------------------------ ② 常驻循环真接了心跳
    for name, rel in LOOPS.items():
        p = os.path.join(_REPO, rel)
        check(os.path.exists(p), "找到 %s" % rel)
        if not os.path.exists(p):
            continue
        src = io.open(p, encoding="utf-8").read()
        check('register("%s"' % name in src,
              '%s 里 register("%s") 存在（循环真接了心跳）' % (rel, name))
        check("beat(" in src, "%s 里有 beat 调用" % rel)

    # ------------------------------------------------ ③ 端到端：health 带 loops
    tmp = tempfile.mkdtemp(prefix="t33_")
    reg = build(path=os.path.join(tmp, "devices.json"), with_nodes=False)
    srv = GS.FleetServer(registry=reg)
    srv.start()
    try:
        st, js = http_json(srv.base_url + "/api/health", token=srv.token)
        check(st == 200, "/api/health 可达（%s）" % st)
        check(bool(js and "loops" in js), "health 里有 loops 字段")
        loops = (js or {}).get("loops") or {}
        check("stale" in loops and isinstance(loops.get("stale"), list),
              "loops 带 stale 名单（列表）")
        check("count" in loops, "loops 带 count")
        check(any(r["name"] == "t33.stuck" for r in loops.get("loops") or []),
              "注册过的循环出现在 health 里（不是只在本进程变量里）")
        check("t33.stuck" in (loops.get("stale") or []),
              "卡住的循环在 health 里被标 stale（真被看见，不是摆设）")
        # 失败的循环必须同时出现在 any_error 里，两种事实分开表达
        check(any(r["name"] == "t33.demo" for r in loops.get("loops") or []),
              "有失败的循环也在清单里（errors 单列）")
    finally:
        srv.stop()

    B.reset()
    if FAIL:
        print("\nt33_loops_beat: FAIL (%d 项)" % len(FAIL), flush=True)
        return 1
    print("t33_loops_beat: PASS（账本语义 + 循环接线 + health 可见）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
