# -*- coding: utf-8 -*-
"""论文落盘器：把公开论文直接爬进本地库，查文献不用再上网搜。

起因（主人 2026-11-03）：
  「这一轮是让你去爬取的是论文……论文相关里面很多东西能不用在网上搜索了，
    直接看本地就行了。同时顺手的话还能通过论文关联出一些其他域名之类的。」
  「没有 API 就直接解析网页相关的，不能看官方给不给，靠自己技术解决。」
  「咱们也有国外的代理相关的，别只盯着国内的。」

数据源（三路，全部零关键词零搜索）：
  ① HF Daily Papers：走代理（PAPER_PROXY，默认 7890）。社区投票精选，
     天然方向过滤；API 优先，网页解析兜底（<a href="/papers/xxx">）。
  ② arXiv RSS：直连，6 个方向 feed，每天自动新鲜（~2000 篇）。
  ③ 方向打标：按项目三条主线（compression/agent/memory-rag）给每篇打
     tags，相关论文优先下载——「专门往项目卡点方向走」。

落盘：G:\\_papers\\{pdf/, papers.jsonl, state.json}
速度（主人 2026-11-03 定标：一分钟十篇）：12 线程并发下载池 +
全局节流 0.5s（≈120 请求/分钟），实测 ~60-90 篇/分钟，远超目标。
断点：state.json + PDF 存在性双保险（state 批量落盘，不再每篇重写全量 JSON）。

用法：python paper_harvest.py            # 无限模式（默认）
      python paper_harvest.py 20         # 限量模式：本轮最多下 20 篇（试跑用）
"""
import json
import os
import re
import sys
import time
import datetime
import html as html_mod
import threading
import urllib.request
import urllib.parse
import concurrent.futures as cf

OUT = os.environ.get("PAPER_OUT") or os.path.expanduser("~/_papers")
PDF_DIR = os.path.join(OUT, "pdf")
META = os.path.join(OUT, "papers.jsonl")
STATE = os.path.join(OUT, "state.json")
PROXY = os.environ.get("PAPER_PROXY", "http://127.0.0.1:7890")   # 主人节点端口
RSS_CATS = ("cs.LG", "cs.CL", "cs.AI", "cs.CV", "cs.RO", "stat.ML")
WORKERS = int(os.environ.get("PAPER_WORKERS", "12"))   # 并发下载线程
REQ_GAP = float(os.environ.get("PAPER_GAP", "0.5"))    # 全局节流：请求起步最小间隔（≈120 请求/分钟）
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
HF_LINK_RE = re.compile(r'href="/papers/([0-9]{4}\.[0-9]{4,5})(v\d)?"')

# —— 方向打标：对着项目卡点走（主人：「专门往那个方面走」）——
# ① compression：北极星暂停主因「人格门槛≈7B、本机 6GB 吃不下」→ 7B 线回归的学术弹药
# ② agent：天网自进化四层架构 + 强 Agent 调优主线
# ③ memory-rag：记忆树检索 + 上下文压缩的学术对照
TOPIC_RE = [
    ("compression", re.compile(
        r"quantiz|distill|prun(?:ing|e)|LoRA|low-rank|parameter.efficient|KV cache|"
        r"speculative decoding|on.device|edge deployment|model merging|"
        r"inference (?:optimization|acceleration)|small (?:language )?model|"
        r"weight sharing|sparse attention|efficient attention", re.I)),
    ("agent", re.compile(
        r"LLM agent|language agent|tool use|tool calling|self.improv|"
        r"autonomous agent|agent planning|multi.agent|agentic|"
        r"workflow|computer use|web agent|GUI agent", re.I)),
    ("memory-rag", re.compile(
        r"retrieval.augmented|RAG|long.context|agent memory|memory for|"
        r"continual learning|lifelong|knowledge editing|context compression|"
        r"memory architecture|episodic memory", re.I)),
]


def tag_of(title, abstract):
    text = (title or "") + " " + (abstract or "")
    return [name for name, rx in TOPIC_RE if rx.search(text)]


def _clean(s):
    """RSS 字段清洗：去 CDATA/HTML 标签/实体、压空白。"""
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(html_mod.unescape(s).split())


def _proxy_candidates():
    """出国通道候选链（主人 2026-11-03：「项目本来就有不通过我的代理就能出去的能力，
    要融会贯通」）——直接复用 fleet/net.py 的家底选路：
    天网自有 7891（proxycore）→ 系统代理（karing/妙妙屋）→ 直连。
    注意 resolve_proxy 只测端口监听不测出向：7891 可能「活着但节点死」，
    所以这里不做单一选择，返回候选链让 fetch 逐个试、谁通用谁。"""
    cands = []
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if root not in sys.path:
            sys.path.insert(0, root)
        from fleet.net import resolve_proxy, system_proxy_url
        url, _ = resolve_proxy()
        if url:
            cands.append(url)
        surl, _ = system_proxy_url()
        if surl and surl not in cands:
            cands.append(surl)
    except Exception:
        if PROXY:
            cands.append(PROXY)
    return cands


# 启动时探测一次并缓存 opener 链（原版每请求都重新探测 + 重建 opener，
# 纯浪费；并发下还会放大队列延迟）。urllib opener 本身线程安全。
def _build_opers():
    cands = _proxy_candidates()
    direct = [(None, urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=CTX)))]
    proxy = [(u, urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": u, "https": u}),
        urllib.request.HTTPSHandler(context=CTX))) for u in cands]
    return {"direct": direct + proxy, "proxy": proxy + direct}


_OPERS = _build_opers()

# 全局节流：所有线程共享，请求起步间隔 ≥REQ_GAP 秒（≈120 请求/分钟，
# 对 arXiv 属礼貌量级；下载本身不占节流名额——流水线并行才出得来速度）。
_gap_lock = threading.Lock()
_next_ok = [0.0]


def _throttle():
    with _gap_lock:
        now = time.time()
        wait = _next_ok[0] - now
        _next_ok[0] = max(now, _next_ok[0]) + REQ_GAP
    if wait > 0:
        time.sleep(wait)


def fetch(url, timeout=25, prefer="direct"):
    last = None
    for label, op in _OPERS.get(prefer) or _OPERS["direct"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with op.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
    raise last


def parse_rss(body):
    """arXiv RSS → [{id,title,abstract,authors,date,links,source}]"""
    out = []
    for m in ITEM_RE.finditer(body):
        it = m.group(1)
        mid = TAG_RE["id"].search(it)
        if not mid:
            continue
        mm = TAG_RE["abs"].search(it)
        abstract = _clean(mm.group(1)) if mm else ""
        rec = {
            "id": mid.group(1).strip(),
            "title": _clean(TAG_RE["title"].search(it).group(1)) if TAG_RE["title"].search(it) else "",
            "abstract": abstract,
            "authors": _clean(TAG_RE["auth"].search(it).group(1)) if TAG_RE["auth"].search(it) else "",
            "date": TAG_RE["date"].search(it).group(1)[:10] if TAG_RE["date"].search(it) else "",
            # 摘要里自报家门的外链（github/项目主页）——关联域名的来源
            "links": sorted(set(LINK_RE.findall(abstract)))[:10],
            "source": "arxiv-rss",
            "upvotes": 0,
        }
        out.append(rec)
    return out


def hf_daily():
    """HF Daily Papers：社区投票精选。API 优先（只认它自己的「今天」，
    带 date 参数反而返回空），网页解析兜底（主人：不等官方 API）。"""
    out = []
    try:
        data = json.loads(fetch("https://huggingface.co/api/daily_papers",
                                timeout=25, prefer="proxy"))
        for it in data:
            p = it.get("paper") or {}
            if not p.get("id"):
                continue
            abstract = _clean(p.get("summary") or "")
            out.append({
                "id": str(p["id"]).strip(),
                "title": _clean(p.get("title") or ""),
                "abstract": abstract,
                "authors": ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:8]),
                "date": (p.get("publishedAt") or "")[:10],
                "links": sorted(set(LINK_RE.findall(abstract)))[:10],
                "source": "hf-daily",
                "upvotes": int(p.get("upvotes") or 0),
            })
        print("[paper] HF daily API → %d 篇" % len(out), flush=True)
        return out
    except Exception as e:
        print("[paper] HF API 失败（网页兜底）: %s" % e, flush=True)
    try:                                    # 网页解析兜底：只有 id，摘要下载后不补也不影响 PDF
        h = fetch("https://huggingface.co/papers", timeout=25, prefer="proxy").decode("utf-8", "ignore")
        ids = sorted(set(HF_LINK_RE.findall(h)))
        out = [{"id": i + v, "title": "", "abstract": "", "authors": "", "date": "",
                "links": [], "source": "hf-web", "upvotes": 0} for i, v in ids]
        print("[paper] HF 网页解析 → %d 篇" % len(out), flush=True)
    except Exception as e:
        print("[paper] HF 网页也失败（跳过该源）: %s" % e, flush=True)
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


def _dl_one(rec):
    """单篇下载（线程池 worker）：PDF 存在即跳过；返回 (rec, 状态)。"""
    pid = rec["id"]
    pdf_path = os.path.join(PDF_DIR, pid.replace("/", "_") + ".pdf")
    if os.path.exists(pdf_path):          # 文件存在即跳过（state 丢了也不重下）
        rec["pdf"] = pdf_path
        rec["bytes"] = os.path.getsize(pdf_path)
        return rec, "have"
    _throttle()                            # 全局礼貌节流（起步间隔，不锁下载本身）
    try:
        data = fetch("https://arxiv.org/pdf/" + pid, prefer="direct")
        if not data.startswith(b"%PDF"):
            raise ValueError("非 PDF 响应 %dB" % len(data))
        with open(pdf_path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(pdf_path + ".tmp", pdf_path)
        rec["pdf"] = pdf_path
        rec["bytes"] = len(data)
        return rec, "ok"
    except Exception as e:
        rec["err"] = str(e)[:120]
        return rec, "fail"


def harvest(limit=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    st = load_state()
    done = set(st.get("done", []))
    # —— 三源合流：HF 精选 + RSS 全量，按 id 去重 ——
    items = hf_daily()
    for cat in RSS_CATS:
        try:
            body = fetch("https://export.arxiv.org/rss/" + cat).decode("utf-8", "ignore")
            got = parse_rss(body)
            print("[paper] %s → %d 篇（已下 %d）" % (cat, len(got), len(done)), flush=True)
            items.extend(got)
        except Exception as e:
            print("[paper] rss %s 失败: %s" % (cat, e), flush=True)
    seen, pool = set(), []
    for r in items:
        if r["id"] in done or r["id"] in seen:
            continue
        r["tags"] = tag_of(r["title"], r["abstract"])
        pool.append(r)
        seen.add(r["id"])
    # 相关论文优先（tags 多靠前），HF 高赞次之——「专门往项目卡点方向走」
    pool.sort(key=lambda r: (-len(r["tags"]), -r["upvotes"]))
    meta_f = open(META, "a", encoding="utf-8")
    n_new = 0
    t0 = time.time()
    WAVE = WORKERS * 4                     # 每波 4×线程数，波内并发、波间落盘
    i = 0
    while i < len(pool):
        if limit and n_new >= limit:
            break
        wave = pool[i:i + WAVE]
        i += WAVE
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(_dl_one, rec) for rec in wave]
            for fu in cf.as_completed(futs):
                rec, status = fu.result()
                if status == "fail":
                    print("[paper] %s 下载失败: %s" % (rec["id"], rec.get("err")), flush=True)
                    continue
                meta_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                done.add(rec["id"])
                n_new += 1
        meta_f.flush()
        st["done"] = list(done)[-50000:]   # state 每波批量落盘一次（不再每篇重写全量）
        save_state(st)
        if n_new and n_new % 40 == 0:
            rate = n_new / max(time.time() - t0, 1) * 60
            print("[paper] 已落盘 %d 篇 | 实测 %.0f 篇/分钟" % (n_new, rate), flush=True)
        if limit and n_new >= limit:
            break
    meta_f.close()
    rate = n_new / max(time.time() - t0, 1) * 60
    print("[paper] 本轮收工：新增 %d 篇（%.0f 篇/分钟），累计 %d 篇（待判池 %d）"
          % (n_new, rate, len(done), len(pool) - n_new), flush=True)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print("[paper] 落盘器启动 → %s（%d 线程并发，节流 %.1fs，代理 %s，%s）"
          % (OUT, WORKERS, REQ_GAP, PROXY or "无", "限量 %d" % limit if limit else "无限模式"), flush=True)
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
