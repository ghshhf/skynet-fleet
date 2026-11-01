"""代理数据面设备（kind=proxycore）：把 `fleet/proxycore.py` 装成编队设备。

能力（按「要不要出网 / 有没有副作用」排）：

    proxy.deploy        有副作用   订阅 → 解析 → 生成配置 → 拉起 sing-box → 开系统代理（翻墙）
    proxy.core_status   只读·本地  查核心进程 / 系统代理状态
    proxy.core_stop     有副作用   停核心 + 关系统代理
    proxy.download_core 有副作用   下载 sing-box 二进制到 vendor/sing-box

这是天网从「管理面」跨到「数据面」的关键一步：把订阅递进来，天网自己
接管流量翻墙，不再依赖 Karing / Clash 等第三方客户端。

架构（学 Karing）：控制面天网自研（proxymgr 解析/归一/测速/规则），
数据面借 sing-box 核心（proxycore 生成 JSON 配置 + 管进程 + 管系统代理）。

纪律（写在返回里）：
- deploy 解析不出节点 → 如实报失败（含逆向兜底 deep 结果），不把空结果包装成成功。
- 改系统代理是 deploy/core_stop 的显式副作用，绝不静默改。
"""
import os
import time
import urllib.request

from .. import proxycore as PC
from ..core import BaseDevice, DeviceResult as R


class ProxyCore(BaseDevice):
    kind = "proxycore"
    via = "local"

    def __init__(self, name="proxy:core", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "local")

    def probe(self):
        exe = PC.find_exe()
        if not exe:
            return True, "代理数据面就绪（sing-box 未下载，deploy 前先 download_core）"
        running = "运行中" if PC.is_running() else "未运行"
        return True, "代理数据面就绪（sing-box v%s，%s，入站 %s）" % (
            PC.SB_VERSION, running, PC.MIXED_ADDR)

    def caps(self):
        return ("proxy.deploy", "proxy.core_status", "proxy.core_stop",
                "proxy.download_core", "proxy.download_wintun")

    # ------------------------------------------------------------ 订阅源取值

    @staticmethod
    def _resolve_source(args):
        """从 args 取订阅原文：content > file > url（url 失败如实报，不静默）。"""
        if args.get("content"):
            return args["content"], None
        fp = args.get("file")
        if fp and os.path.isfile(fp):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    return f.read(), None
            except OSError as e:
                return None, "读文件失败：%s" % e
        url = args.get("url")
        if url:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0", "Referer": url})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "replace"), None
            except Exception as e:
                return None, "拉订阅失败：%s" % e
        return None, "没给订阅（content / file / url 至少给一个）"

    # ------------------------------------------------------------ invoke

    def invoke(self, cap, args=None):
        args = args or {}
        t0 = time.time()
        try:
            if cap == "proxy.deploy":
                raw, err = self._resolve_source(args)
                if err:
                    return R.error(cap, err, device=self.name, via=self.via)
                if not raw:
                    return R.error(cap, "订阅内容为空", device=self.name, via=self.via)
                port = int(args.get("port") or PC.MIXED_PORT)
                r = PC.deploy(raw, fmt=args.get("fmt"), listen_port=port,
                              start_core=bool(args.get("start_core", True)),
                              enable_proxy=bool(args.get("enable_proxy", True)),
                              tun=bool(args.get("tun", False)),
                              fakeip=args.get("fakeip"))
                if r.get("ok"):
                    return R.success(cap, self.name, via=self.via, data=r,
                                     ms=round((time.time() - t0) * 1000, 1))
                return R.error(cap, r.get("err") or "部署失败", device=self.name, via=self.via)

            if cap == "proxy.core_status":
                return R.success(cap, self.name, via=self.via, data=PC.status(),
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.core_stop":
                port = int(args.get("port") or PC.MIXED_PORT)
                ok1, m1 = PC.stop(port)
                ok2, m2 = PC.system_proxy(False)
                return R.success(cap, self.name, via=self.via,
                                 data={"core": m1, "system_proxy": m2},
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.download_core":
                ok, msg = PC.download_singbox()
                if ok:
                    return R.success(cap, self.name, via=self.via, data={"msg": msg},
                                     ms=round((time.time() - t0) * 1000, 1))
                return R.error(cap, msg, device=self.name, via=self.via)

            if cap == "proxy.download_wintun":
                ok, msg = PC.download_wintun()
                if ok:
                    return R.success(cap, self.name, via=self.via, data={"msg": msg},
                                     ms=round((time.time() - t0) * 1000, 1))
                return R.error(cap, msg, device=self.name, via=self.via)

            return R.unsupported(cap, "%s（proxycore）没有这项能力" % self.name,
                                 device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "代理数据面出错（%s: %s）" % (type(e).__name__, e),
                           device=self.name, via=self.via)
