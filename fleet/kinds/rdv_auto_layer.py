"""地址自动化设备：把 rdv.* 从「手动调」变成「一直在跑」。

## 它和 rdv.* 的分工

    rdv.*        一次调用做一件事：查、报、验、解析
    rdv.auto.*   把上面几件事按正确次序循环起来，并且记得上次结果

「按正确次序」不是凑数：先检测 → 变了才上报 → 上报成功后再从墙外自检 →
由两者共同定状态。反过来（先自检）会出现「地址簿里还没有这个地址就让人去连」，
那不是网络问题，是自己制造出来的假阴性。

## 五个状态，每个都说清「接下来该干什么」

    never       还没跑过                   → 起来
    unverified  报了，但还没法验/没验过     → 等自检周期，或先开监听口
    linked      报了，墙外确实连得进来      → 不用管
    blind       报了，墙外一个都进不来      → 开 pinhole / 查 v6 防火墙
    down        报不上去                    → 查中继与密钥

**unverified 单列**：把「没验过」报告成 linked，对端会拿着这个结论去连然后超时，
而真正的原因根本不在对端。

## 诚实边界（重要）

`rdv.auto.start` 起的是**进程内**线程。它只在长驻进程（编队节点 / GUI 服务）里
才有意义；用 CLI 一次性调用时，命令结束线程随之消失，`runtime.running` 会是 False。
这一层不注册系统计划任务，也不偷偷常驻 —— 要常驻就在节点进程里设
`FLEET_RDV_AUTO=1`。
"""
import os

from .. import rdv_auto as auto_mod
from ..relay_pair import resolve_endpoint
from ..core import BaseDevice, DeviceResult as R

# 复用 rdv:layer 那个单例：两者必须共用同一份快照与节流，
# 否则一边刷新、一边还在用旧快照，会互相覆盖状态。
try:
    from .rendezvous_layer import _RDV
except Exception:                                    # pragma: no cover
    _RDV = None


class RdvAutoLayer(BaseDevice):
    kind = "rdv:auto"
    via = "wan"

    def __init__(self, name="rdv:auto", **kw):
        super().__init__(name, via="wan", **kw)
        self.meta.setdefault(
            "note", "检测→上报→墙外自检→切换，按周期自己跑；只在长驻进程里有效")

    def probe(self):
        """这一层永远「在场」：它是本机的一条循环，不依赖外部设备。

        **刻意不在这里检查中继配置**。上一版这么干了，后果是：没配置 ⇒ 设备
        被判不可用 ⇒ 连 `rdv.auto.status` 都调不到 ⇒ 恰恰在最需要看「到底缺什么」
        的时候看不到。诊断性能力必须永远可查，配置缺失由 start 如实报。
        """
        return True, ""

    def caps(self):
        return ("rdv.auto.start", "rdv.auto.stop",
                "rdv.auto.status", "rdv.auto.events")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "rdv.auto.start":
            return self._start(args)
        if cap == "rdv.auto.stop":
            return self._stop(args)
        if cap == "rdv.auto.status":
            return self._status(args)
        if cap == "rdv.auto.events":
            return self._events(args)
        return R.unsupported(cap, "%s 没有这项能力" % self.name,
                             device=self.name, via=self.via)

    # ---------------------------------------------------------- 起
    def _start(self, args):
        cfg, why = resolve_endpoint(args.get("relay_url") or "",
                                    args.get("relay_secret") or "")
        if cfg is None:
            return R.unavailable("rdv.auto.start", why,
                                 device=self.name, via=self.via)
        # 已经在跑就直接返回同一份。**不能新建实例再 start()**：
        # 新实例的 _thread 是 None，它的去重只能护住自己，
        # 结果就是「重复调用」真起了第二条线程 —— 两条循环抢同一份状态文件。
        running = auto_mod.get_loop()
        if running is not None and running.status_dict.get("running"):
            return R.success("rdv.auto.start", self.name, via=self.via, data={
                "already": True, "interval": running.interval,
                "verify_every": running.verify_every,
                **running.status_dict,
                "note": "已经在跑，没有起第二条",
            })

        loop = auto_mod.AutoLink(
            rdv=_RDV,
            origin=cfg["url"], secret=cfg["secret"],
            interval=float(args.get("interval") or auto_mod.DEFAULT_INTERVAL),
            verify_every=float(args.get("verify_every")
                               or auto_mod.DEFAULT_VERIFY_EVERY),
            deep_every=float(args.get("deep_every") or auto_mod.DEFAULT_DEEP_EVERY),
            port=int(args.get("port") or 0),
        )
        r = loop.start()
        if not r.get("ok"):
            return R.error("rdv.auto.start", r.get("err") or "起不来",
                           device=self.name, via=self.via)
        with auto_mod._LOCK:                        # 登记成进程内单例
            auto_mod._ALIVE["default"] = {"thread": loop._thread, "loop": loop}
        return R.success("rdv.auto.start", self.name, via=self.via, data={
            "already": r.get("already"),
            "interval": loop.interval,
            "verify_every": loop.verify_every,
            **loop.status_dict,
            "note": ("已经在跑，没有起第二条" if r.get("already")
                     else "已起：先报地址，再等自检周期给结论"),
        })

    # ---------------------------------------------------------- 停
    def _stop(self, args):
        r = auto_mod.stop_loop()
        return R.success("rdv.auto.stop", self.name, via=self.via, data=r)

    # ---------------------------------------------------------- 看
    def _status(self, args):
        loop = auto_mod.get_loop()
        st = loop.status_dict if loop is not None else auto_mod.loop_status()
        return R.success("rdv.auto.status", self.name, via=self.via, data=st)

    def _events(self, args):
        loop = auto_mod.get_loop()
        if loop is not None:
            evs = loop.events(limit=int(args.get("limit") or 20))
        else:
            evs = auto_mod.AutoLink().events(limit=int(args.get("limit") or 20))
        return R.success("rdv.auto.events", self.name, via=self.via, data={
            "count": len(evs), "events": evs,
            "note": "只有状态迁移与地址变化才记；「还是原样」不记",
        })
