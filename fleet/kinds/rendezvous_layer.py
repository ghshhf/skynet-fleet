"""地址动态设备：让「地址会变」从问题变成常态。

## 它回答的四个问题

    rdv.self     我是谁、现在在哪、变了没有
    rdv.publish  把「我现在在哪」写进地址簿（地址一变就重写）
    rdv.verify   **从墙外**回连我自己，确认到底进不进得来
    rdv.resolve  按设备指纹查对端现在在哪
    rdv.events   变更历史（什么时候换过、换掉了哪个）
    rdv.forget   清掉本地记忆

## 为什么这是「另类的固态」

主人的原话。看懂了这句话，这一层才有意义：

  上层的世界里只有 self_id（设备指纹，跨重启跨网络不变），
  地址是它的属性，可以随便漂 —— 漂了自动检测、自动上报、自动切换。
  就像 SSD 的磨损均衡，写的是同一个逻辑块，落在哪颗颗粒上用户看不见。

所以这里的核心不是「拿到一个地址」，而是**维护一个稳定的映射**：
`self_id → 当前地址`。映射本身是持久的，映射的内容是易变的。

## 为什么不主动跑

项目铁律：不建定时任务、不养常驻进程。所以 `_rdv.refresh()` 是
**幂等 + 节流**的：谁调谁推动，1 分钟内重复调就是白调（直接返回上次结果）。
这样它既能被面板反复点，也能被某次真实发送顺手带起来。

## 入站自检为什么只能靠中继

本机往自己的公网地址发包走 hairpin NAT，很多家用路由器根本不支持。
于是「本机自连成功」不能证明外网能连、「本机自连失败」也不能证明外网连不进来
—— 两个方向都是假信号。只有站在墙外的机器来试才算数，中继正好在墙外。
"""
import time

from .. import rendezvous as rdv_mod
from ..relay_pair import resolve_endpoint
from ..core import BaseDevice, DeviceResult as R

#: 进程内单例。地址状态是「这台机器」的，不是「某次调用」的 ——
#: 每次请求新建一个实例，就会丢掉节流和最近一次快照。
_RDV = rdv_mod.Rendezvous()


class RendezvousLayer(BaseDevice):
    kind = "rendezvous"
    via = "wan"

    def __init__(self, name="rdv:layer", **kw):
        super().__init__(name, via="wan", **kw)
        self.meta.setdefault(
            "note", "设备指纹不变、地址随时漂；变了自动检测、上报、切换")

    def probe(self):
        """本地判断，不联网。有没有地址决定这一层能不能做事。"""
        snap = _RDV.snapshot()
        if snap["candidates"]:
            return True, ""
        return False, "本机没有任何可用地址（连私网 IPv4 都没有）"

    def caps(self):
        return ("rdv.self", "rdv.publish", "rdv.verify", "rdv.resolve",
                "rdv.events", "rdv.forget")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "rdv.self":
            return self._self(args)
        if cap == "rdv.publish":
            return self._publish(args)
        if cap == "rdv.verify":
            return self._verify(args)
        if cap == "rdv.resolve":
            return self._resolve(args)
        if cap == "rdv.events":
            return self._events(args)
        if cap == "rdv.forget":
            return self._forget(args)
        return R.unsupported(cap, "%s 没有这项能力" % self.name,
                             device=self.name, via=self.via)

    # ---------------------------------------------------------- 自己
    def _self(self, args):
        r = _RDV.refresh(force=bool(args.get("force")),
                         deep=bool(args.get("deep")))
        return R.success("rdv.self", self.name, via=self.via, data={
            "self_id": r["self_id"],
            "current": r["current"],
            "changed": r["changed"],
            "added": r["added"],
            "removed": r["removed"],
            "candidates": r["candidates"],
            "note": ("地址变了：新增 %d 个、消失 %d 个" % (len(r["added"]), len(r["removed"]))
                     if r["changed"] else "地址没变，不需要重新上报"),
        })

    # ---------------------------------------------------------- 上报
    def _publish(self, args):
        cfg, why = self._cfg(args)
        if cfg is None:
            return R.unavailable("rdv.publish", why, device=self.name, via=self.via)
        r = _RDV.publish(cfg["url"], cfg["secret"], meta=args.get("meta"),
                         deep=bool(args.get("deep")))
        if not r.get("ok"):
            return R.error("rdv.publish", r.get("err") or "上报失败",
                           device=self.name, via=self.via)
        return R.success("rdv.publish", self.name, via=self.via, data={
            "self_id": r["self_id"], "published": r["published"],
            "changed": r.get("changed"),
            "note": "对端现在可以按 self_id 查到这些地址",
        })

    # ---------------------------------------------------------- 自检
    def _verify(self, args):
        cfg, why = self._cfg(args)
        if cfg is None:
            return R.unavailable("rdv.verify", why, device=self.name, via=self.via)
        r = _RDV.verify(cfg["url"], cfg["secret"],
                        port=int(args.get("port") or 0),
                        only=args.get("only") or None)
        if not r["ok"]:
            # 不是「能力不支持」，是「查了，但一个都连不进来」——
            # 这个区别很重要：前者该换方案，后者该开防火墙。
            return R.success("rdv.verify", self.name, via=self.via, data=r)
        return R.success("rdv.verify", self.name, via=self.via, data=r)

    # ---------------------------------------------------------- 查对端
    def _resolve(self, args):
        fp = str(args.get("fp") or "").strip()
        if not fp:
            return R.error("rdv.resolve", "要查谁？缺 fp（设备指纹）",
                           device=self.name, via=self.via)
        cfg, why = self._cfg(args)
        if cfg is None:
            return R.unavailable("rdv.resolve", why, device=self.name, via=self.via)

        r = _RDV.resolve(cfg["url"], fp)
        if not r.get("ok"):
            return R.unavailable("rdv.resolve", r.get("err") or "地址簿里没有这台设备",
                                 device=self.name, via=self.via)

        # 复用可达层账本：把「对端现在在哪条通道」记下来。
        # 不记的话，每次发文件都要重新查一遍地址簿 —— 而地址 1 分钟内不会变。
        try:
            from .. import reach
            for c in r["addrs"]:
                kind = "v6" if ":" in str(c.get("ip")) else "v4"
                reach.note(fp, channel=reach.channel_key(kind, c.get("ip"),
                                                         c.get("port") or None),
                           status="available",
                           meta={"source": "rendezvous"})
        except Exception:
            # 记账失败不该让查询失败 —— 地址本身已经拿到了，这才是调用方要的
            pass

        return R.success("rdv.resolve", self.name, via=self.via, data=r)

    # ---------------------------------------------------------- 历史
    def _events(self, args):
        evs = _RDV.events(limit=int(args.get("limit") or 20))
        return R.success("rdv.events", self.name, via=self.via, data={
            "count": len(evs), "events": evs,
            "note": "只有真正变化过才记；没变化不写空事件",
        })

    def _forget(self, args):
        r = _RDV.forget()
        return R.success("rdv.forget", self.name, via=self.via, data=r)

    # ---------------------------------------------------------- 配置
    @staticmethod
    def _cfg(args):
        return resolve_endpoint(args.get("relay_url") or "",
                                args.get("relay_secret") or "")
