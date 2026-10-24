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

from . import authority as AUTH
from . import netrunner as NR

#: 环境变量**名**。注意：这是名字，不是路径值。
#: 🔴 之前误写成 `NR.NETFORCE_HOME or "NETFORCE_HOME"`——那是**路径**当成了变量名，
#:    于是 `os.environ.get(路径)` 永远取不到值，模块会一路 fallback 到默认目录
#:    ⇒ 设了环境变量也指不到别处（实测：门禁因此连上了真网，跑了几分钟不返回）。
NETFORCE_HOME_ENV = "NETFORCE_HOME"
#: 备用位置：环境变量没设时，在这些目录下找带 `skynet/` 的那一个。
#: 🔴 **单一真源**：候选列表由 `netrunner` 维护，本模块直接复用。
#:    2026-10-24 之前这里另写了一份 `[D:\code\netforce, ~/code/netforce]`，
#:    与 netrunner 的常量各自为政 ⇒ 改一处另一处仍是死路径，`web.*` 照样降级。
#:    候选里第一条是「与本仓并列」推断——换盘换机不用再手动指路径。
DEFAULT_NETFORCE_DIRS = NR.DEFAULT_NETFORCE_DIRS

#: 返回给调用方的标题硬上限。上游（skynet）已经修了刷屏仓库，这里是**边界防御**：
#: 标题最终会被拼进模型上下文，谁也不能保证上游永远干净。
#: 实测事故：一条 GitHub 刷屏仓库的 description 有 5.9 万字，5 条凑出 31 万字，
#: 直接撑爆手机模型 4096 上下文（llama-server 400）。截断比报错重要，但必须标出来。
TITLE_MAX = 300

#: 摘要上限，与 skynet 的 DESC_MAX 同值。上游已截，这里是边界防御
#: （同标题：谁也不能保证上游永远干净，而摘要最终也进模型上下文）。
DESC_MAX = 240


def netforce_home():
    """找 netforce 仓库。找不到返回 ""。

    判定顺序：
      ① 环境变量 NETFORCE_HOME **设了** ⇒ 就认它。指错了就是指错了
         （不 fallback 到别处——悄悄换一个源比报错更坏）。
      ② 没设 ⇒ 看默认目录列表（与 `netrunner` 共用同一份，单一真源）。
    两处都要真的有 `skynet/` 子目录才算数（老版本 netforce 没有这个包）。
    """
    p = os.environ.get(NETFORCE_HOME_ENV, "").strip()
    if p:
        return p if os.path.isdir(os.path.join(p, "skynet")) else ""
    for d in DEFAULT_NETFORCE_DIRS:
        if d and os.path.isdir(os.path.join(d, "skynet")):
            return d
    return ""


def _ensure_path():
    """把 netforce 仓库塞进 sys.path。没塞就 import skynet ⇒ ModuleNotFoundError。

    🔴 **这里曾经漏了**：只有 `available()` 会走 `_skynet()` 注入路径，
    而 `search()`/`probe()`/`fetch_text()` 等直接 `from skynet import ...`
    ⇒ 首次调用时 netforce 明明在 `D:\\code\\netforce`，却报
    「No module named 'skynet'」（真机实测踩到）。
    门禁用假 skynet 包、路径已在 sys.path 里，所以没抓出来。
    教训：**注入路径的副作用必须发生在每个使用点**，不能只放在"检查"函数里。
    """
    home = netforce_home()
    if not home:
        raise ImportError(
            "没找到 netforce（天网采集内核）："
            f"把环境变量 {NETFORCE_HOME_ENV} 指向 netforce 仓库，"
            f"或放到 {' / '.join(DEFAULT_NETFORCE_DIRS)} 之一")
    if home not in sys.path:
        sys.path.insert(0, home)
    return home


def _skynet():
    """import skynet 包。失败抛 ImportError（消息说清怎么装）。"""
    _ensure_path()
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
    _ensure_path()
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
        raw_title = (h.title or "").strip()
        title = raw_title[:TITLE_MAX]
        rec = {
            "url": h.url,
            "host": h.host,
            "title": title,
            # desc 是搜索引擎给的摘要——广告话术（导流词、品类词、入口词）
            # 最密集的一段。2026-10-24 之前这一层整个丢弃，下游只剩标题可看，
            # 等于自废一半身份证据（见 docs/违法广告向量研究报告）。
            "desc": (getattr(h, "desc", "") or "")[:DESC_MAX],
            "engines": list(h.engines),
            "cross": len(h.engines),
            "rel": round(float(h.rel), 4),
            "score": round(float(h.score), 4),
        }
        if len(raw_title) > TITLE_MAX:
            # 不静默：调用方有权知道这条是被砍过的，砍过就说明上游出了问题
            rec["title_truncated"] = len(raw_title)
        # 证据分级：引擎摘要与相关度都是外部生成的 ⇒ historical-reference。
        # cross 引擎交叉只提高可信度，不改变来源（那是权重，不是 authority）
        AUTH.attest(rec, "web.search",
                    f"搜索引擎命中（{rec['cross']} 引擎交叉）",
                    subject=query, authority=AUTH.authority_of_hit(rec),
                    value=rec["url"],
                    limitations=["摘要与相关度为搜索方生成，非目标站原文"])
        out.append(rec)
    return out


def engine_health():
    """各搜索引擎的健康度。只读。

    🔴 skynet 的 `HEALTH` 字典键是 `引擎名` 或 `引擎名|语言桶`，
    字段是 `in/out/calls`（提交数/存活数/调用次数）——不是 submitted/alive。
    """
    _ensure_path()
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

def fetch_text(url, timeout=15.0, budget=25.0, gap=0.8, force_channel=None,
               max_chars=4000):
    """取一个 URL 的正文。返回 dict（不抛异常，失败也带原因）。

    🔴 skynet 的 `net.fetch_text()` 返回 `FetchResult`（dataclass），字段是
    `url/ok/status/size/final_url/channel/elapsed/text/data/error/cached/hops`。
    这里把它拍平成 dict，好让上层能力信封直接 JSON 化。

    `max_chars` 控制 `text` 的截断长度，默认 4000（能力信封回传体积别太大）。
    ⚠️ **调用方要正文时务必调大**：`skynet` 的 text 是**整页原始 HTML**，
    4000 字往往连 `<head>` 都没走完 —— 正文压根不在里面。联网检索
    （`webtool.py`）就是踩了这个坑：拿到的「正文」全是 og:/meta 标签。
    注意 `chars` 字段始终是**全文**长度，与截断无关。
    """
    _ensure_path()
    from skynet import net as N
    res = N.fetch_text(url, timeout=timeout, budget=budget, gap=gap,
                       force_channel=force_channel)
    text = getattr(res, "text", "") or ""
    out = {
        "url": getattr(res, "url", url),
        "final_url": getattr(res, "final_url", "") or "",
        "ok": bool(res.ok),
        "status": getattr(res, "status", 0),
        "channel": getattr(res, "channel", ""),
        "size": getattr(res, "size", 0),
        "chars": len(text),
        "text": text[:max_chars],
        "hops": list(getattr(res, "hops", []) or []),
        "error": getattr(res, "error", "") or "",
    }
    # 抓成功 ⇒ 页面字节是实物读到的；失败 ⇒ 失败是观察，「为什么失败」是推断
    AUTH.attest(out, "web.fetch", "取正文结果", subject=url,
                authority=AUTH.authority_of_fetch(out),
                value=out["final_url"] or url,
                limitations=(["失败原因（墙/超时/截断）为推断"] if not out["ok"] else []))
    return out


def download(url, dest, timeout=60.0, max_bytes=200 * 1024 * 1024, dedupe=True):
    """下载一个 URL 到 dest（**真 Range 续传**，断点续跑）。

    skynet 的 `net.download()` 落盘后按内容 sha256 去重（同内容硬链接），
    这对音频资源很有用：不同 URL 常常指向同一份字节。
    """
    _ensure_path()
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
    _ensure_path()
    from skynet import probe as P
    res = P.probe(url, timeout=timeout, gap=gap)
    c = getattr(res, "content", None)
    out = {
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
    # probe 是我们主动跑一遍的探测 ⇒ controlled-replay；没探到东西时任何结论都是猜
    AUTH.attest(out, "web.probe", "四级可用性判定", subject=url,
                authority=AUTH.authority_of_probe(out),
                value=f"{out['verdict']}（score={out['score']}）",
                limitations=(["未探到实质内容"] if out["verdict"] == "unknown" else []))
    return out


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
    _ensure_path()
    from skynet import discover as D
    res = D.structure(url, max_urls=max_urls)
    if not isinstance(res, dict):
        res = dict(getattr(res, "__dict__", {}) or {})
    files = res.get("sitemap_files")
    sample = res.get("sample")
    feed = res.get("feed")
    out = {
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
    # robots/sitemap 是站点主动发布的实物字节 ⇒ shipped-artifact。
    # ⚠️ 站点声明 ≠ 真实存在（sitemap 里可以有死链）——那是引用时的事
    AUTH.attest(out, "web.structure", "站结构（站点主动发布）", subject=url,
                authority=AUTH.authority_of_structure(out),
                value=f"sitemap_urls={out['sitemap_urls']}",
                limitations=["站点声明不等于真实存在，sitemap 内可有死链"])
    return out


# --------------------------------------------------------------------------
# 跳转穿透 + DNS 污染判据
# --------------------------------------------------------------------------

def origin(url, max_hops=10, timeout=10.0):
    """逐跳穿透一个 URL，直到终点。返回 dict。

    干什么用：短链 / 跳板页 / meta refresh / JS location 都算跳转。
    只读原始 URL 会把跳板页本身当成目标——它的正文通常只有一行跳转脚本，
    拿回来当内容是静默零产出（`probe()` 的 L4 解析校验防的正是这类）。

    🔴 skynet 的 `route.trace()` 返回 `Trace`（dataclass），字段
    `start/final/short/hops/error/cross_domain`；每跳是 `Hop`
    （`n/url/status/location/kind/host/set_cookie`，kind ∈ http/meta/js/end）。
    这里全部拍平，别让上层去猜属性名。
    """
    _ensure_path()
    from skynet import route as R
    t = R.trace(url, max_hops=max_hops, timeout=timeout)
    hops = [{
        "n": int(getattr(h, "n", 0) or 0),
        "url": getattr(h, "url", "") or "",
        "status": int(getattr(h, "status", 0) or 0),
        "kind": getattr(h, "kind", "") or "",
        "host": getattr(h, "host", "") or "",
        "location": (getattr(h, "location", "") or "")[:500],
        "set_cookie": int(getattr(h, "set_cookie", 0) or 0),
    } for h in (getattr(t, "hops", None) or [])]
    return {
        "start": getattr(t, "start", url) or url,
        "final": getattr(t, "final", "") or "",
        "hops": hops,
        "hop_count": len(hops),
        "cross_domain": bool(getattr(t, "cross_domain", False)),
        "short": bool(getattr(t, "short", False)),
        "error": getattr(t, "error", "") or "",
    }


#: dns.check() 的全部 verdict（上游语义：无交集只报可疑，不下死结论）
DNS_VERDICTS = ("ok", "suspicious", "doh_failed", "system_failed")


def dns_check(url, timeout=6.0):
    """对比系统解析与 DoH，判断是否疑似被污染。返回 dict。

    干什么用：本机链路会把域名带到假 IP（连得上、返回错内容，或直接 reset）。
    表现为 fetch 成功但内容不对——比报错更难发现。

    🔴 **判据不能是「两边 IP 不同」**（上游 docstring 写明，实测踩过）：
      CDN 站本来就有十几个 IP，按地域/线路给出不同子集完全正常，
      那样判会把所有 CDN 站误报成投毒。真正的特征是**两边完全没有交集**，
      所以无交集只报 `suspicious`，不下死结论。
    """
    _ensure_path()
    from skynet import dns as D
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").strip()
    if not host:
        return {"host": "", "verdict": "system_failed", "error": "URL 里没有 host",
                "system": [], "doh": [], "common": [], "only_system": []}
    res = D.check(host, timeout=timeout) or {}
    verdict = res.get("verdict", "")
    return {
        "host": res.get("host", host) or host,
        "verdict": verdict if verdict in DNS_VERDICTS else "suspicious",
        "system": list(res.get("system") or []),
        "doh": list(res.get("doh") or []),
        "common": list(res.get("common") or []),
        "only_system": list(res.get("only_system") or []),
    }


def resolve_hint(url, timeout=6.0):
    """给 curl 的 `--resolve` 拼参数：`host:port:ip`。拿不到返回 ""。

    只读帮手：上层自己起 curl 时，用 DoH 结果绕开被污染的系统解析。
    拿不到就返回空串，调用方照常用系统解析——不假装绕成功了。
    """
    _ensure_path()
    from skynet import dns as D
    try:
        return D.resolve_for_curl(url, timeout=timeout) or ""
    except Exception as e:                        # noqa: BLE001
        return ""


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
