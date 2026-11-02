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
    """把出向请求的默认 opener 换成「按家底选路」的那个（导入本包时自动执行一次）。

    这里一次做两件事，都不是可选项：

    1. **摘掉僵尸代理**。事故背景（2026-11-02）：运行环境按「系统代理设置」注入了
       HTTP_PROXY/HTTPS_PROXY=127.0.0.1:3067，那个端口是 karing 的，karing 一停就没人
       监听。urllib 默认 opener 会吃这两个变量，于是全站请求发往死端口，报 10061。
       同一时刻纯 socket 直连 baidu/github/douyin 全部 0.2s 内通 ——「网络坏了」是假象。

    2. **认自己的代理**。天网自有代理层（`fleet/proxymgr.py` + `fleet/proxycore.py`：
       订阅解析 → 测速择优 → 生成 sing-box 配置 → 起内核在 127.0.0.1:7891）起来之后，
       默认出向就该走它。**内核起了而出向层不认，等于白起** ——「不靠系统代理也获得
       代理能力」这句话，必须在出向层兑现才算数。

    选路顺序只在 `net.resolve_proxy()` 一处定义（自有 7891 → 系统代理 → 直连），
    这里不重写第二份 —— 两份顺序迟早会漂。

    为什么必须在包入口做而不是各调各的：仓库里有 40 处 urllib.request.urlopen，
    逐个改既改不干净也守不住（新人加一行就又漏了）。一处兜底，全体受益。

    已知边界：install_opener 是「装一次定终身」。若本包 import 时 7891 还没起、之后才起，
    裸 urllib 的默认 opener 会停留在直连；走 `net.urlopen()` 的调用不受影响（它每次
    现算选路）。这个边界是有意的 —— 不在 import 之外偷偷改全局状态。
    """
    try:
        from . import net
    except Exception:
        return
    try:
        url, _why = net.resolve_proxy()
        urllib.request.install_opener(net.opener(url or "direct"))
    except Exception:
        pass  # 兜底失败也不能拖垮 import（网络问题不许变成启动问题）


_harden_outbound()
