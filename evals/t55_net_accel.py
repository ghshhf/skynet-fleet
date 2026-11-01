#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T55 自定义代理层不许炸：netaccel / wsrelay 两条入口必须活着且 GBK 防线在位。

为什么要有这一项
----------------
`fleet/netaccel.py`（gitport 控制层）和 `tools/cloudnode/wsrelay.py`（WS 隧道）
是天网网络层的两条 Python 入口，历史上没有任何自测覆盖——改完只能手跑，
坏了也只有用到那一刻才知道。

更要命的是 GBK 坑（2026-11-01 铁律：中文 Windows 控制台一个 GBK 外字符
就把整个命令炸死）。netaccel 的输出里全是 `✓ ✗ 【】`，wsrelay 里有 `·`——
两个文件都靠入口处 `reconfigure(encoding="utf-8", errors="replace")` 保命。
这条防线如果哪天被「清理无用代码」删掉，本文件第一个跳红。

测什么
------
1. `netaccel.py --selftest` 子进程退出码 0（二进制探测 / 域名表 / PAC 生成）。
2. `netaccel.py status` 在 `PYTHONIOENCODING=gbk`（模拟中文控制台）下退出码 0
   ——GBK 防线失效时这里会 UnicodeEncodeError 直接炸。
3. `netaccel.py help` 输出包含用法关键字（命令表入口活着）。
4. `wsrelay.py --selftest` 子进程退出码 0（WS 帧编解码 / 回环搬运）。

门禁映射
--------
本项以 subprocess 方式调两个入口，import 闭包表达不了这层依赖，
用 GATE_COVERS 显式声明覆盖这两个文件。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATE_COVERS = (
    "fleet/netaccel.py",
    "tools/cloudnode/wsrelay.py",
)


def _run(cmd, env_extra=None, timeout=120):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable] + cmd, capture_output=True,
                       timeout=timeout, cwd=HERE, env=env)
    return p.returncode, (p.stdout or b"") + (p.stderr or b"")


def main():
    failures = []

    rc, out = _run(["fleet/netaccel.py", "--selftest"])
    if rc != 0:
        failures.append("netaccel --selftest 退出码 %d：%s" % (rc, out[-300:]))

    # GBK 防线：模拟中文控制台（cp936）跑 status，不许炸。
    rc, out = _run(["fleet/netaccel.py", "status"],
                   env_extra={"PYTHONIOENCODING": "gbk"})
    if rc != 0:
        failures.append("netaccel status 在 GBK 控制台下炸了（防线丢了？）：%s"
                        % out[-300:])
    if b"gitport" not in out:
        failures.append("netaccel status 输出异常（没有 gitport 字样）")

    rc, out = _run(["fleet/netaccel.py", "help"])
    if rc != 0 or b"gateway" not in out:
        failures.append("netaccel help 不可用（命令表坏了？）rc=%d" % rc)

    rc, out = _run(["tools/cloudnode/wsrelay.py", "--selftest"], timeout=180)
    if rc != 0:
        failures.append("wsrelay --selftest 退出码 %d：%s" % (rc, out[-300:]))

    if failures:
        print("T55 失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("T55 通过：netaccel selftest/status(gbk)/help 全过，wsrelay selftest 过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
