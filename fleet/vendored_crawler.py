# -*- coding: utf-8 -*-
"""天网 · 爬虫能力注入（crawlee 主提取 · 方案 A：全量声明 + 诚实依赖检查）

来源：apify/crawlee + crawlee-python（按能力维度提取，非工具清单）。
底层引擎：crawlee / playwright+chromium / scrapy / trafilatura / bs4 / parsel / httpx。
提取口径同 REA：能力全量声明，check= 诚实探测底层引擎；引擎不在场则该能力离线。
"""
import importlib.util as _ilu
import os
import glob

from .core import Capability, Param

# 引擎 -> (探测模块列表, 人类可读标签)
_ENGINE_MODS = {
    "httpx": (["httpx"], "httpx / requests（底层 HTTP）"),
    "bs4": (["bs4"], "BeautifulSoup（HTML 解析）"),
    "parsel": (["parsel"], "Parsel / lxml（XPath 解析）"),
    "trafilatura": (["trafilatura"], "trafilatura（正文提取）"),
    "playwright": (["playwright"], "playwright + chromium（无头渲染）"),
    "crawlee": (["crawlee"], "crawlee（Apify 爬虫框架）"),
    "scrapy": (["scrapy"], "Scrapy（备选爬虫框架）"),
    # 纯标准库实现的 handler 不该被第三方引擎绑架：check 恒真。
    "stdlib": ([], "标准库（无第三方依赖）"),
}


def _find_specs(mods):
    return [m for m in mods if _ilu.find_spec(m) is None]


def _check(mods, label, reg, dev):
    miss = _find_specs(mods)
    if miss:
        return False, "需要 %s（未安装：%s）" % (label, ", ".join(miss))
    return True, ""


def _chromium_present():
    cands = []
    bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if bp:
        cands.append(bp)
    cands.append(os.environ.get("FLEET_CRAWLER_BROWSERS") or "")
    cands.append(os.path.expanduser("~/.cache/ms-playwright"))
    for c in cands:
        if glob.glob(os.path.join(c, "chromium-*")):
            return True
    return False


def _check_playwright(mods, label, reg, dev):
    ok, msg = _check(mods, label, reg, dev)
    if not ok:
        return ok, msg
    if not _chromium_present():
        return False, "需要 chromium 浏览器（PLAYWRIGHT_BROWSERS_PATH / ~/.cache/ms-playwright 下未找到）"
    return True, ""


def _mk(engine):
    mods, label = _ENGINE_MODS.get(engine, ([engine], engine))
    if engine == "playwright":
        return lambda reg, dev: _check_playwright(mods, label, reg, dev)
    return lambda reg, dev: _check(mods, label, reg, dev)


ENGINE_OF = {
    "crawl.http_fetch": "httpx",
    "crawl.render": "playwright",
    "crawl.parse_html": "bs4",
    "crawl.extract_article": "trafilatura",
    "crawl.discover_links": "bs4",
    "crawl.crawl_site": "crawlee",
    # scrape_structured 的 handler 走 httpx + bs4（XPath 兜底才用 lxml），并不 import crawlee。
    # 声明成 crawlee 会让「装了 bs4 就能用」的能力在没装 crawlee 的机器上被误判为离线。
    "crawl.scrape_structured": "bs4",
    "crawl.anti_block": "crawlee",
    "crawl.proxy_rotate": "crawlee",
    "crawl.session_manage": "crawlee",
    # export_dataset 只用了 json/csv 两个标准库，不依赖任何第三方引擎。
    "crawl.export_dataset": "stdlib",
    "crawl.robots_sitemap": "httpx",
    "crawl.autoscale": "crawlee",
    "crawl.diagnose": "httpx",
}

CRAWLER_CAPABILITIES = [
    Capability("crawl.http_fetch", "原始 HTTP 抓取：自定义 header/cookie/timeout，返回状态码+响应头+正文",
               params=(Param("url", "str", True, "目标 URL"),
                       Param("headers", "dict", doc="自定义请求头"),
                       Param("timeout", "int", doc="秒，默认 30")),
               side_effect=False, check=_mk("httpx"), note="[CRAWL] 依赖 httpx"),
    Capability("crawl.render", "无头 chromium 渲染 JS 页面，返回渲染后完整 HTML（绕过静态抓取拿不到的 SPA 内容）",
               params=(Param("url", "str", True, "目标 URL"),
                       Param("wait", "int", doc="等几秒让 JS 跑完，默认 3")),
               side_effect=False, check=_mk("playwright"), note="[CRAWL] 依赖 playwright+chromium"),
    Capability("crawl.parse_html", "用 BeautifulSoup 解析 HTML，抽取标题/正文文本/全部链接",
               params=(Param("html", "str", doc="HTML 源码"),
                       Param("url", "str", doc="二选一：直接给 URL 让它先抓")),
               side_effect=False, check=_mk("bs4"), note="[CRAWL] 依赖 bs4"),
    Capability("crawl.extract_article", "trafilatura 提取网页正文：标题/作者/日期/正文/元数据，去导航广告",
               params=(Param("url", "str", True, "目标 URL")),
               side_effect=False, check=_mk("trafilatura"), note="[CRAWL] 依赖 trafilatura"),
    Capability("crawl.discover_links", "从页面提取外链清单（含同域/跨域过滤），用于扩散发现",
               params=(Param("url", "str", True, "起始页"),
                       Param("same_domain", "bool", doc="只要同域，默认 true")),
               side_effect=False, check=_mk("bs4"), note="[CRAWL] 依赖 bs4"),
    Capability("crawl.crawl_site", "整站爬取：enqueue_links 同域扩散，限制深度与最大请求数，落 Dataset",
               params=(Param("start_url", "str", True, "入口 URL"),
                       Param("max_pages", "int", doc="最多几页，默认 50"),
                       Param("max_depth", "int", doc="最大链接深度，默认 2"),
                       Param("proxy", "str", doc="代理(逗号分隔多个) -> 轮换出口"),
                       Param("max_concurrency", "int", doc="提示值；实际并发由 crawlee AutoscaledPool 自适应")),
               side_effect=False, check=_mk("crawlee"), note="[CRAWL] 依赖 crawlee"),
    Capability("crawl.scrape_structured", "按 CSS/XPath selector 或字段 schema 抽取结构化数据",
               params=(Param("url", "str", True, "目标 URL"),
                       Param("selectors", "dict", doc="字段名->selector 映射")),
               side_effect=False, check=_mk("bs4"),
               note="[CRAWL] 依赖 bs4 + httpx；XPath 兜底需 lxml（可选）"),
    Capability("crawl.anti_block", "反检测抓取：代理轮换 + 会话池 + 浏览器 stealth 指纹，绕过 bot 防护",
               params=(Param("url", "str", True, "目标 URL"),
                       Param("proxy", "str", doc="代理地址，如 http://ip:port")),
               side_effect=False, check=_mk("crawlee"), note="[CRAWL] 依赖 crawlee（proxy/session）"),
    Capability("crawl.proxy_rotate", "配置代理池（ProxyConfiguration），多出口轮换",
               params=(Param("urls", "list", True, "代理 URL 列表")),
               side_effect=False, check=_mk("crawlee"), note="[CRAWL] 依赖 crawlee"),
    Capability("crawl.session_manage", "会话池管理（SessionPool）：cookie/指纹/错误计数，模拟稳定访客",
               params=(Param("url", "str", True, "目标 URL")),
               side_effect=False, check=_mk("crawlee"), note="[CRAWL] 依赖 crawlee"),
    Capability("crawl.export_dataset", "把爬取结果落 Dataset 并导出 JSON/CSV",
               params=(Param("data", "list", True, "待导出记录"),
                       Param("fmt", "str", doc="json/csv，默认 json")),
               side_effect=False, check=_mk("stdlib"), note="[CRAWL] 纯标准库（json/csv），无第三方依赖"),
    Capability("crawl.robots_sitemap", "读取并解析 robots.txt / sitemap.xml，判断可抓取范围",
               params=(Param("url", "str", True, "站点根或 robots/sitemap URL")),
               side_effect=False, check=_mk("httpx"), note="[CRAWL] 依赖 httpx"),
    Capability("crawl.diagnose", "网络诊断：探测一次 URL，产出结构化协议证据（耗时/重定向/状态码）与故障诊断（错误分类+下一步建议）",
               params=(Param("url", "str", True, "目标 URL"),
                       Param("timeout", "int", doc="秒，默认 30")),
               side_effect=False, check=_mk("httpx"), note="[CRAWL] 依赖 httpx · 抓包方向第一版"),
    Capability("crawl.autoscale", "并发自适应（AutoscaledPool）：按系统负载自动调爬取并发",
               params=(Param("url", "str", True, "目标 URL")),
               side_effect=False, check=_mk("crawlee"), note="[CRAWL] 依赖 crawlee"),
]
