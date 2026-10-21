"""netforce「天网」采集内核（skynet/）的适配层。

这一层把**别人写好的**采集内核接进本仓的能力面：
多引擎搜索（含意图校验）+ 带续传的下载 + 四级可用性判定。

🔴 **为什么不把 netforce 的代码搬进来（vendoring）**：
  跟 `fleet/kinds/render.py` 找 `ruic-motion-reel` 引擎是同一条纪律——
  **代码不进来，只做适配层**。搬进来有两个坏处：
  ① 仓库体积暴涨；
  ② 对方升级要靠人肉同步两份（本仓会静默用到旧版）。
  所以这里只 import，不复制；netforce 不在 ⇒ 如实说「没装」，不静默降级。

🔴 **为什么 netforce 的 `resource_finder.py` 走子进程、skynet 走 import**：
  · `resource_finder.py` 是**长任务**（跑到早上 6 点的无限流）⇒ 必须子进程，
    否则界面卡死、且没法中途叫停。既有 `web.discover` 就是这么做的。
  · `skynet/` 是**短任务**（一次搜索几十秒、一次下载一分多钟）⇒ 直接 import
    同步调，拿到结构化结果，比解析 stdout 稳得多。
  两条路都存在，各按性质选。

🔴 **本层解决的真问题（都是实测踩出来的）**：
  ① **搜索结果的污染**：Bing RSS 在中国区被污染，查「碧蓝航线 语音 台词 wiki」
     返回香港楼盘、查「open source ... dataset」返回剑桥词典的 open 词条。
     skynet 的 `search()` 有**意图校验**（token 覆盖率打分）+ **引擎健康度**
     （长期 0 相关的引擎自动降权停用）⇒ 本仓直接受益。
  ② **下载中途截断**：本机链路会截断（实测 359KB / 256KB 断点），
     半截文件当成功 = 静默失败。skynet 的 `download()` 是**真 Range 续传**
     且留 `.part` 续传 ⇒ 本仓直接受益。
  ③ **死链与壳页**：页面能打开不等于有内容。skynet 的 `probe()` 四级判定
     （可达 / 实质 / 密度 / 解析校验）能把「活着但没产出」的页挑出来。

能力四项（都在 `web.*` 族下，与既有 web.discover/web.corpus 并列）：
  web.search  只读：多引擎聚合搜索，带意图校验与引擎健康度
  web.fetch   只读：取一个 URL 的正文（带熔断/限速/镜像降级）
  web.probe   只读：四级判定「这页值不值得抓」
  web.structure 只读：探站结构（robots / sitemap / feed）＝定向扩散的入口
"""
from __future__ import annotations

import os
import sys

from . import netrunner as NR

#: 环境变量**名**。注意：这是名字，不是路径值。
#: 🔴 之前误写成 `NR.NETFORCE_HOME or "NETFORCE_HOME"`——那是**路径**当成了变量名，
#:    于是 `os.environ.get(路径)` 永远取不到值，模块会一路 fallback 到默认目录
#:    ⇒ 设了环境变量也指不到别处（实测：门禁因此连上了真网，跑了几分钟不返回）。
NETFORCE_HOME_ENV = "NETFORCE_HOME"
#: 备用位置：环境变量没设时，在这些目录下找带 `skynet/` 的那一个。
DEFAULT_NETFORCE_DIRS = [
    r"D:\code\netforce",
    os.path.join(os.path.expanduser("~"), "code", "netforce"),
]


def netforce_home():
    """找 netforce 仓库。找不到返回 ""。

    判定顺序：
      ① 环境变量 NETFORCE_HOME **设了** ⇒ 就认它。指错了就是指错了
         （不 fallback 到别处——悄悄换一个源比报错更坏）。
      ② 没设 ⇒ 看默认目录列表。
    两处都要真的有 `skynet/` 子目录才算数（老版本 netforce 没有这个包）。
    """
    p = os.environ.get(NETFORCE_HOME_ENV, "").strip()
    if p:
        return p if os.path.isdir(os.path.join(p, "skynet")) else ""
    cands = [NR.NETFORCE_HOME] + DEFAULT_NETFORCE_DIRS
    for d in cands:
        if d and os.path.isdir(os.path.join(d, "skynet")):
            return d
    return ""


def _skynet():
    """import skynet 包。失败抛 ImportError（消息说清怎么装）。"""
    home = netforce_home()
    if not home:
        raise ImportError(
            "没找到 netforce（天网采集内核）："
            f"把环境变量 {NETFORCE_HOME_ENV} 指向 netforce 仓库，"
            f"或放到 {' / '.join(DEFAULT_NETFORCE_DIRS)} 之一")
    if home not in sys.path:
        sys.path.insert(0, home)
    import skynet  # noqa: F401
    return sys.modules["skynet"]


def available():
    """内核在不在。返回 (bool, 原因)。"""
    home = netforce_home()
    if not home:
        return False, (f"没装 netforce：把 {NETFORCE_HOME_ENV} 指向 netforce 仓库"
                       f"（或放到 {' / '.join(DEFAULT_NETFORCE_DIRS)}）")
    try:
        _skynet()
    except Exception as e:
        return False, f"netforce 在 {home} 但 import 失败：{type(e).__name__}: {e}"
    return True, ""


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------

def search(query, wiki_tag=None, only=None, min_rel=None, limit=20,
           budget=18.0, workers=5):
    """多引擎聚合搜索。返回命中列表（每条是 dict，不是 skynet 的 Hit 对象）。

    `only` 给引擎名列表（如 ["ddg","github"]）可只跑指定引擎。
    `min_rel` 低于此相关度的结果被丢掉（skynet 默认阈值 0.18 已够严）。
    `wiki_tag` 是 MediaWiki 站点的标签——**不给就不跑 wiki 引擎**（needs_tag）。
    """
    from skynet import search as S
    kwargs = {"wiki_tag": wiki_tag, "only": only,
              "workers": workers, "budget": budget}
    if min_rel is not None:
        kwargs["min_rel"] = min_rel
    hits = S.search(query, **kwargs)
    if limit and limit > 0:
        hits = hits[:limit]
    out = []
    for h in hits:
        out.append({
            "url": h.url,
            "host": h.host,
            "title": h.title,
            "engines": list(h.engines),
            "cross": len(h.engines),
            "rel": round(float(h.rel), 4),
            "score": round(float(h.score), 4),
        })
    return out


def engine_health():
    """各搜索引擎的健康度。只读。

    🔴 skynet 的 `HEALTH` 字典键是 `引擎名` 或 `引擎名|语言桶`，
    字段是 `in/out/calls`（提交数/存活数/调用次数）——不是 submitted/alive。
    """
    from skynet import search as S
    S.bootstrap_health()
    out = []
    for key in sorted(getattr(S, "HEALTH", {})):
        rec = S.HEALTH[key]
        name = key.split("|")[0]
        out.append({
            "engine": name,
            "lang": key.split("|")[1] if "|" in key else "",
            "submitted": int(rec.get("in", 0) or 0),
            "alive": int(rec.get("out", 0) or 0),
            "calls": int(rec.get("calls", 0) or 0),
            "weight": round(float(S.health_weight(key)), 4),
        })
    return out


# --------------------------------------------------------------------------
# 取正文
# --------------------------------------------------------------------------

def fetch_text(url, timeout=15.0, budget=25.0, gap=0.8, force_channel=None):
    """取一个 URL 的正文。返回 dict（不抛异常，失败也带原因）。

    🔴 skynet 的 `net.fetch_text()` 返回 `FetchResult`（dataclass），字段是
    `url/ok/status/size/final_url/channel/elapsed/text/data/error/cached/hops`。
    这里把它拍平成 dict，好让上层能力信封直接 JSON 化。
    """
    from skynet import net as N
    res = N.fetch_text(url, timeout=timeout, budget=budget, gap=gap,
                       force_channel=force_channel)
    text = getattr(res, "text", "") or ""
    return {
        "url": getattr(res, "url", url),
        "final_url": getattr(res, "final_url", "") or "",
        "ok": bool(res.ok),
        "status": getattr(res, "status", 0),
        "channel": getattr(res, "channel", ""),
        "size": getattr(res, "size", 0),
        "chars": len(text),
        "text": text[:4000],
        "hops": list(getattr(res, "hops", []) or []),
        "error": getattr(res, "error", "") or "",
    }


def download(url, dest, timeout=60.0, max_bytes=200 * 1024 * 1024, dedupe=True):
    """下载一个 URL 到 dest（**真 Range 续传**，断点续跑）。

    skynet 的 `net.download()` 落盘后按内容 sha256 去重（同内容硬链接），
    这对音频资源很有用：不同 URL 常常指向同一份字节。
    """
    from pathlib import Path
    from skynet import net as N
    res = N.download(url, Path(dest), timeout=timeout,
                     max_bytes=max_bytes, dedupe=dedupe)
    return {
        "url": getattr(res, "url", url),
        "ok": bool(res.ok),
        "status": getattr(res, "status", 0),
        "channel": getattr(res, "channel", ""),
        "size": getattr(res, "size", 0),
        "cached": bool(getattr(res, "cached", False)),
        "error": getattr(res, "error", "") or "",
    }


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

#: verdict 的全部取值（来自 skynet.probe.verdict_of）
VERDICTS = ("valuable", "portal", "thin", "dead", "blocked", "junk", "unknown")


def probe(url, timeout=15.0, gap=0.6):
    """四级判定：可达 / 实质 / 密度 / 解析校验。返回 dict。

    🔴 skynet 的 `probe()` 返回 `Probe`（dataclass），真实字段是
    `url/alive/status/final_url/size/title/content/score/verdict/reasons`。
    `content` 是 `Content`（含 text_chars/text_density/candidates/audio_links…）。
    这里全部拍平，别让上层去猜属性名。
    """
    from skynet import probe as P
    res = P.probe(url, timeout=timeout, gap=gap)
    c = getattr(res, "content", None)
    return {
        "url": getattr(res, "url", url),
        "final_url": getattr(res, "final_url", "") or "",
        "alive": bool(getattr(res, "alive", False)),
        "status": getattr(res, "status", 0),
        "size": getattr(res, "size", 0),
        "title": getattr(res, "title", "") or "",
        "score": int(getattr(res, "score", 0) or 0),
        "verdict": getattr(res, "verdict", "unknown"),
        "reasons": list(getattr(res, "reasons", []) or []),
        "text_chars": int(getattr(c, "text_chars", 0) or 0) if c else 0,
        "text_density": round(float(getattr(c, "text_density", 0.0) or 0.0), 4) if c else 0.0,
        "tables": int(getattr(c, "tables", 0) or 0) if c else 0,
        "rows": int(getattr(c, "rows", 0) or 0) if c else 0,
        "candidates": int(getattr(c, "candidates", 0) or 0) if c else 0,
        "audio_links": int(getattr(c, "audio_links", 0) or 0) if c else 0,
        "junk_hit": (getattr(c, "junk_hit", "") or "") if c else "",
    }


# --------------------------------------------------------------------------
# 站内结构（定向扩散：robots + sitemap 递归 + feed）
# --------------------------------------------------------------------------

def structure(url, max_urls=1500):
    """探一个站里有什么：robots / sitemap / feed。返回 dict。

    🔴 skynet 的 `discover.structure()` 返回的是**普通 dict**（不是 dataclass），
    键为 `root/robots/crawl_delay/sitemap_files/sitemap_urls/truncated/ext/sample/feed`。
    它没有 timeout 参数（内部自己控），别照抄别的函数的签名。

    🔴 **键的类型别猜**（实测踩过）：
      sitemap_files 是 **int**（找到几个 sitemap 文件），不是列表；
      sitemap_urls 是 **int**（里面总共有多少条 URL）；
      feed 可能是 **None**（站上没有 feed），不是空串。
    这里统一拍平成稳定类型：计数用 int，列表用 list，缺失用空值。
    """
    from skynet import discover as D
    res = D.structure(url, max_urls=max_urls)
    if not isinstance(res, dict):
        res = dict(getattr(res, "__dict__", {}) or {})
    files = res.get("sitemap_files")
    sample = res.get("sample")
    feed = res.get("feed")
    return {
        "root": res.get("root", "") or "",
        "robots": res.get("robots", "") or "",
        "crawl_delay": float(res.get("crawl_delay", 0) or 0),
        "sitemap_files": int(files or 0),
        "sitemap_urls": int(res.get("sitemap_urls", 0) or 0),
        "truncated": bool(res.get("truncated", False)),
        "ext": dict(res.get("ext") or {}),
        "sample": list(sample) if isinstance(sample, (list, tuple)) else [],
        "feed": feed or "",
    }


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

def _selftest():
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [ok] {label}")
        else:
            fail += 1
            print(f"  [FAIL] {label}")

    home = netforce_home()
    check(bool(home), f"找到 netforce：{home or '(没找到)'}")

    av, why = available()
    if not av:
        # 没装不是代码坏了 ⇒ 如实说清怎么装，不算失败
        print(f"  [--] netforce 不可用，跳过功能自测：{why}")
        print(f"[skynet_bridge] 自测：{ok} 通过 / {fail} 失败（内核缺席，不计失败）")
        return 0 if fail == 0 else 1

    check(True, "skynet 内核 import 成功")

    from skynet import search as S
    names = [e.name for e in getattr(S, "ENGINES", [])]
    check(bool(names), f"搜索引擎清单非空：{names}")

    # 意图校验函数在（这是 skynet 的核心价值之一）
    rel = S.relevance("game voice lines dataset", "https://x.com/voice-lines",
                      "Game Voice Lines Dataset")
    check(rel > 0.5, f"相关的结果 rel 分数高（{rel:.2f}）")
    rel2 = S.relevance("game voice lines dataset", "https://hk.example/flat",
                       "清水湾楼盘")
    check(rel2 < rel, f"不相关的结果 rel 分数低（{rel2:.2f} < {rel:.2f}）")

    print(f"[skynet_bridge] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
