"""fleet：设备编队层（一层「什么设备、能干什么、现在能不能干」）。

为什么要有这个包：仓库根目录的 37 个脚本各自认得一台具体设备，没有一层能回答
「家里现在几台在线、哪台不正常」。这个包把那堆脚本收成三层：

    core/        契约（结果信封 / 设备基类 / 注册表）——唯一真相
    transports/  「怎么连」（本地 / adb / …）——一类通道一个文件
    kinds/       「是什么」（本机 / 手机 / 扫描器 / 编队节点）——一类设备一个文件

纪律：旧脚本原地不动，这里只加薄适配（adapter）；不搬家、不重写。
"""
import os
import sys

# 根目录那批模块（common / android_device / pc_state …）是平铺的，fleet 是它们的
# 上层。这样加一行，`python -m fleet` 从任何目录跑都能 import 到被包住的老模块。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

__all__ = ["core", "transports", "kinds"]
VERSION = "0.2.0"
__version__ = VERSION
