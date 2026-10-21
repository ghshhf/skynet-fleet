#!/usr/bin/env python
"""门禁 t16：netforce skynet 采集内核接进本仓 web.* 能力族。

**这个门禁钉的是六件真事**（每一条都是接线时实测踩出来的坑）：

  ① **字段名不许猜。** skynet 的 `probe()` 返回 `Probe` dataclass，
     真实字段是 `alive/status/final_url/size/title/content/score/verdict/reasons`，
     **不是** `ok/text_len/density/why`。猜错 ⇒ 上层永远读到空值。
     ⇒ 门禁用**假 skynet**（照抄真实签名）把包装层钉死。

  ② **`structure()` 的键类型也不许猜。** 实测踩到：
     `sitemap_files` 是 **int**（几个 sitemap 文件），不是列表；
     `sitemap_urls` 是 **int**（总条数）；`feed` 可能是 **None**。
     当成 list 处理 ⇒ `list(int)` 抛 `TypeError: 'int' object is not iterable`
     ⇒ 能力直接报错，界面上变成一条看不懂的异常。
     ⇒ 门禁钉死这三种类型。

  ③ **内核不在 ⇒ 能力不可用，且必须说清怎么装。**
     不许静默降级成「搜不了就返回空」——空回是最阴的失败。

  ④ **副作用能力没闸门时必须 fail-closed。**
     `web.download` 要写本地文件 ⇒ 没注入 gate 必须是 denied，不是「先写了再说」。

  ⑤ **`check` 判定的是「内核在不在」，不是「netforce 在不在」。**
     netforce 仓库装了不代表 `skynet/` 子包装上了（它可以是老版本）。

  ⑥ **适配层不许 vendoring。** 仓库里不许出现 skynet 的源码副本；
     只 import，不复制 ⇒ 对方升级不用人肉同步两份。

**全离线**：门禁自己造一个**假 skynet 包**（照抄真实模块签名）放到临时目录，
用 NETFORCE_HOME 指过去。不连真网络。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def skip(msg):
    print("  --", msg)
    SKIP.append(msg)


# --------------------------------------------------------------------------
# 假 skynet 包：照抄真实签名与返回类型（真实字段名来自 D:/code/netforce/skynet/）
# --------------------------------------------------------------------------

FAKE_SKYNET = {
    "search.py": '''
"""假 search：照抄真实 skynet/search.py 的关键签名与字段。"""
MIN_REL = 0.18
HEALTH = {"ddg|zh": {"in": 10, "out": 8, "calls": 3},
          "bing|zh": {"in": 10, "out": 0, "calls": 3}}
ENGINES = []


class Engine:
    def __init__(self, name, fn=None, weight=1.0, needs_tag=False, generic=True):
        self.name = name
        self.weight = weight


ENGINES = [Engine("ddg"), Engine("bing"), Engine("github")]


class Hit:
    def __init__(self, url, host, title="", engines=None, rel=0.0, score=0.0):
        self.url = url
        self.host = host
        self.title = title
        self.engines = list(engines or [])
        self.rel = rel
        self.score = score

    @property
    def cross(self):
        return len(self.engines)


def bootstrap_health():
    return None


def health_weight(name, lang=""):
    h = HEALTH.get(name) or HEALTH.get(name + "|" + lang)
    if not h:
        return 1.0
    return (h["out"] + 1) / (h["in"] + 4)


def search(query, wiki_tag=None, only=None, min_rel=MIN_REL,
           workers=5, budget=18.0):
    return [Hit("https://github.com/a/b", "github.com", "repo b",
                ["github"], 0.75, 3.5)]


def relevance(q, url, title):
    return 1.0 if "game" in (title or "").lower() else 0.0
''',
    "net.py": '''
"""假 net：照抄真实 FetchResult 字段。"""


class FetchResult:
    def __init__(self, url, ok=False, status=0, size=0, final_url="",
                 channel="", elapsed=0.0, text="", data=b"", error="",
                 cached=False, hops=None):
        self.url = url
        self.ok = ok
        self.status = status
        self.size = size
        self.final_url = final_url
        self.channel = channel
        self.elapsed = elapsed
        self.text = text
        self.data = data
        self.error = error
        self.cached = cached
        self.hops = list(hops or [])

    def __bool__(self):
        return self.ok


def fetch_text(url, timeout=15.0, gap=0.8, retry=2, force_channel=None,
               budget=25.0):
    return FetchResult(url, ok=True, status=200, size=1234,
                       final_url=url, channel="session", elapsed=0.1,
                       text="hello world " * 100)


def download(url, dest, timeout=60.0, gap=0.3, max_bytes=1 << 28, dedupe=True):
    from pathlib import Path
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * 2048, encoding="utf-8")
    return FetchResult(url, ok=True, status=200, size=2048, channel="download")
''',
    "probe.py": '''
"""假 probe：照抄真实 Probe / Content dataclass 字段。"""


class Content:
    def __init__(self):
        self.bytes = 0
        self.title = ""
        self.text_chars = 0
        self.text_density = 0.0
        self.tables = 0
        self.rows = 0
        self.candidates = 0
        self.with_pronoun = 0
        self.audio_links = 0
        self.junk_hit = ""
        self.headers = []


class Probe:
    def __init__(self, url):
        self.url = url
        self.alive = True
        self.status = 200
        self.final_url = url
        self.size = 4096
        self.title = "Fake Page"
        self.content = Content()
        self.content.text_chars = 3210
        self.content.text_density = 0.72
        self.content.candidates = 12
        self.content.audio_links = 3
        self.score = 88
        self.verdict = "valuable"
        self.reasons = ["ok"]

    def __bool__(self):
        return self.alive

    def brief(self):
        return "fake"


def probe(url, timeout=15.0, gap=0.6, keep_html=False):
    return Probe(url)
''',
    "discover.py": '''
"""假 discover：照抄真实 structure() 的键与**类型**。

🔴 这里 sitemap_files 是 int、sitemap_urls 是 int、feed 可能是 None ——
    跟真实实现一模一样（真实版就是 int，当时猜成 list 直接炸了）。
"""


def structure(url, max_urls=1500):
    return {
        "root": "https://fake.example",
        "robots": "✓ robots 100B",
        "crawl_delay": 1.5,
        "sitemap_files": 2,          # int：几个 sitemap 文件
        "sitemap_urls": 137,         # int：总条数
        "truncated": False,
        "ext": {"mp3": 12, "html": 90},
        "sample": ["https://fake.example/a", "https://fake.example/b"],
        "feed": None,                # 可能没有 feed
    }
''',
    "__init__.py": "",
}


def _make_fake_skynet(base):
    """在 base 下造一个假 skynet 包，返回它的父目录（当 NETFORCE_HOME）。"""
    sk = os.path.join(base, "skynet")
    os.makedirs(sk, exist_ok=True)
    for fn, body in FAKE_SKYNET.items():
        with open(os.path.join(sk, fn), "w", encoding="utf-8") as f:
            f.write(body)
    return base


def main():
    import fleet.catalog as C
    from fleet import netforce_skynet as NS

    tmp = tempfile.mkdtemp(prefix="fleet-t16-")
    home = _make_fake_skynet(os.path.join(tmp, "netforce"))

    old_home = os.environ.get("NETFORCE_HOME")
    old_path = list(sys.path)
    os.environ["NETFORCE_HOME"] = home
    # 清掉可能已 import 的真 skynet，保证门禁用的是假包
    for k in [k for k in sys.modules if k == "skynet" or k.startswith("skynet.")]:
        del sys.modules[k]

    try:
        # ---------- ① 内核在场 ⇒ 可用 ----------
        ok, why = NS.available()
        check(ok, "假 skynet 在位时适配层报告可用（why=%r）" % why)
        check(NS.netforce_home() == home,
              "netforce_home 找到的是 NETFORCE_HOME 指的地方")

        # ---------- ② probe 包装：字段名不许猜 ----------
        d = NS.probe("https://fake.example/x")
        check(d.get("alive") is True, "probe 包装读到了 alive（不是 ok）")
        check(d.get("verdict") == "valuable", "probe 包装读到了 verdict")
        check(d.get("score") == 88, "probe 包装读到了 score")
        check(d.get("text_chars") == 3210, "probe 包装读到了 text_chars")
        check(d.get("candidates") == 12, "probe 包装读到了 candidates")
        check(d.get("audio_links") == 3, "probe 包装读到了 audio_links")
        check(d.get("reasons") == ["ok"], "probe 包装读到了 reasons")
        # 旧猜法（ok/text_len/density/why）必须已经不存在
        check("text_len" not in d and "density" not in d and "why" not in d,
              "包装层不再暴露猜错的旧字段名（text_len/density/why）")

        # ---------- ③ structure 包装：键的类型也不许猜 ----------
        s = NS.structure("https://fake.example")
        check(isinstance(s.get("sitemap_files"), int),
              "sitemap_files 是 int（实测踩过：当成 list 会 TypeError）")
        check(isinstance(s.get("sitemap_urls"), int), "sitemap_urls 是 int")
        check(isinstance(s.get("sample"), list), "sample 是 list")
        check(isinstance(s.get("feed"), str),
              "feed 缺失时拍成空串（真实值可能是 None）")
        check(s.get("ext", {}).get("mp3") == 12, "ext 里的扩展名统计带过来了")

        # ---------- ④ search 包装：拍平成 dict ----------
        hits = NS.search("game voice lines dataset")
        check(isinstance(hits, list) and hits, "search 返回命中列表")
        h0 = hits[0]
        for k in ("url", "host", "title", "engines", "cross", "rel", "score"):
            check(k in h0, "search 命中带字段 %s" % k)
        check(h0["cross"] == len(h0["engines"]), "cross 等于引擎数")

        # ---------- ⑤ 健康度：键是「引擎|语言桶」 ----------
        eh = NS.engine_health()
        names = [e["engine"] for e in eh]
        check("ddg" in names and "bing" in names,
              "健康度里有 ddg 与 bing（实现在 %s）" % names)
        ddg = [e for e in eh if e["engine"] == "ddg"][0]
        check(ddg["submitted"] == 10 and ddg["alive"] == 8,
              "健康度把 in/out 映射成 submitted/alive")

        # ---------- ⑥ 能力都登记进注册表了 ----------
        reg = C.build()
        names = reg.cap_names()
        for cap in ("web.search", "web.fetch", "web.probe",
                    "web.structure", "web.download"):
            check(cap in names, "能力 %s 已登记" % cap)
        capd = reg.capability("web.download")
        check(capd.side_effect is True, "web.download 是副作用能力（写本地文件）")
        for cap in ("web.search", "web.fetch", "web.probe", "web.structure"):
            c = reg.capability(cap)
            check(c.side_effect is False, "%s 是只读能力" % cap)

        # ---------- ⑦ 只读能力真调能通 ----------
        r = reg.call("web.probe", args={"url": "https://fake.example/x"})
        check(r.get("ok") is True, "web.probe 能调通（err=%r）" % r.get("err"))
        check((r.get("data") or {}).get("verdict") == "valuable",
              "web.probe 的信封里带回了 verdict")
        r = reg.call("web.structure", args={"url": "https://fake.example"})
        check(r.get("ok") is True, "web.structure 能调通（err=%r）" % r.get("err"))

        # ---------- ⑧ 副作用能力没闸门 ⇒ fail-closed ----------
        dest = os.path.join(tmp, "out.bin")
        r = reg.call("web.download", args={"url": "https://fake.example/f",
                                          "dest": dest})
        check(r.get("ok") is False, "web.download 没闸门时被拒绝")
        check(r.get("code") == "denied", "拒绝码是 denied（不是普通失败）")
        check(not os.path.exists(dest), "被拒时没有偷偷写文件")

        # 有闸门 ⇒ 放行
        r = reg.call("web.download", args={"url": "https://fake.example/f",
                                          "dest": dest},
                     gate=lambda c, d, a, l: (True, ""))
        check(r.get("ok") is True, "web.download 有闸门时放行（err=%r）" % r.get("err"))
        check(os.path.exists(dest), "放行后文件真的落盘了")

        # ---------- ⑨ 缺 url ⇒ 说清缺什么，不许静默 ----------
        r = reg.call("web.fetch", args={})
        check(r.get("ok") is False, "web.fetch 缺 url 时失败")
        check("url" in (r.get("err") or ""), "失败原因里点名了缺 url")

        # ---------- ⑩ 内核不在 ⇒ 不可用且说清怎么装 ----------
        os.environ["NETFORCE_HOME"] = os.path.join(tmp, "no-such-netforce")
        for k in [k for k in sys.modules if k == "skynet" or k.startswith("skynet.")]:
            del sys.modules[k]
        import importlib
        importlib.reload(NS)
        ok2, why2 = NS.available()
        check(ok2 is False, "内核不在时报告不可用")
        check("NETFORCE_HOME" in why2, "不可用原因里说了怎么装（提到 NETFORCE_HOME）")
        r = reg.call("web.probe", args={"url": "https://fake.example/x"})
        check(r.get("ok") is False, "内核不在时 web.probe 不可用（不是空回）")
        check(bool((r.get("err") or "").strip()),
              "不可用时带原因（静默失败最阴）")

        # ---------- ⑪ 不许 vendoring ----------
        sk_src = os.path.join(ROOT, "skynet")
        check(not os.path.isdir(sk_src),
              "本仓里没有 skynet 源码副本（只 import，不 vendoring）")
        for fn in os.listdir(ROOT):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(ROOT, fn)
            try:
                body = open(p, encoding="utf-8").read()
            except Exception:
                continue
            if "class Hit" in body or "def health_weight" in body:
                check(False, "本仓 %s 里出现了 skynet 的源码副本（vendoring）" % fn)
                break
        else:
            check(True, "本仓没有 skynet 源码副本（只做适配层）")

    finally:
        if old_home is None:
            os.environ.pop("NETFORCE_HOME", None)
        else:
            os.environ["NETFORCE_HOME"] = old_home
        sys.path[:] = old_path
        for k in [k for k in sys.modules if k == "skynet" or k.startswith("skynet.")]:
            del sys.modules[k]
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if SKIP:
        print("跳过 %d 项：" % len(SKIP))
        for s in SKIP:
            print("  --", s)
    if FAIL:
        print("=== t16 失败 %d 项 ===" % len(FAIL))
        for f in FAIL:
            print("  -", f)
        return 1
    print("=== t16 全通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
