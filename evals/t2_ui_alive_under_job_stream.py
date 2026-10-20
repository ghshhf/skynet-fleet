"""事故型门禁：长任务在界面里跑时，界面不许被它拖死。

要防的事故（2026-10-21 实测，是真事不是推测）：

  联网作业一跑就是几分钟，它的输出行要从 worker 线程一路回到界面。
  早先这个回路用的是 Textual 的 `post_message`——它文档上写着线程安全，
  但在这个界面上的实测结果是：**长任务还在跑的时候，worker 线程每递一次
  post_message，主线程的事件循环就整个冻住**：定时器不响、界面不刷新、
  心跳全断，而且**一句报错都没有**。

  最阴的是它挑食：瞬时能力（web.status）也走同一个 post_message，却一切正常，
  所以门禁里那条 smoke 是全绿的——只有真跑长任务才暴露。
  定位到最后靠 faulthandler dump 线程栈：栈里连 MainThread 都看不见，
  说明它堵在 Windows 层面，根本不在 Python 代码里。

  改用官方的 `call_from_thread` 就正常了（它是事件循环自己的跨线程入口）。

这条门禁怎么用假活代替真网络：真跑 web.diffuse 要连外网，门禁就会时好时坏，
红了也分不清是网络抽风还是代码坏了。所以这里直接在 worker 线程里灌 60 行，
只验证一件事——**灌的过程中界面还活着吗**。这个不依赖网络，永远可复现。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading

from fleet.tui import FleetTUI

FLOOD = 60          # 灌多少行（真跑一次站群作业也就是几行到几十行）


def flood(app):
    """假的长任务：在 worker 线程里高频吐行，跟真作业的路径一模一样。"""
    for i in range(FLOOD):
        app._on_job_line(f"[假] (1/2) 收获 {i}")
        time.sleep(0.03)


async def go():
    app = FleetTUI()
    beats = []

    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(2.5)
        if not app.shell:
            return 0, 0, ["shell 没起来"]
        app.set_interval(0.4, lambda: beats.append(time.time()))

        threading.Thread(target=flood, args=(app,), daemon=True).start()
        # 灌的同时让事件循环有充分机会转起来
        for _ in range(14):
            await pilot.pause(0.35)
        return len(beats), app._rendered, []


beats, rendered, fails = asyncio.run(go())

if not beats:
    fails.append("心跳一次都没响：界面在长任务期间被拖死了（跨线程投递又走回老路了？）")
elif beats < 4:
    fails.append(f"只响了 {beats} 次心跳，事件循环明显被卡住")
if rendered < FLOOD:
    fails.append(f"灌了 {FLOOD} 行，只有 {rendered} 行进界面")

print(f"t2: 心跳 {beats} 次 / 收获 {rendered} 行")
for f in fails:
    print(f"  x {f}")
sys.exit(1 if fails else 0)
