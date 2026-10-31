"""可达层设备：把「谁在这张网里」变成一问就能答的东西。

🔴 这文件最要紧的不是那三条能力，是它**没有**的东西。

  内圈（编队节点）与外圈（可达对端）之间那道墙，就是在这里划的：

      编队节点 — 入过网、有 PSK、账本里有记录 → 能 exec / 能用算力 / 能改配置
      可达对端 — 只要连得上                  → 能传文件 / 能读名片 / 能被看见

  外圈的设备**不会**被注册成一张 BaseDevice，所以它们天然进不了 `reg.call(...)`
  的目标列表 —— 不是靠「记得别调」，是靠**结构上够不着**。
  注册表是按设备分派的，一台设备不在表里，就没有任何一条能力能落到它头上。

  这不是洁癖。局域网里能连上我们的东西包括同事的笔记本、访客的手机、
  打印机、摄像头。如果「连上了」等于「可以在我这边执行命令」，
  那这个功能本身就是个洞。LocalSend 之所以敢做成「连上就互传」，
  正因为它的协议里**只有传文件**——文件由对面的人自己点接受。
  我们把这条线画在同一个位置：**外圈只能做「对面人会自己确认」的事。**

三条能力，故意只有三条（`forget` 算在内）：
  reach.list   只读。列现在看得见的设备，带每条通道的状态。
  reach.at     只读。一台设备的全貌：所有通道、排序结果、什么时候见过。
  reach.forget 有副作用。**真删**，连同通道历史一起。用于「这台我不想知道」。

为什么没有 reach.add：设备只能被**观察到**，不能被**声明**。
一条 `reach.add <随便一个名字>` 会立刻长出「我说它在，它就在」的假账本，
而假的账本比空的账本坏得多。
"""
from .. import reach
from ..core import BaseDevice, DeviceResult as R


class ReachLayer(BaseDevice):
    kind = "reach"
    via = "lan"

    def __init__(self, name="reach:layer", **kw):
        super().__init__(name, via="lan", **kw)
        self.meta.setdefault(
            "note", "外圈：连得上就在，连不上就标不可达；这里没有执行权")

    def probe(self):
        """这一层永远可用 —— 它是账本，不是探针。

        为什么不做「有没有设备」这种 probe：账本是空的也不代表坏了。
        把「空」判成「不可用」，界面就会显示一个红叉，而正确答案是
        「现在网里没别人」。空与坏必须分开。
        """
        return True, ""

    def caps(self):
        return ("reach.list", "reach.at", "reach.forget")

    def invoke(self, cap, args=None):
        args = args or {}

        if cap == "reach.list":
            devs = reach.devices(only_available=not args.get("all"))
            out = [{"id": d["id"], "alias": d.get("alias"), "live": d.get("live"),
                    "best": d.get("best"), "last_seen": d.get("last_seen")}
                   for d in devs]
            return R.success("reach.list", self.name, via=self.via, data={
                "count": len(out), "devices": out, "stats": reach.stats(),
                "note": "只列出至少一条通道通的；加 all=true 连断过的也列出"})

        if cap == "reach.at":
            key = args.get("id") or args.get("device")
            if not key:
                return R.error("reach.at", "缺少 id（指纹前缀 / 名字都行）",
                               device=self.name, via=self.via)
            dev = reach.get(key) or reach.find(key)
            if not dev:
                return R.unavailable("reach.at", "账本里没有这台设备（还没发现过？）",
                                     device=self.name, via=self.via)
            detail = dict(dev)
            detail["ranked"] = reach.ranked_channels(dev)
            detail["best"] = reach.best_channel(dev)
            return R.success("reach.at", self.name, via=self.via, data={"device": detail})

        if cap == "reach.forget":
            key = args.get("id") or args.get("device")
            if not key:
                return R.error("reach.forget", "缺少 id", device=self.name, via=self.via)
            # 先按精确 id 找，再按名字/通道地址模糊找 —— 跟 reach.at 一条规矩。
            # 只认 id 的话，「删掉手机」这句人话就用不了，人得先查一次指纹。
            dev = reach.get(key) or reach.find(key)
            target = dev["id"] if dev else key
            existed = reach.forget(target)
            return R.success("reach.forget", self.name, via=self.via, data={
                "removed": existed, "id": target,
                "note": "" if existed else "本来就没有这台，什么都没删"})

        return R.unsupported(cap, "%s 没有这项能力" % self.name,
                             device=self.name, via=self.via)
