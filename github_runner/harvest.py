"""天网 · X 资源收割机 v2（云端 / 关联扩散）
=============================================
跑在 Kaggle CPU 内核，本机不用开机。

和 v1 的区别：v1 只处理给定 ID，v2 做**关联扩散** —— 一次种子能长出一片。

扩散三条边
----------
    账号 --timeline--> 推文 --@mention--> 新账号        （人）
    推文 --外链--> t.co --> 真实地址 --> 页面           （内容）
    页面 --GitHub owner--> 该作者的其他 repo             （仓库）
    页面 --正文里的链接--> 更多页面                       （深层）

每条边都进 visited 去重，深度与总量都有上限，不会失控。

为什么慢也能用
--------------
X 的 timeline 接口是**缓存**（实测 github 停在 10-28、OpenAI 停在 10-26）。
它追不了新，但能考古，而且是免费的、不需要登录的、可以无限并发的。
慢的发现也是发现 —— 反正云上是白给的 CPU。

用法
----
    HARVEST_OUT=./out python harvest.py        # 本地试跑
    kaggle kernels push -p .                   # 上云
    kaggle kernels output skynet93560862/x-harvest -p ./out
"""
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
}
CTX = ssl.create_default_context()
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
# 默认写到仓库内的 data/，随每次 commit 回传 —— 断点续传就是靠它。
OUT = os.environ.get("HARVEST_OUT", "data")
# 单轮时间预算。Actions 单 job 硬上限 6 小时，这里留 20 分钟做收尾和提交。
# 到点不是"失败"，是"这一轮先到这，剩下的下一轮接着跑"。
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "20400"))   # 5 小时 40 分

# ------------------------------------------------------------------ 任务配置
# 种子：AI / 开源 / 工具 / 资源类账号。名字写错不会报错，只是抓不到。
SEED_ACCOUNTS = [
    "github", "OpenAI", "huggingface", "_akhaliq", "kaggle",
    "GoogleDeepMind", "AIatMeta", "MistralAI", "deepseek_ai",
    "simonw", "karpathy", "ylecun", "DrJimFan", "goodside",
    "levelsio", "shawmakesmagic", "LangChainAI", "llama_index",
    "ollama", "vllm_project", "ggerganov", "TheBlokeAI",
    "reach_vb", "philschmid", "cwolferesearch", "rohanpaul_ai",
    "TheTuringPost", "akshay_pachaar", "mntruell", "AlexHormozi",
]
# 直接指定 ID（本机浏览器搜到的），优先级最高
SEED_IDS = [
    "2117001357672825165",
    "2116992117163831525",
    "2116985948894375983",
    "2116976274857959747",
    "2116975052155507200",
    "2116982693543960913",
    "2116973323460735200",
    "2116827081015541830",
]

MAX_ACCOUNTS = 80       # 扩散后账号上限
MAX_TWEETS = 900        # 推文上限
MAX_URLS = 500          # 外链上限
MAX_DEPTH = 2           # 账号扩散深度
WORKERS = 10            # 并发（CPU 活，拉满）

# ------------------------------------------------------------------ 基础
# 429 是真实会遇到的：短时间内密集请求 syndication 接口必被限流。
# 所以这里做两件事：① 429/503 指数退避重试 ② 对 X 域名做全局节流。
import threading

_LOCK = threading.Lock()
_LAST_X = [0.0]
X_GAP = float(os.environ.get("X_GAP", "0.35"))   # 对 X 域名的两次请求最小间隔（秒）


def _throttle_x():
    with _LOCK:
        gap = time.time() - _LAST_X[0]
        if gap < X_GAP:
            time.sleep(X_GAP - gap)
        _LAST_X[0] = time.time()


def get(url, timeout=25, data=None, headers=None, retries=3):
    h = dict(UA)
    if headers:
        h.update(headers)
    is_x = "twimg.com" in url or "twitter.com" in url or "t.co/" in url
    last = None
    for attempt in range(retries + 1):
        if is_x:
            _throttle_x()
        try:
            req = urllib.request.Request(url, data=data, headers=h,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503, 502):
                # 1.5s → 3s → 6s，别硬撞
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
    raise last


def token(tweet_id):
    v = (int(tweet_id) / 1e15) * math.pi
    ip, frac = int(v), v - int(v)
    s = ""
    while ip:
        ip, r = divmod(ip, 36)
        s = DIGITS[r] + s
    s = s or "0"
    if frac > 0:
        f = ""
        for _ in range(24):
            frac *= 36
            d = int(frac)
            f += DIGITS[d]
            frac -= d
            if frac <= 0:
                break
        s += "." + f
    return re.sub(r"0+|\.", "", s)


# ------------------------------------------------------------------ 单条操作
def tweet(tid, retries=1):
    url = ("https://cdn.syndication.twimg.com/tweet-result"
           "?id=%s&token=%s&lang=en" % (tid, token(tid)))
    for i in range(retries + 1):
        try:
            body, _ = get(url, timeout=18)
            d = json.loads(body)
            u = d.get("user") or {}
            ents = d.get("entities") or {}
            urls = [x.get("expanded_url") for x in (ents.get("urls") or [])
                    if x.get("expanded_url")]
            mentions = [x.get("screen_name") for x in (ents.get("user_mentions") or [])
                        if x.get("screen_name")]
            return dict(
                id=str(d.get("id_str") or tid),
                author=u.get("screen_name") or "",
                name=u.get("name") or "",
                created=d.get("created_at") or "",
                text=d.get("text") or "",
                urls=urls,
                mentions=mentions,
                tags=[x.get("text") for x in (ents.get("hashtags") or []) if x.get("text")],
                likes=d.get("favorite_count") or 0,
                lang=d.get("lang") or "",
                url="https://x.com/%s/status/%s" % (u.get("screen_name") or "i", tid),
                links=[],
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(0.4 * (i + 1))
        except Exception:
            time.sleep(0.4 * (i + 1))
    return None


def timeline(name, limit=40):
    """账号时间线 → ID 列表。缓存，可能滞后数天，但免费且无需登录。"""
    url = "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + name.lstrip("@")
    try:
        html, _ = get(url, timeout=25)
    except Exception:
        return []
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return []
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except Exception:
        return []
    out = []
    for ent in entries:
        c = ent.get("content") or {}
        tw = c.get("tweet") or c.get("itemContent", {}).get("tweet_results", {}).get("result")
        if not tw:
            continue
        # 三种结构都见过：rest_id / id_str / legacy.id_str
        tid = (tw.get("rest_id") or tw.get("id_str")
               or (tw.get("legacy") or {}).get("id_str") or tw.get("id"))
        if tid:
            out.append(str(tid))
        if len(out) >= limit:
            break
    return out


def expand_tco(url):
    if "t.co/" not in (url or ""):
        return url or ""
    try:
        _, final = get(url, timeout=10)
        return "" if final.startswith("https://t.co/") else final
    except Exception:
        return ""


# ------------------------------------------------------------------ 深挖
def notion_page(url):
    m = re.search(r"([0-9a-f]{32})", url.replace("-", ""), re.I)
    if not m:
        return None
    h = m.group(1).lower()
    pid = "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])
    body = json.dumps({"pageId": pid, "limit": 300, "cursor": {"stack": []},
                       "chunkNumber": 0, "verticalColumns": False}).encode()
    for host in ("https://www.notion.so", "https://app.notion.com"):
        try:
            raw, _ = get(host + "/api/v3/loadPageChunk", data=body,
                         headers={"Content-Type": "application/json"}, timeout=28)
            blocks = json.loads(raw).get("recordMap", {}).get("block", {})
            lines = []
            for b in blocks.values():
                v = b.get("value") or {}
                v = v.get("value", v) or {}
                props = v.get("properties") or {}
                segs = props.get("title") or props.get("Name") or []
                t = "".join(s[0] for s in segs
                            if isinstance(s, list) and s and isinstance(s[0], str)).strip()
                if t:
                    lines.append(t)
            return "\n".join(lines) if lines else None
        except Exception:
            continue
    return None


def gh_repo(repo):
    try:
        raw, _ = get("https://api.github.com/repos/" + repo,
                     headers={"Accept": "application/vnd.github+json"})
        d = json.loads(raw)
        return dict(repo=d.get("full_name") or repo,
                    title=d.get("full_name") or repo,
                    text=(d.get("description") or "")[:600],
                    stars=d.get("stargazers_count"),
                    lang=d.get("language"),
                    pushed=d.get("pushed_at"),
                    topics=d.get("topics") or [],
                    owner=(d.get("owner") or {}).get("login"))
    except Exception:
        return None


def gh_owner_repos(owner, limit=10):
    """关联扩散：一个仓库 → 同作者的其他仓库。"""
    try:
        raw, _ = get("https://api.github.com/users/%s/repos?sort=updated&per_page=%d"
                     % (owner, limit),
                     headers={"Accept": "application/vnd.github+json"})
        return [x["html_url"] for x in json.loads(raw) if not x.get("fork")]
    except Exception:
        return []


_URL_RE = re.compile(r'https?://[^\s"\'<>\\)\]]+')

# 页面里混进来的通用域名：登录页、CDN、统计、播放器外壳。
# 不去掉的话，一次扩散会被这些东西灌满（实测 122 条里有 30 多条是 google 登录页）。
_JUNK = (
    "accounts.google.com", "drive.google.com", "docs.google.com", "mail.google.com",
    "sites.google.com", "connect.corp.google.com", "play.google.com",
    "google.com/", "gstatic.com", "googleapis.com", "googleusercontent.com",
    "w3.org", "schema.org", "facebook.com", "instagram.com", "linkedin.com",
    "doubleclick.net", "googletagmanager", "cloudflare.com/cdn-cgi",
    "github.com/login", "github.com/signup", "github.com/features",
    "notion.so/login", "apple.com/app", "microsoft.com/store",
)


def _is_junk(u: str) -> bool:
    ul = (u or "").lower()
    if not ul.startswith("http"):
        return True
    if any(s in ul for s in _JUNK):
        return True
    if any(ul.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".svg",
                                    ".css", ".js", ".woff", ".woff2", ".ico",
                                    ".mp4", ".mp3", ".zip")):
        return True
    return False



def page_title(url, deep_links=0):
    try:
        html, _ = get(url, timeout=18)
    except Exception:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    out = {"url": url, "kind": "web", "title": title[:200], "text": body[:600]}
    if deep_links:
        # 不做"像不像噪音"的域名判断，只排掉静态资源和自身回环。
        # 有量之后才谈分辨力 —— 没资源的时候，过滤掉的往往就是唯一线索。
        found = []
        for u in _URL_RE.findall(html):
            u = u.rstrip(".,;")
            if not u.startswith("http"):
                continue
            if any(u.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif",
                                                   ".svg", ".css", ".js", ".ico",
                                                   ".woff", ".woff2", ".mp4")):
                continue
            if any(s in u for s in ("x.com/", "twitter.com/", "t.co/")):
                continue
            found.append(u)
        out["found"] = list(dict.fromkeys(found))[:deep_links]
    return out


def dig(url):
    if not url:
        return None
    if "notion." in url or "notion.site" in url:
        txt = notion_page(url)
        if txt:
            return {"url": url, "kind": "notion",
                    "title": txt.split("\n")[0][:150], "text": txt[:6000],
                    "found": [u.rstrip(".,;") for u in _URL_RE.findall(txt)
                              if "t.co" not in u and not _is_junk(u)][:15]}
    if "github.com" in url:
        m = re.search(r"github\.com/([^/\s]+/[^/\s?#]+)", url)
        if m:
            r = gh_repo(m.group(1).rstrip("/."))
            if r:
                r["url"] = url
                r["kind"] = "github"
                return r
    r = page_title(url, deep_links=12)
    if r:
        return r
    return {"url": url, "kind": "unknown"}


# ------------------------------------------------------------------ 主流程
def _load_done(stream):
    """读已完成的记录 —— 断点续传靠它。本机随时可以关，回来接着跑。"""
    done = {"account": set(), "tweet": set(), "link": set()}
    if not os.path.exists(stream):
        return done
    n = 0
    with open(stream, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = r.get("t")
            if k == "account":
                done["account"].add((r.get("name") or "").lower())
            elif k == "tweet":
                done["tweet"].add(str(r.get("id")))
            elif k == "link":
                done["link"].add(r.get("url"))
            n += 1
    print("续传：已有 %d 条记录（账号 %d / 推文 %d / 链接 %d）"
          % (n, len(done["account"]), len(done["tweet"]), len(done["link"])), flush=True)
    return done


def _emit(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_all(stream):
    """从流式文件重建全量结果。"""
    tweets, links, accs = [], [], []
    with open(stream, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = r.get("t")
            if k == "tweet":
                r.pop("t", None)
                tweets.append(r)
            elif k == "link":
                r.pop("t", None)
                links.append(r)
            elif k == "account":
                accs.append(r.get("name"))
    return tweets, links, accs


def main():
    """流式主循环。

    为什么不用并发：所有请求都打向同一批域名（twimg / twitter / t.co），
    并发只会互相抢限流、触发 429。**串行 + 节流 + 增量落盘**才是对的。
    并发只在跨域名时才有意义，这里不做。
    """
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    stream = os.path.join(OUT, "stream.jsonl")
    done = _load_done(stream)

    # ---------- 1. 账号 → 推文 ID（流式，一个账号一条记录）
    tweet_ids = list(SEED_IDS)
    for acc in SEED_ACCOUNTS:
        if time.time() - t0 > MAX_SECONDS:
            print("时间预算到，账号阶段收尾（%.0fs）" % (time.time() - t0), flush=True)
            break
        if acc.lower() in done["account"]:
            continue
        ids = timeline(acc, 40)
        _emit(stream, {"t": "account", "name": acc, "ids": ids,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        done["account"].add(acc.lower())
        tweet_ids.extend(ids)
        print("  @%-18s %3d 条  [累计 ID %d]" % (acc, len(ids), len(tweet_ids)), flush=True)

    tweet_ids = [t for t in dict.fromkeys(tweet_ids) if t not in done["tweet"]][:MAX_TWEETS]
    print("待补全推文 %d 条（%.0fs）" % (len(tweet_ids), time.time() - t0), flush=True)

    # ---------- 2. 推文补全（流式）
    queue = deque()
    done_n = 0
    for tid in tweet_ids:
        if time.time() - t0 > MAX_SECONDS:
            print("时间预算到，补全阶段收尾（%.0fs）" % (time.time() - t0), flush=True)
            break
        t = tweet(tid)
        done_n += 1
        if t:
            _emit(stream, dict(t, t="tweet"))
            for u in t["urls"]:
                queue.append(u)
            for m in re.finditer(r"https://t\.co/\w+", t["text"]):
                queue.append(m.group(0))
        if done_n % 25 == 0:
            print("  推文 %d/%d（%.0fs）" % (done_n, len(tweet_ids), time.time() - t0), flush=True)

    # 已入库推文的链接也要进队列（续传时不能漏）
    tweets_all, _, _ = _read_all(stream)
    for t in tweets_all:
        queue.extend(t.get("urls") or [])
        queue.extend(re.findall(r"https://t\.co/\w+", t.get("text") or ""))
    print("阶段2完成：推文 %d 条，链接队列 %d（%.0fs）"
          % (len(tweets_all), len(queue), time.time() - t0), flush=True)

    # ---------- 3. 链接深挖 + 二跳扩散（流式 BFS：队列边跑边长）
    skip_host = ("x.com/", "twitter.com/", "t.co/")
    seen = set(done["link"])
    pending = deque()
    for u in queue:
        if u and u not in seen:
            seen.add(u)
            pending.append(u)

    dug_n, hop2_n = 0, 0
    while pending and dug_n < MAX_URLS:
        if time.time() - t0 > MAX_SECONDS:
            print("时间预算到，链接阶段收尾（%.0fs）" % (time.time() - t0), flush=True)
            break
        url = pending.popleft()
        if any(s in url for s in skip_host):
            real = expand_tco(url)
            if not real or any(s in real for s in skip_host):
                continue
            if real in seen:
                continue
            seen.add(real)
            url = real
        d = dig(url)
        dug_n += 1
        if d:
            _emit(stream, dict(d, t="link"))
            # 二跳：页面里发现的链接直接入队 —— 这就是「关联爬取」
            for u in (d.get("found") or []):
                if u in seen:
                    continue
                # 只挡两种：X 自身回环、纯静态文件。
                # 域名"像不像噪音"不做判断 —— 先要量，判断放在有量之后。
                if any(s in u for s in ("x.com/", "twitter.com/", "t.co/")):
                    continue
                if any(u.lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif",
                                                       ".svg", ".css", ".js", ".ico",
                                                       ".woff", ".woff2", ".mp4")):
                    continue
                seen.add(u)
                pending.append(u)
                hop2_n += 1
        if dug_n % 20 == 0:
            print("  链接 %d（队列还剩 %d，二跳发现 %d，%.0fs）"
                  % (dug_n, len(pending), hop2_n, time.time() - t0), flush=True)

    # ---------- 4. 汇总落盘
    tweets, links, accs = _read_all(stream)
    gh = [d for d in links if d.get("kind") == "github"]
    nt = [d for d in links if d.get("kind") == "notion"]
    web = [d for d in links if d.get("kind") == "web"]
    stats = {"accounts": len(set(accs)), "tweets": len(tweets), "urls": len(links),
             "github": len(gh), "notion": len(nt), "web": len(web),
             "elapsed_s": round(time.time() - t0)}

    with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "stats": stats, "tweets": tweets, "links": links,
                   "accounts": sorted({a for a in accs if a})},
                  f, ensure_ascii=False, indent=1)

    md = ["# X 资源收割 · %ds" % stats["elapsed_s"], "",
          "账号 %d · 推文 %d · 链接 %d" % (stats["accounts"], len(tweets), len(links)), ""]
    if gh:
        md.append("## GitHub 仓库（%d）" % len(gh))
        for d in sorted(gh, key=lambda x: -(x.get("stars") or 0))[:60]:
            md.append("- **%s** ⭐%s %s — %s" %
                      (d.get("repo"), d.get("stars"), d.get("lang") or "",
                       (d.get("text") or "")[:110]))
        md.append("")
    if nt:
        md.append("## Notion 文档（%d）" % len(nt))
        for d in nt:
            md.append("- [%s](%s)" % (d.get("title"), d["url"]))
        md.append("")
    if web:
        md.append("## 网页（%d）" % len(web))
        for d in web[:80]:
            md.append("- [%s](%s)" % ((d.get("title") or "(无标题)")[:90], d["url"]))
    with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print("完成：账号 %d / 推文 %d / 链接 %d（%.0fs）"
          % (stats["accounts"], len(tweets), len(links), time.time() - t0), flush=True)
    print("输出：", os.listdir(OUT), flush=True)


if __name__ == "__main__":
    main()
