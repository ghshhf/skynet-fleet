"""开机自启：把天网网络层整体拉起（gitport + 系统代理 PAC）。

由计划任务用 pythonw.exe 无头执行：
    pythonw.exe E:\\skynet-fleet\\_boot_network.py

三件事，全部幂等：
  1. 从 fleet.netaccel 当前域名表重新生成 gitport.pac（跟随代码更新，不落死域名）；
  2. netaccel.start()：启动 gitport（已在跑则跳过）+ git 代理指向 7810；
  3. netaccel.system_proxy(True, pac)：系统代理指向 PAC（首次会备份原设置）。

失败不抛出、不弹窗，只写 _boot_network.log，方便事后排查。
"""
import os
import sys
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.join(ROOT, "fleet")
PAC = os.path.join(ROOT, "gitport.pac")
LOG = os.path.join(ROOT, "_boot_network.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def main():
    if FLEET not in sys.path:
        sys.path.insert(0, FLEET)
    try:
        import netaccel
    except Exception as e:
        log("IMPORT FAIL: %r" % e)
        return 1

    # 1) 刷新 PAC，跟随代码里的域名表（避免 PAC 陈旧）
    try:
        with open(PAC, "w", encoding="utf-8") as f:
            f.write(netaccel.pac_script())
        log("PAC refreshed: %s" % PAC)
    except OSError as e:
        log("PAC WRITE FAIL (用已有 PAC 继续): %r" % e)

    # 2) 启动 gitport + git 代理（幂等）
    ok1, m1 = netaccel.start()
    ok2, m2 = netaccel.git_proxy(True)
    log("start: %s %s | git-proxy: %s %s" % (ok1, m1, ok2, m2))

    # 3) 系统代理指向 PAC（首次备份原设置）
    ok3, m3 = netaccel.system_proxy(True, PAC)
    log("system-proxy: %s %s" % (ok3, m3))

    rc = 0 if (ok1 and ok2 and ok3) else 1
    log("DONE rc=%d" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(2)
