"""事故型门禁：直播开关默认开，且论文页真的能一条条长出收获。

要防的事故（2026-10-21 提的需求，是真事不是推测）：

  论文爬虫一跑就是几分钟。用户原话——"web 会耗时很长，所以如果一个屏
  不刷新，他就看不到收获"。所以联网作业的输出必须是**逐行实时回流**，
  而不是等跑完才给一句总结。这条门禁就是锁死这件事：

    1. 直播开关默认开（开着才"逐行长出来"）；
    2. 开着时，跑 web.papers，论文页那一屏（RichLog）要一行行长出来；
    3. 关掉时（ctrl+b），中途一行都不该往界面长，但行数要累计，
       收工那一句才能报"共 N 行"；
    4. 重新打开，又能继续长——开关可逆。

同样用假活代替真网络：不连外网，只在 worker 线程里灌行，只验证
"回流的路径对不对、开关管不管用"，永远可复现。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading

from textual.widgets import TabbedContent

from fleet.tui import FleetTUI

FLOOD = 10


def flood(app, n):
    """假的长任务：在 worker 线程里高频吐行，跟真作业的路径一模一样。"""
    for i in range(n):
        app._on_job_line(f"[arxiv] 假收获 {i}")
        time.sleep(0.02)


async def go():
    fails = []
    app = FleetTUI()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(2.5)
        if not app.shell:
            return 0, [], ["shell 没起来"]

        # 1) 开关默认开
        if not app._live:
            fails.append("直播开关默认应是开")

        # 停在论文页：让 RichLog 尺寸已知，写入直接显形（不进 deferred）
        app.query_one(TabbedContent).active = "pane_paper"
        await pilot.pause(0.4)
        app._job_cap = "web.papers"

        # 2) 开着：论文页一行行长出来
        threading.Thread(target=flood, args=(app, FLOOD), daemon=True).start()
        await pilot.pause(0.8)
        on_rows = len(app.query_one("#paperlog").lines)
        if on_rows <= 0:
            fails.append("直播开着，论文页没收流")

        # 3) 关掉：中途不长，但行数累计
        app._live = False
        before = len(app.query_one("#paperlog").lines)
        threading.Thread(target=flood, args=(app, FLOOD), daemon=True).start()
        await pilot.pause(0.8)
        after_off = len(app.query_one("#paperlog").lines)
        if after_off != before:
            fails.append("直播关了还往论文页长行")
        if app._job_lines < 2 * FLOOD:
            fails.append("直播关着也该累计行数（收工才报得出总行数）")

        # 4) 重新开：又能继续长（开关可逆）
        app._live = True
        threading.Thread(target=flood, args=(app, 5), daemon=True).start()
        await pilot.pause(0.8)
        after_on = len(app.query_one("#paperlog").lines)
        if after_on <= after_off:
            fails.append("重新开直播后没有继续长")

        return on_rows, after_on, fails


on_rows, after_on, fails = asyncio.run(go())
print(f"t3: 直播开时长 {on_rows} 行 / 关掉不再长 / 重开累计到 {after_on} 行")
for f in fails:
    print(f"  x {f}")
sys.exit(1 if fails else 0)
