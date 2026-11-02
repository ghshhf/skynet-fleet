# -*- coding: utf-8 -*-
"""论文落盘器：把公开论文直接爬进本地库，查文献不用再上网搜。

起因（主人 2026-11-03）：
  「这一轮是让你去爬取的是论文……论文相关里面很多东西能不用在网上搜索了，
    直接看本地就行了。同时顺手的话还能通过论文关联出一些其他域名之类的。」

分工（别和 resource_finder 混）：
  - resource_finder 的 paper 泵 = 论文当**引子**挖域名（abs 页出链 → 资源发现线）
  - 本脚本 = 论文**本身**落盘：PDF 全文 + 标题/作者/摘要元数据 → G:\\_papers\\
    摘要外链（github/项目主页）记进 links 字段，检索时可用。

数据源：arXiv 官方 RSS（零关键词零搜索，每天自动新鲜，单 feed 数百篇）。
限速：arXiv 官方要求爬虫间隔 ≥3 秒，照办（1 PDF/3s ≈ 1200 篇/小时）。
断点：state.json 记已下 id + PDF 文件存在性双保险，重启续传不重下。

用法：python paper_harvest.py            # 无限模式（默认）
      python paper_harvest.py 20         # 限量模式：本轮最多下 20 篇就停（试跑用）
"""
import json
import os
import re
import sys
import time
import datetime
import html as html_mod
import urllib.request
import urllib.parse

OUT = os.environ.get("PAPER_OUT", r"G:\_papers")
PDF_DIR = os.path.join(OUT, "pdf")
META = os.path.join(OUT, "papers.jsonl")
STATE = os.path.join(OUT, "state.json")
RSS_CATS = ("cs.LG", "cs.CL", "cs.AI", "cs.CV", "cs.RO", "stat.ML")
DELAY = 3.0            # arXiv 官方爬虫礼仪：请求间隔 ≥3s
UA = "Mozilla/5.0 (paper-harvest; skynet-fleet)"
CTX = __import__("ssl")._create_unverified_context()

# RSS 单条 item 的拆解（RDF 格式，零 XML 依赖正则拆，与项目风格一致）
ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.S)
TAG_RE = {
    "id":   re.compile(r"<link>https?://arxiv\.org/abs/([^<\s]+)</link>"),
    "title": re.compile(r"<title>(.*?)</title>", re.S),
    "abs":  re.compile(r"<description>(.*?)</description>", re.S),
    "auth": re.compile(r"<dc:creator>(.*?)</dc:creator>", re.S),
    "date": re.compile(r"<dc:date>(.*?)</dc:date>", re.S),
}
LINK_RE = re.compile(r"https?://[^\s\)\]\>\"'`<]+")


def _clean(s):
    """RSS 字段清洗：去 CDATA/HTML 标签/实体、压空白。"""
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(html_mod.unescape(s).split())


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def parse_rss(body):
    """RSS → [{id,title,abstract,authors,date,links}]"""
    out = []
    for m in ITEM_RE.finditer(body):
        it = m.group(1)
        mid = TAG_RE["id"].search(it)
        if not mid:
            continue
        abstract = _clean(TAG_RE["abs"].search(it).group(1)) if TAG_RE["abs"].search(it) else ""
        rec = {
            "id": mid.group(1).strip(),
            "title": _clean(TAG_RE["title"].search(it).group(1)) if TAG_RE["title"].search(it) else "",
            "abstract": abstract,
            "authors": _clean(TAG_RE["auth"].search(it).group(1)) if TAG_RE["auth"].search(it) else "",
            "date": TAG_RE["date"].search(it).group(1)[:10] if TAG_RE["date"].search(it) else "",
            # 摘要里自报家门的外链（github/项目主页）——关联域名的来源
            "links": sorted(set(LINK_RE.findall(abstract)))[:10],
        }
        out.append(rec)
    return out


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"done": []}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, STATE)


def harvest(limit=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    st = load_state()
    done = set(st.get("done", []))
    meta_f = open(META, "a", encoding="utf-8")
    n_new = 0
    for cat in RSS_CATS:
        try:
            body = fetch("https://export.arxiv.org/rss/" + cat).decode("utf-8", "ignore")
        except Exception as e:
            print("[paper] rss %s 失败: %s" % (cat, e), flush=True)
            continue
        items = parse_rss(body)
        print("[paper] %s → %d 篇（已下 %d）" % (cat, len(items), len(done)), flush=True)
        for rec in items:
            pid = rec["id"]
            if pid in done:
                continue
            pdf_path = os.path.join(PDF_DIR, pid.replace("/", "_") + ".pdf")
            if not os.path.exists(pdf_path):          # 文件存在即跳过（state 丢了也不重下）
                try:
                    data = fetch("https://arxiv.org/pdf/" + pid)
                    if not data.startswith(b"%PDF"):
                        raise ValueError("非 PDF 响应 %dB" % len(data))
                    with open(pdf_path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(pdf_path + ".tmp", pdf_path)
                    rec["pdf"] = pdf_path
                    rec["bytes"] = len(data)
                except Exception as e:
                    print("[paper] %s 下载失败: %s" % (pid, e), flush=True)
                    time.sleep(DELAY)
                    continue
            rec["cat"] = cat
            meta_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            meta_f.flush()
            done.add(pid)
            st["done"] = list(done)[-50000:]           # state 只留尾 5 万防膨胀
            save_state(st)
            n_new += 1
            if n_new % 10 == 0:
                print("[paper] 已落盘 %d 篇（本次会话）" % n_new, flush=True)
            if limit and n_new >= limit:
                meta_f.close()
                print("[paper] 限量 %d 篇已到，收工" % limit, flush=True)
                return
            time.sleep(DELAY)                           # arXiv 礼仪间隔
    meta_f.close()
    print("[paper] 本轮收工：新增 %d 篇，累计 %d 篇" % (n_new, len(done)), flush=True)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print("[paper] 落盘器启动 → %s（限速 %ss/篇，%s）"
          % (OUT, DELAY, "限量 %d" % limit if limit else "无限模式"), flush=True)
    if limit:
        harvest(limit)
        return
    started = time.time()
    while True:                                        # 无限模式：跟主爬虫一样到点续期
        try:
            harvest()
        except Exception as e:
            print("[paper] 轮异常（继续）: %s" % e, flush=True)
        print("[paper] 已运行 %.1f 小时，休眠 30 分钟等下一批新论文"
              % ((time.time() - started) / 3600), flush=True)
        time.sleep(1800)


if __name__ == "__main__":
    main()
