"""角色卡商店：把「浏览卡 / 下载卡 / 入库卡 / 体检卡」接进设备编队。

为什么做成一个 kind（设备）而不是 GUI 里的几个按钮：
  本仓铁律是「能力唯一真相在注册表」。角色卡商店要能被
  终端 / T 版 / 图形版 / MCP 四个面同时看见、要能过闸门、要能记调用流水，
  就必须走 `BaseDevice`（probe / caps / invoke）这一条路。
  它跟 `ai` 那一层同构：**联网型设备**，via="web"。

能力五项：
  card.sources  只读：列出可用的卡源
  card.search   只读：在某个源上搜卡（浏览）
  card.detail   只读：单卡详情
  card.lint     只读：给一张卡做体检（本地卡也能查）
  card.fetch    副作用：从源下载一张卡并入库（要审批）
  card.ingest   副作用：把本地投递口里的卡体检后入库（要审批）

🔴 为什么「下载」要审批：
  往 `cards/` 里写文件 = 改磁盘 = 影响「她是谁」这一层。
  闸门判在注册表层（side_effect=True ⇒ level 1 审批），
  UI 绕不过去——「点一下就偷偷换掉她的人格」这种事不许发生。
  审批弹窗里能看见参数（哪个源、哪张卡），主人知道自己在批什么。

🔴 为什么「浏览」不要审批：
  搜索是只读的网络请求，不改任何东西。

🔴 体检器在 `card_store.py`（不在本文件）：
  它跟商店无关（本地投递口的卡也要过它），而且它要能被命令行直接用。
"""
import os

from ..core import BaseDevice, DeviceResult as R

#: 仓库根（fleet/kinds/card_store.py → 上三级）。card_store.py 在根下。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _card_dir():
    """卡目录：**唯一真相在 persona_card.DEFAULT_CARD_DIR**。

    🔴 之前这里自己算了一遍（join ROOT/cards），跟 GUI 后端算的不是同一个
    ⇒ 卡下到一处、界面读另一处。现在两边都读 persona_card 的同一个常量。
    """
    import persona_card as pc
    return pc.DEFAULT_CARD_DIR


class CardStore(BaseDevice):
    kind = "cardstore"
    via = "web"

    def __init__(self, name="cards:store", **kw):
        super().__init__(name, via="web", **kw)
        self.meta.setdefault("layer", "persona")
        self._mod = None

    def _cs(self):
        """card_store 模块（懒加载：它会 import persona_card，别在注册表构建期就拖进来）。"""
        if self._mod is None:
            import sys
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            import card_store as cs
            self._mod = cs
        return self._mod

    def probe(self):
        """卡商店是纯 stdlib 的，装不装都在 ⇒ 恒可用。

        🔴 这里**不许**因为「卡目录不存在」就报不可用：
        probe 的契约是「不产生副作用的前提下回答能不能用」，
        建目录是副作用，不许塞进 probe。
        而且浏览/搜索/详情根本不需要目录——只有下载/入库才需要，
        那两个动作自己会建目录（建不出来时如实报错）。
        """
        return True, ""

    def caps(self):
        return ("card.sources", "card.search", "card.detail", "card.lint",
                "card.fetch", "card.ingest")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "card.sources":
            return self._sources(args)
        if cap == "card.search":
            return self._search(args)
        if cap == "card.detail":
            return self._detail(args)
        if cap == "card.lint":
            return self._lint(args)
        if cap == "card.fetch":
            return self._fetch(args)
        if cap == "card.ingest":
            return self._ingest(args)
        return super().invoke(cap, args)

    # ---------------- 只读 ----------------

    def _sources(self, args):
        cs = self._cs()
        return R.success("card.sources", self.name, via=self.via, data={
            "sources": cs.list_sources(),
            "card_dir": _card_dir(),
            "inbox": cs.inbox_dir(_card_dir()),
        })

    def _search(self, args):
        cs = self._cs()
        source = (args.get("source") or "aicc").strip()
        query = args.get("query") or args.get("q") or ""
        skip = int(args.get("skip") or 0)
        limit = int(args.get("limit") or 24)
        try:
            out = cs.search(source, query, skip, limit)
        except cs.SourceError as e:
            return R.error("card.search", str(e), device=self.name, via=self.via)
        except Exception as e:
            return R.error("card.search", f"{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        return R.success("card.search", self.name, via=self.via, data={
            "source": source, "query": query, "skip": skip, "limit": limit,
            "total": out.get("total", 0), "items": out.get("items", []),
        })

    def _detail(self, args):
        cs = self._cs()
        source = (args.get("source") or "aicc").strip()
        cid = args.get("id")
        if cid in (None, ""):
            return R.error("card.detail", "缺少 id（要查哪张卡）",
                           device=self.name, via=self.via)
        if source != "aicc":
            return R.error("card.detail",
                           f"源 {source} 暂不支持查详情（目前只有 aicc 有详情接口）",
                           device=self.name, via=self.via)
        try:
            det = cs.aicc_detail(cid)
        except cs.SourceError as e:
            return R.error("card.detail", str(e), device=self.name, via=self.via)
        except Exception as e:
            return R.error("card.detail", f"{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        return R.success("card.detail", self.name, via=self.via, data={"detail": det})

    def _lint(self, args):
        cs = self._cs()
        path = (args.get("path") or "").strip()
        if not path:
            return R.error("card.lint", "缺少 path（要体检哪张卡）",
                           device=self.name, via=self.via)
        if not os.path.isabs(path):
            path = os.path.join(_card_dir(), path)
        if not os.path.exists(path):
            return R.error("card.lint", f"卡不存在：{path}",
                           device=self.name, via=self.via)
        rep = cs.lint_card(path)
        # 体检不通过**不是调用失败**：体检报告本身就是答复。
        # 但答复里必须说清过没过（ok 字段），不许让调用方自己猜。
        return R.success("card.lint", self.name, via=self.via, data={"lint": rep})

    # ---------------- 副作用 ----------------

    def _fetch(self, args):
        cs = self._cs()
        source = (args.get("source") or "aicc").strip()
        cid = args.get("id")
        if cid in (None, ""):
            return R.error("card.fetch", "缺少 id（要下哪张卡）",
                           device=self.name, via=self.via)
        force = bool(args.get("force"))
        out = cs.fetch_card(source, cid, _card_dir(), force=force)
        if not out.get("ok"):
            return R.error("card.fetch", str(out.get("err") or "下载失败"),
                           device=self.name, via=self.via)
        return R.success("card.fetch", self.name, via=self.via, side_effect=True,
                         data={k: v for k, v in out.items() if k != "path"})

    def _ingest(self, args):
        cs = self._cs()
        move = args.get("move", True)
        out = cs.ingest_inbox(_card_dir(), move=bool(move))
        return R.success("card.ingest", self.name, via=self.via, side_effect=True,
                         data=out)
