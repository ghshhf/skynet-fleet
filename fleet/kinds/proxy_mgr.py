"""代理管理面设备（kind=proxy）：把 `fleet/proxymgr.py` 装成编队设备。

能力（按「要不要出网 / 有没有副作用」排）：

    proxy.subscribe  只读·本地  解析订阅（Clash / V2ray 链接 / sing-box / SS）→ 节点清单
    proxy.test       只读·本地  TCP 测速（节点 server:port 的 RTT 与可达性）
    proxy.rules      只读·本地  查某域名 / IP 走直连还是代理（国内直连、其余代理）
    proxy.pick       只读·本地  按延迟选最优节点
    proxy.deep       只读·本地  常规解析识别不了时，用天网协议逆向拆解字节结构兜底

为什么是管理面：天网现有 gitport 是克制型核心（只接管救得动的站），
本设备不做流量转发，只做「订阅归一 + 测速选优 + 规则分流 + 深解兜底」，
产出数据可喂给任意兼容核心（gitport 或 sing-box）。规则数据直接引用
karing-ruleset（克隆到 E:/xmanbian/karing-ruleset），不复制。

纪律（写在返回里）：
- 解析不出节点 ≠ 失败：method="reversed"/"failed" 时如实报，不把空结果包装成成功。
- 测速不可达的节点标 ok=False / latency=-1，不假装能连。
"""
import os
import time
import urllib.request

from .. import proxymgr as PM
from ..core import BaseDevice, DeviceResult as R

_RULESET_REF = r"E:/xmanbian/karing-ruleset"


class ProxyMgr(BaseDevice):
    kind = "proxy"
    via = "local"

    def __init__(self, name="proxy:mgr", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "local")
        self._rt = None  # RouteTable 缓存（加载 karing 规则集稍重）

    def probe(self):
        # 纯本地：订阅解析用标准库 + 可选 yaml；规则引用外部目录（缺失则用内置精简版）
        n = len(PM.builtin_cn_ruleset().domain_suffix)
        return True, "代理管理面就绪（内置 %d 条国内直连后缀，完整版见 %s）" % (n, _RULESET_REF)

    def caps(self):
        return ("proxy.subscribe", "proxy.test", "proxy.rules", "proxy.pick", "proxy.deep")

    # ------------------------------------------------------------ 路由表缓存

    def _routetable(self):
        if self._rt is None:
            self._rt = PM.default_routetable()
        return self._rt

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
            if cap == "proxy.subscribe":
                raw, err = self._resolve_source(args)
                if err:
                    return R.error(cap, err, device=self.name, via=self.via)
                if not raw:
                    return R.error(cap, "订阅内容为空", device=self.name, via=self.via)
                nodes = PM.parse_subscription(raw, source=args.get("source", "subscription"))
                return R.success(cap, self.name, via=self.via,
                                 data={"count": len(nodes),
                                       "nodes": [n.to_dict() for n in nodes],
                                       "sources": sorted({n.source for n in nodes})},
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.test":
                raw, err = self._resolve_source(args)
                if err:
                    return R.error(cap, err, device=self.name, via=self.via)
                if not raw:
                    return R.error(cap, "没给要测的订阅内容", device=self.name, via=self.via)
                nodes = PM.parse_subscription(raw, source="test")
                if not nodes:
                    return R.error(cap, "解析不出节点，没东西可测", device=self.name, via=self.via)
                timeout = float(args.get("timeout") or 3.0)
                PM.probe_all(nodes, timeout=timeout)
                ok = sum(1 for n in nodes if n.ok is True)
                return R.success(cap, self.name, via=self.via,
                                 data={"tested": len(nodes), "reachable": ok,
                                       "nodes": [n.to_dict() for n in nodes]},
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.rules":
                host = args.get("host")
                ip = args.get("ip")
                if not host and not ip:
                    return R.error(cap, "要给 host 或 ip", device=self.name, via=self.via)
                dec = self._routetable().decision(host=host, ip=ip)
                return R.success(cap, self.name, via=self.via, data={
                    "host": host, "ip": ip, **dec},
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.pick":
                raw, err = self._resolve_source(args)
                if err or not raw:
                    return R.error(cap, err or "没给订阅内容", device=self.name, via=self.via)
                nodes = PM.parse_subscription(raw, source="pick")
                PM.probe_all(nodes, timeout=float(args.get("timeout") or 3.0))
                best = PM.pick_best(nodes, only_ok=bool(args.get("only_ok", True)))
                return R.success(cap, self.name, via=self.via, data={
                    "best": best.to_dict() if best else None,
                    "tested": len(nodes),
                    "reachable": sum(1 for n in nodes if n.ok is True)},
                                 ms=round((time.time() - t0) * 1000, 1))

            if cap == "proxy.deep":
                raw, err = self._resolve_source(args)
                if err or not raw:
                    return R.error(cap, err or "没给要深解的内容", device=self.name, via=self.via)
                res = PM.deep_analyze(raw)
                return R.success(cap, self.name, via=self.via, data=res,
                                 ms=round((time.time() - t0) * 1000, 1))

            return R.unsupported(cap, "%s（proxy）没有这项能力" % self.name,
                                 device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "代理管理面出错（%s: %s）" % (type(e).__name__, e),
                           device=self.name, via=self.via)
