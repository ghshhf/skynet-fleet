"""入网载荷（扫码入网）—— shim，真实实现在 fleet/enroll.py。

保留此文件是为了兼容 `python enroll.py ...` 的老调用方式与 commander.py 的 import。
"""
from fleet.enroll import *  # noqa: F401,F403
import fleet.enroll as _impl
import sys
if __name__ == "__main__":
    # 把 argv 透传给真实模块的 main
    sys.argv[0] = "fleet/enroll.py"
    import runpy
    runpy.run_module("fleet.enroll", run_name="__main__")
