# -*- coding: utf-8 -*-
"""天网 · 爬虫设备：承载 crawl.* 能力（crawlee 主提取）。

每个 crawl.* 能力已在 fleet/vendored_crawler.py 带 check= 诚实探测底层引擎
（crawlee / playwright+chromium / trafilatura / bs4 / httpx）。
invoke 按 cap 路由到 handler：已接真 handler 的引擎出真实数据，
crawlee 系深度能力（crawl_site / scrape_structured / anti_block 等）先诚实待接入，
装好 crawlee 后补异步 handler 即可，不谎报可用。
"""
import os

from ..core import BaseDevice, DeviceResult as R
from ..vendored_crawler import ENGINE_OF

# playwright 浏览器目录：**存在才用**，不存在就交给 playwright 自己的默认路径
# （%LOCALAPPDATA%\ms-playwright）。
#
# 原来这里无条件写成 D:/DevTools/crawler/playwright-browsers —— 那个目录早就没了，
# 后果是「谁 import 本模块，谁的进程里 playwright 就集体找不到浏览器」，一 launch 就
# 报「Looks like Playwright was just installed」。t18 的门禁活线检查就是这么红的
# （2026-11-02 定位）。环境变量是进程级全局副作用，不能这么随手写。
_PW_HOME = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "D:/DevTools/crawler/playwright-browsers"
if os.path.isdir(_PW_HOME):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _PW_HOME
else:
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

_HANDLERS = {}


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


@_reg("crawl.http_fetch")
def _h_http_fetch(cap, args, dev):
    import httpx
    url = args.get("url")
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    try:
        r = httpx.get(url, headers=args.get("headers") or {},
                      timeout=args.get("timeout") or 30, follow_redirects=True)
    except Exception as e:
        return R.error(cap, "请求失败：%s" % e, device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "url": str(r.url), "status": r.status_code,
        "headers": dict(r.headers), "body": r.text[:200000],
        "truncated": len(r.text) > 200000})


@_reg("crawl.render")
def _h_render(cap, args, dev):
    from playwright.sync_api import sync_playwright
    url = args.get("url")
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    wait = args.get("wait") or 3
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, timeout=30000)
            pg.wait_for_timeout(wait * 1000)
            html = pg.content()
            b.close()
    except Exception as e:
        return R.error(cap, "渲染失败：%s" % e, device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "url": url, "html": html[:300000], "truncated": len(html) > 300000})


@_reg("crawl.parse_html")
@_reg("crawl.discover_links")
def _h_parse(cap, args, dev):
    import bs4
    import httpx
    from urllib.parse import urlparse
    url = args.get("url")
    html = args.get("html")
    if not html and url:
        try:
            html = httpx.get(url, timeout=30, follow_redirects=True).text
        except Exception as e:
            return R.error(cap, "抓取失败：%s" % e, device=dev.name, via=dev.via)
    if not html:
        return R.error(cap, "html 或 url 至少给一个", device=dev.name, via=dev.via)
    soup = bs4.BeautifulSoup(html, "html.parser")
    if cap == "crawl.discover_links":
        base = url or ""
        same = args.get("same_domain", True)
        links = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if same and base:
                net = urlparse(h).netloc
                if net and net != urlparse(base).netloc:
                    continue
            links.append(h)
        return R.success(cap, dev.name, via=dev.via,
                         data={"links": links[:5000], "count": len(links)})
    title = soup.title.get_text() if soup.title else ""
    text = soup.get_text("\n", strip=True)
    links = [a["href"] for a in soup.find_all("a", href=True)]
    return R.success(cap, dev.name, via=dev.via, data={
        "title": title, "text": text[:200000], "links": links[:2000],
        "truncated": len(text) > 200000})


@_reg("crawl.extract_article")
def _h_article(cap, args, dev):
    import trafilatura
    import httpx
    url = args.get("url")
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    try:
        html = httpx.get(url, timeout=30, follow_redirects=True).text
    except Exception as e:
        return R.error(cap, "抓取失败：%s" % e, device=dev.name, via=dev.via)
    extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
    meta = trafilatura.extract_metadata(html)
    if not extracted:
        return R.error(cap, "trafilatura 未提取到正文（可能非文章页）",
                       device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "title": getattr(meta, "title", None) if meta else None,
        "author": getattr(meta, "author", None) if meta else None,
        "date": getattr(meta, "date", None) if meta else None,
        "text": extracted[:100000]})


@_reg("crawl.robots_sitemap")
def _h_robots(cap, args, dev):
    import httpx
    url = args.get("url")
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
    except Exception as e:
        return R.error(cap, "请求失败：%s" % e, device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "url": str(r.url), "status": r.status_code, "content": r.text[:50000]})


@_reg("crawl.crawl_site")
def _h_crawl_site(cap, args, dev):
    """整站爬取：crawlee BeautifulSoupCrawler + enqueue_links 同域扩散。"""
    import asyncio
    import crawlee
    from crawlee.crawlers import BeautifulSoupCrawler
    import bs4

    start = args.get("start_url")
    if not start:
        return R.error(cap, "start_url 必填", device=dev.name, via=dev.via)
    max_pages = int(args.get("max_pages") or 50)
    max_depth = int(args.get("max_depth") or 2)
    proxy = args.get("proxy")  # 逗号分隔多个代理 -> 轮换

    try:
        crawler_kwargs = dict(
            max_requests_per_crawl=max_pages,
            max_crawl_depth=max_depth,
        )
        if proxy:
            from crawlee.proxy_configuration import ProxyConfiguration
            urls = [u.strip() for u in str(proxy).split(",") if u.strip()]
            crawler_kwargs["proxy_configuration"] = ProxyConfiguration(proxy_urls=urls)
        crawler = BeautifulSoupCrawler(**crawler_kwargs)
    except Exception as e:
        return R.error(cap, "构建 crawler 失败：%s" % e, device=dev.name, via=dev.via)

    store = []

    @crawler.router.default_handler
    async def handler(context):
        url = context.request.url
        soup = getattr(context, "soup", None) or bs4.BeautifulSoup(context.body, "html.parser")
        title = soup.title.get_text() if soup.title else ""
        text = soup.get_text("\n", strip=True)
        links = [a["href"] for a in soup.find_all("a", href=True)]
        store.append({"url": url, "title": title,
                      "text_len": len(text), "links": links[:200]})
        context.log.info("crawled %s (%d chars, %d links)", url, len(text), len(links))
        # 同域扩散：受 max_requests_per_crawl / max_crawl_depth 限制
        await context.enqueue_links()

    try:
        asyncio.run(crawler.run([start]))
    except Exception as e:
        return R.error(cap, "爬取执行异常：%s" % e, device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "start_url": start, "pages": store, "page_count": len(store),
        "engine": "crawlee:BeautifulSoupCrawler",
        "config": {"max_pages": max_pages, "max_depth": max_depth,
                   "concurrency": "autoscaled by crawlee", "proxy": bool(proxy)}})


@_reg("crawl.scrape_structured")
def _h_scrape_structured(cap, args, dev):
    """按 selector 字段映射抽取结构化数据（CSS 为主，lxml XPath 兜底）。"""
    import httpx
    import bs4
    url = args.get("url")
    selectors = args.get("selectors") or {}
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    if not selectors:
        return R.error(cap, "selectors 必填（字段名->CSS/XPath）", device=dev.name, via=dev.via)
    try:
        html = httpx.get(url, timeout=30, follow_redirects=True).text
    except Exception as e:
        return R.error(cap, "抓取失败：%s" % e, device=dev.name, via=dev.via)
    soup = bs4.BeautifulSoup(html, "html.parser")
    fields = {}
    for name, sel in selectors.items():
        nodes = []
        try:
            nodes = soup.select(sel)
        except Exception:
            nodes = []
        if not nodes:
            try:
                sx = bs4.BeautifulSoup(html, "lxml")
                nodes = sx.xpath(sel)
            except Exception:
                nodes = []
        fields[name] = [n.get_text(strip=True) for n in nodes[:50]]
    return R.success(cap, dev.name, via=dev.via,
                     data={"url": url, "fields": fields, "engine": "bs4/css+xpath"})


@_reg("crawl.export_dataset")
def _h_export_dataset(cap, args, dev):
    """把爬取结果落 Dataset：返回 json/csv 字符串（也可带 file 参数落盘）。"""
    import json
    import csv
    import io
    data = args.get("data")
    fmt = (args.get("fmt") or "json").lower()
    path = args.get("file")
    if data is None:
        return R.error(cap, "data 必填", device=dev.name, via=dev.via)
    content = None
    if fmt == "csv":
        buf = io.StringIO()
        if data and isinstance(data, list) and isinstance(data[0], dict):
            flds = []
            for row in data:
                for k in row:
                    if k not in flds:
                        flds.append(k)
            w = csv.DictWriter(buf, fieldnames=flds)
            w.writeheader()
            for row in data:
                w.writerow(row)
        else:
            buf.write(str(data))
        content = buf.getvalue()
    else:
        content = json.dumps(data, ensure_ascii=False, indent=2)
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return R.success(cap, dev.name, via=dev.via,
                             data={"fmt": fmt, "file": path, "bytes": len(content.encode("utf-8"))})
        except Exception as e:
            return R.error(cap, "落盘失败：%s" % e, device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data={
        "fmt": fmt, "content": content[:200000], "bytes": len(content.encode("utf-8"))})


@_reg("crawl.anti_block")
@_reg("crawl.proxy_rotate")
@_reg("crawl.session_manage")
@_reg("crawl.autoscale")
def _h_crawlee_param(cap, args, dev):
    """crawlee 运行时参数维度：单独调用无副作用，应作为 crawl_site 参数使用。"""
    return R.success(cap, dev.name, via=dev.via, data={
        "role": "runtime-parameter",
        "note": "该能力是 crawl_site 的运行时参数维度；单独调用不执行动作，请携带下列参数调用 crawl.crawl_site",
        "use_with": "crawl.crawl_site",
        "parameters": {
            "proxy": "代理地址(逗号分隔多个) -> ProxyConfiguration 轮换出口",
            "use_session_pool": "会话池(默认开) -> 模拟稳定访客、携带 cookie/指纹",
            "stealth": "浏览器 stealth 指纹；仅 PlaywrightCrawler / JS 渲染站点生效",
            "max_concurrency": "最大并发 -> AutoscaledPool 上限",
        },
        "crawl_site_params": ["start_url", "max_pages", "max_depth",
                              "proxy", "use_session_pool", "max_concurrency"]})


class CrawlerDevice(BaseDevice):
    kind = "crawler"
    via = "local"

    def __init__(self, name="crawler:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "crawler 爬虫能力（crawlee 主提取）；底层引擎不在场如实下线")

    def probe(self):
        return True, ""

    def caps(self):
        return tuple(ENGINE_OF.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in ENGINE_OF:
            return R.unsupported(cap, "crawler 设备没有这个能力",
                                 device=self.name, via=self.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(
                cap, "该爬虫能力已声明；引擎 %s 的 handler 待接入" % ENGINE_OF[cap],
                device=self.name, via=self.via)
        try:
            return h(cap, args, self)
        except Exception as e:
            return R.error(cap, "handler 执行异常：%s" % e, device=self.name, via=self.via)
