"""万维网：终端的联网层。

这一层不自己实现爬虫，它把两个别人的项目当成两个「远程设备」来管：

  netforce        —— 资源站发现 / 游戏 wiki 语料 / 站群扩散（D:/code/netforce）
  ai_frontier_crawl —— arXiv + HF + OpenAlex 论文（train_15b/tools）

为什么它们也算设备：在注册表里，设备就是「能对外做什么」的一方。网络跟手机、
本机、节点没有本质区别——都是「有的能力现在能用，有的现在不能用，问一句才知道」。
所以联网能力跟其它能力走同一条路：注册表登记 → 闸门 → 结果信封 → 终端两栏同时显形。

域是 `web.*` 不是 `net.*`：局域网那台扫描器（`net.scan` / `net.nearby`）早就占了
`net` 这个族名，而能力名必须唯一——重名在注册表里会当场炸，不给你留情面。

长任务怎么在终端里跑：这些活一跑就是几分钟到几小时，绝不能把界面堵死。
所以 `invoke` 里起的是子进程，边跑边把输出行回调出去（args["_on_line"]），
界面拿它画进度；想中途叫停就 `job.stop()`。不设置这个回调时行为不变，
照样完整跑完再返回结果——命令行模式就是这么用的。
"""
import time

from .. import netrunner as NR
from .. import netforce_skynet as NS
from .. import re_site as RS
from ..core import BaseDevice, DeviceResult as R


class Internet(BaseDevice):
    kind = "internet"
    via = "net"

    def __init__(self, name="web:net", **kw):
        super().__init__(name, via="net", **kw)
        self.meta.setdefault("layer", "web")
        self._job = None            # 当前在跑的作业（供 stop 用）

    def probe(self):
        """联网层算在线的条件：两个源里至少一个装了。

        不要求两个都在——少一个，只是那几个能力变不可用，不是整层塌掉。
        """
        av = NR.available()
        if not av["netforce"] and not av["papers"]:
            return False, ("两个联网源都没装：NETFORCE_HOME（netforce 仓库）与 "
                           "PAPERS_HOME（train_15b）至少给一个")
        return True, ""

    def caps(self):
        return ("web.status", "web.discover", "web.corpus", "web.diffuse",
                "web.papers", "web.frontier", "web.sites",
                "web.search", "web.quote", "web.fetch", "web.probe",
                "web.structure", "web.download")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "web.status":
            return self._status(args)
        if cap == "web.discover":
            return self._discover(args)
        if cap == "web.corpus":
            return self._corpus(args)
        if cap == "web.diffuse":
            return self._diffuse(args)
        if cap == "web.papers":
            return self._papers(args)
        if cap == "web.frontier":
            return self._frontier(args)
        if cap == "web.sites":
            return self._sites(args)
        if cap in ("web.search", "web.fetch", "web.probe", "web.structure",
                   "web.download"):
            return self._skynet(cap, args)
        if cap == "web.quote":
            return self._quote(args)
        return super().invoke(cap, args)

    # ---------------- 自建逆向引擎（re_site，见模块头部的纪律）----------------

    def _quote(self, args):
        """行情精确查询：走 re_site 的站点适配器（腾讯/东财，只读公开接口）。

        和 web.search 的分工：search 答「网上怎么说」，quote 答「现在是多少」
        ——自建引擎的命中是结构化事实，不是排名。适配器失败照常回信封，
        不炸、不编（没查出来就说没查出来）。
        """
        q = (args.get("query") or "").strip()
        if not q:
            return R.unavailable("web.quote", "缺少 query（哪只股票/基金）",
                                 device=self.name, via=self.via)
        try:
            d = RS.quote(q, kind=args.get("kind"))
        except Exception as e:
            return R.error("web.quote",
                           f"re_site 适配器异常：{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        h = RS.hit_from(d) if d.get("ok") else None
        return R.success("web.quote", self.name, via=self.via, data={
            "query": q,
            "quote": d,
            "hits": [h] if h else [],
        })

    # ---------------- 采集内核（skynet，短任务，直接 import 同步调）----------------

    def _skynet(self, cap, args):
        """走 netforce 仓库里的 skynet 采集内核。

        🔴 短任务（一次搜索几十秒）⇒ 直接 import 同步调，拿结构化结果。
        长任务（跑一夜的资源发现）走上面的子进程路。两条路别混。
        """
        ok, why = NS.available()
        if not ok:
            return R.unavailable(cap, why, device=self.name, via=self.via)
        try:
            if cap == "web.search":
                only = args.get("only")
                only = [x.strip() for x in only.split(",") if x.strip()] if only else None
                hits = NS.search(
                    args.get("query") or "",
                    wiki_tag=(args.get("wiki_tag") or None),
                    only=only,
                    min_rel=(float(args["min_rel"]) if args.get("min_rel") is not None else None),
                    limit=int(args.get("limit") or 20))
                # 自建逆向引擎前置注入（2026-10-24，路线 P0 第一枪）：
                # 行情类查询先把精确答案顶到最前面——公网引擎答「哪篇资讯」，
                # 适配器答「现在多少钱」，两者不冲突，一起给。
                # 🔴 适配器任何异常都不许炸掉主搜索：降级成「这次没问出来」，
                # 公网结果照常返回。quote 只发一次网络请求（hit_from 复用）。
                qd = None
                try:
                    q = args.get("query") or ""
                    if RS.is_quote_query(q):
                        qd = RS.quote(q)
                        if qd.get("ok"):
                            h = RS.hit_from(qd)
                            if h:
                                hits = [h] + hits
                except Exception:
                    qd = None
                return R.success(cap, self.name, via=self.via, data={
                    "query": args.get("query") or "",
                    "count": len(hits),
                    "hits": hits,
                    "engines": NS.engine_health(),
                    "quote": qd,
                })
            if cap == "web.fetch":
                url = (args.get("url") or "").strip()
                if not url:
                    return R.unavailable(cap, "缺少 url（要取哪一页）",
                                         device=self.name, via=self.via)
                d = NS.fetch_text(url, timeout=float(args.get("timeout") or 15.0))
                return R.success(cap, self.name, via=self.via, data=d)
            if cap == "web.probe":
                url = (args.get("url") or "").strip()
                if not url:
                    return R.unavailable(cap, "缺少 url（要判哪一页）",
                                         device=self.name, via=self.via)
                d = NS.probe(url)
                return R.success(cap, self.name, via=self.via, data=d)
            if cap == "web.structure":
                url = (args.get("url") or "").strip()
                if not url:
                    return R.unavailable(cap, "缺少 url（要探哪个站）",
                                         device=self.name, via=self.via)
                d = NS.structure(url)
                return R.success(cap, self.name, via=self.via, data=d)
            if cap == "web.download":
                url = (args.get("url") or "").strip()
                dest = (args.get("dest") or "").strip()
                if not url or not dest:
                    return R.unavailable(cap, "缺少 url 或 dest",
                                         device=self.name, via=self.via)
                d = NS.download(url, dest)
                return R.success(cap, self.name, via=self.via, side_effect=True, data=d)
        except ImportError as e:
            return R.unavailable(cap, f"skynet 内核不在：{e}",
                                 device=self.name, via=self.via)
        except Exception as e:
            # 🔴 静默失败最阴：这里绝不能吞掉异常。
            #    失败必须带原因回去，让界面显示「为什么没成」。
            return R.error(cap, f"{type(e).__name__}: {e}", device=self.name,
                           via=self.via)
        return super().invoke(cap, args)

    # ---------------- 只读 ----------------

    def _status(self, args):
        st = NR.net_status()
        av = NR.available()
        return R.success("web.status", self.name, via=self.via, data={
            "netforce": st["netforce"], "papers": st["papers"],
            "data": st["data"],
            "papers_day": st["papers_day"], "papers_n": st["papers_n"],
            "sites_total": st["sites_total"],
            "anime_total": st["anime_total"], "anime_alive": st["anime_alive"],
        })

    def _sites(self, args):
        """站群活站清单。只读、瞬时——看的是本地已攒的那本账，不联网。"""
        d = NR.diffuse_out()
        if not d["total"]:
            return R.unavailable("web.sites",
                                 "还没盘过站群（先跑 web.diffuse 盘几个）",
                                 device=self.name, via=self.via)
        return R.success("web.sites", self.name, via=self.via, data={
            "total": d["total"], "alive": d["alive"], "at": d["at"],
            "sites": d["sites"],
        })

    def _frontier(self, args):
        """今天的论文清单。只读、瞬时——不联网也照样能看已经攒下的。"""
        d = NR.papers_out(args.get("day") or "")
        if not d["lines"]:
            return R.unavailable("web.frontier",
                                 "还没有论文产出（先跑 web.papers 抓一轮）",
                                 device=self.name, via=self.via)
        return R.success("web.frontier", self.name, via=self.via, data={
            "day": d["day"], "count": d["lines"],
            "items": [{"src": i.get("src"), "cat": i.get("cat"),
                       "title": i.get("title")} for i in d["items"]],
        })

    # ---------------- 动手：统一走子进程 ----------------

    def _need_netforce(self):
        cli = NR.netforce_cli()
        if not cli:
            return None, R.unavailable("web", "没装 netforce（NETFORCE_HOME 没指到仓库）",
                                       device=self.name, via=self.via)
        return cli, None

    def _need_papers(self):
        cli = NR.papers_cli()
        if not cli:
            return None, R.unavailable("web", "没装论文爬虫（PAPERS_HOME 没指到 train_15b）",
                                       device=self.name, via=self.via)
        return cli, None

    def _stream(self, cap, job, on_line):
        """跑一个作业，把它的每一行实时吐给回调，收工时汇报。"""
        self._job = job
        t0 = time.time()
        try:
            for ln in job.lines():
                if on_line:
                    on_line(ln)
        finally:
            self._job = None
        return time.time() - t0

    def _discover(self, args):
        """资源站发现。它自己是个跑到早上 6 点的无限循环，所以必须我们限时。"""
        cli, bad = self._need_netforce()
        if bad:
            return bad
        minutes = float(args.get("minutes") or 3)
        # 先记下跑之前的存量，收工才能报「这一趟新收了多少」
        before = NR.netforce_out()["total"]
        job = NR.Job([NR.PY, cli], cwd=NR.NETFORCE_HOME, tag="discover",
                     minutes=minutes).start()
        secs = self._stream("web.discover", job, args.get("_on_line"))
        out = NR.netforce_out()
        return R.success("web.discover", self.name, via=self.via,
                         side_effect=True, ms=secs * 1000, data={
                             "minutes": minutes, "stopped": True,
                             "sites_total": out["total"],
                             "new": max(0, out["total"] - before),
                             "tail": out["sites"][-5:],
                         })

    def _corpus(self, args):
        cli, bad = self._need_netforce()
        if bad:
            return bad
        tag = args.get("tag") or "blhx"
        limit = int(args.get("limit") or 10)
        job = NR.Job([NR.PY, cli, "corpus", tag, "--limit", str(limit)],
                     cwd=NR.NETFORCE_HOME, tag="corpus").start()
        secs = self._stream("web.corpus", job, args.get("_on_line"))
        out = NR.corpus_out(tag)
        return R.success("web.corpus", self.name, via=self.via,
                         side_effect=True, ms=secs * 1000, data={
                             "tag": tag, "limit": limit,
                             "lines": out["lines"], "samples": out["samples"][:3],
                         })

    def _diffuse(self, args):
        cli, bad = self._need_netforce()
        if bad:
            return bad
        limit = int(args.get("limit") or 10)
        job = NR.Job([NR.PY, cli, "diffuse", "--limit", str(limit)],
                     cwd=NR.NETFORCE_HOME, tag="diffuse").start()
        secs = self._stream("web.diffuse", job, args.get("_on_line"))
        out = NR.diffuse_out()
        return R.success("web.diffuse", self.name, via=self.via,
                         side_effect=True, ms=secs * 1000, data={
                             "limit": limit, "total": out["total"],
                             "alive": out["alive"], "sites": out["sites"][:8],
                         })

    def _papers(self, args):
        cli, bad = self._need_papers()
        if bad:
            return bad
        days = int(args.get("days") or 1)
        job = NR.Job([NR.PY, cli, "--days", str(days)],
                     cwd=NR.PAPERS_HOME, tag="papers").start()
        secs = self._stream("web.papers", job, args.get("_on_line"))
        out = NR.papers_out()
        return R.success("web.papers", self.name, via=self.via,
                         side_effect=True, ms=secs * 1000, data={
                             "days": days, "day": out["day"],
                             "saved": out["lines"], "items": out["items"][:5],
                         })

    # ---------------- 叫停 ----------------

    def stop(self):
        if self._job:
            self._job.stop()
            self._job = None
            return True
        return False
