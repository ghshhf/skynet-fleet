"""t30 · A4 后台循环心跳 —— 反连重连循环不再静默吞异常。

验收点（对照诊断报告 A4「循环失败无日志 = 点了没反应类玄学」）：
  ① 连接持续失败时，run() 必须产生可见日志（含「失败」字样 + 失败次数）；
  ② 调用 stop() 后线程必须干净退出（wait 可被 event 即时唤醒，不死等 3 秒）。

不依赖真实网络：用 mock 把 socket.create_connection 永远打挂。
"""
import io
import os
import sys
import time
import unittest.mock as mock

_HERE = os.path.dirname(os.path.abspath(__file__))   # evals/
_REPO = os.path.dirname(_HERE)                        # 仓库根（common.py 所在）
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "fleet"))
from fleet import remote_link as RL  # noqa: E402


def main():
    link = RL.PhoneClient(
        host="127.0.0.1", port=1, device_id="d1",
        secret="s", executor=lambda c, a: {"ok": True},
        caps_provider=lambda: [],
    )

    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        with mock.patch.object(RL.socket, "create_connection",
                               side_effect=ConnectionRefusedError("refused")):
            link.start()          # 起 daemon 线程跑 run()
            time.sleep(1.0)       # 足够失败至少一次（run 里 sleep 在 catch 之后）
            link.stop()
            time.sleep(0.3)       # wait(3) 在 stop 后应立即返回
    finally:
        sys.stderr = old

    log = buf.getvalue()
    if "失败" not in log:
        raise AssertionError("重连失败没有任何可见日志（A4 未生效）: %r" % log)
    if "第 1 次失败" not in log:
        raise AssertionError("未记录失败次数: %r" % log)

    # 线程应已退出；给一点宽限再判
    if link._thread is not None and link._thread.is_alive():
        link._thread.join(timeout=1.0)
        if link._thread.is_alive():
            raise AssertionError("run() 线程在 stop() 后未退出（wait 未响应 event）")
    print("[t30] ok: 重连失败可见 + stop 即时唤醒")


if __name__ == "__main__":
    try:
        main()
        print("ALL PASS")
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
