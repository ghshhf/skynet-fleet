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
import urllib.request

# 根目录那批模块（common / android_device / pc_state …）是平铺的，fleet 是它们的
# 上层。这样加一行，`python -m fleet` 从任何目录跑都能 import 到被包住的老模块。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

__all__ = ["core", "transports", "kinds"]
VERSION = "0.2.0"
__version__ = VERSION


def _harden_outbound():
    """把「僵尸代理」这颗地雷从 urllib 的默认 opener 上摘掉（导入本包时自动执行一次）。

    事故背景（2026-11-02）：
        运行环境按「系统代理设置」注入了 HTTP_PROXY/HTTPS_PROXY=127.0.0.1:3067，
        而那个端口是 karing 的，karing 一停就没人监听。urllib 默认 opener 会吃这两个
        变量，于是全站请求发往死端口，报 10061「目标计算机积极拒绝」。
        同一时刻纯 socket 直连 baidu/github/douyin 全部 0.2s 内通 —— 「网络坏了」是假象。

    这里只做一件事：**当且仅当**代理指向本机且端口确实没人监听时，才把默认 opener
    换成直连；代理活着就一个字都不改（正在用的代理必须保住）。

    为什么必须在包入口做而不是各调各的：仓库里有 40 处 urllib.request.urlopen，
    逐个改既改不干净也守不住（新人加一行就又漏了）。一处兜底，全体受益。
    显式要代理的代码用 net.opener("http://...") / net.opener("auto")，不受影响。
    """
    try:
        from . import net
    except Exception:
        return
    try:
        url, _why = net.system_proxy_url()
        if not url:
            return
        if net.is_proxy_alive(url, timeout=0.4):
            return  # 代理活着，别碰
        urllib.request.install_opener(urllib.request.build_opener(
            urllib.request.ProxyHandler({})))
    except Exception:
        pass  # 兜底失败也不能拖垮 import（网络问题不许变成启动问题）


_harden_outbound()
