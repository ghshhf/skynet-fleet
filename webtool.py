#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webtool.py —— 给大模型接上网。

链路：问题 → 判要不要联网 → 多引擎搜 → 并发抓正文 → 拼成带编号的资料块
      → 塞进 system → 模型只负责「读资料 + 回答」。

━━ 为什么不走 function calling ━━
手机上的模型是 0.6B~4B。实测 0.6B 会编出不存在的工具名、把参数写成
「查询词：xxx」这种自然语言、在 JSON 外面再包一层解释。工具协议它守不住，
给它工具描述反而更容易跑偏。所以：**「决定搜什么」留在服务端用规则做，
「用资料回答」交给模型** —— 小模型只做它擅长的部分。

━━ 为什么资料块必须卡字数 ━━
手机 llama-server 默认 `-c 4096`。人格包 + 历史折进 prompt 之后，留给资料的
正经空间只有一千多字。塞多了不是「知道更多」，是把历史挤出去 ——
表现是多轮对话突然忘了前面说过什么，而且人很难把这两件事联系起来。

━━ 为什么不自己写 HTTP 抓 ━━
`fleet/netforce_skynet.py` 已经有多引擎聚合 + 四级判活 + 真 Range 续传，
它是这仓库里唯一被反复调过的东西。这里只做编排，不重复造。
"""

from __future__ import annotations

import concurrent.futures as _fut
import html as _html
import re
import threading
import time

#: 搜到几条就够。给小模型的资料不是越多越好，是越准越好。
DEFAULT_K = 4

#: 单条正文最多取多少字。4 条 × 500 字 ≈ 2000 字，小模型能吃得下。
DEFAULT_CHARS = 500

#: 一次联网的总预算（秒）。超了就用已经拿到的，不许把人晾在那儿。
DEFAULT_BUDGET = 22.0

#: 同一个问题在这么多秒内重复问，直接用上次结果。
#: 多轮对话里「它参考谁」这件事不该每轮重搜一遍 —— 那会让每轮都慢十几秒，
#: 而且两次搜到的结果不一样，模型前后说法就打架。
CACHE_TTL = 300.0

#: 单条结果标题超过这个长度就丢。中文正常标题 20~40 字，超过 100 字的
#: 基本都是把关键词堆起来抢排名的垃圾页（实测：搜「最新安卓版本」，
#: 前四条标题全是 100+ 字的口号堆砌，跟安卓一点关系没有）。
MAX_TITLE_CHARS = 100

#: 同一个站点最多收几条。挡站群：spam 常用几十个同模板站互相顶排名，
#: 不限制的话四条结果能全来自同一个域名。
MAX_PER_HOST = 2

#: 命中这些词的结果整条丢弃（不做「批判性地看」，而是根本不进上下文）。
#: 实测：搜一个纯技术问题时，排名最前的几条是政治口号仓库 —— 它们的
#: description 里堆了几百个热词来劫持几乎所有查询。这类东西一旦进了
#: 小模型的上下文，模型会当成「资料」去模仿，回答直接跑偏。
_BLOCK_WORDS = (
    "反中共", "反华", "台独", "藏独", "疆独", "法轮功", "六四事件",
    "政治宣传库", "propaganda.", "天安门事件", "颠覆国家",
)

#: 已知的垃圾源。留空也不影响 —— 上面的长度/相关度/站群规则已经能挡住绝大多数。
_BLOCK_HOSTS = ()

# ── 违法广告的形状规则（2026-10-24 真机样本固化，见 docs/违法广告向量研究报告）━━
# 为什么要单列一套：上面四条判据挡的是「形状垃圾」（口号库、堆问号、无字面
# 重叠），而**违法广告是「形状正常 + 内容违法」**——它们的标题里真有查询词
# （SEO 劫持的目的就是这个），字面重叠规则对它们完全失效。实测 112 条样本里
# 12 条违法/垃圾全部穿过现有过滤，且全部长在同一形状上。

#: 品类词。正常页也会提（电影/下载），所以**必须和入口词组合**才判导流——
#: 单独命中不拦，那是误伤：「电影美剧搜索及在线观看离线下载软件」是真开源
#: 项目（EvilCult/moviecatcher），通篇讲功能、没有一个入口词。
_AD_KINDS = ("直播", "影视", "电影", "网盘", "资源站", "破解", "在线观看",
             "纸飞机", "色情", "菠菜", "娱乐城", "赌场", "六合彩", "时时彩",
             "首存")

#: 入口词。正常标题几乎不堆这些——堆了就不是在介绍东西，是在给入口。
_AD_ENTRY = ("官网", "官方入口", "入口网站", "网址", "最新网站", "欢迎光临",
             "点击进入")

#: 真交易所品牌。单独出现不拦（新闻天天提 OKX），和「官方 App 下载」话术
#: 一起出现 = 假 App 导流，是资金诈骗的前置入口。
#: 实测样本：「如何安全下载OKX官方APK文件？OKX安卓版本APK安装失败怎么办」
#:            「欧易OKX怎么下载安装？OKX官方APP在哪里下载？」
_AD_EXCHANGE = ("okx", "欧易", "币安", "binance", "火币", "htx", "bybit")
_AD_APP_BAIT = ("apk", "下载安装", "怎么下载", "在哪里下载", "官方app",
                "app下载")

#: GitHub 寄生仓库名里的拼音品类词（电影/直播/网站/成人……）。
_AD_REPO_PINYIN = ("dianying", "zhibo", "shipin", "wangzhan", "porn", "sex")

#: 标题内嵌裸域名：`(lyiwush.com)`、`-xxx.com`。但**标题提自己的域名是
#: 正常的**（「Welcome to Python.org」就在 python.org 上）——判据必须是
#: 「标题里的域名 ≠ 命中页自己的域名」：想把你导去别处，才是导流页。
#: 再加一层：**知名站点被正常页引用也常事**（中文镜像站标题写 Python.org）
#: ——只有引用「来路不明的裸域名」才拦。GitHub 寄生那条由规则 5 单独管。
_AD_TRUSTED_DOMAINS = ("github.com", "python.org", "gnu.org", "wikipedia.org",
                       "stackoverflow.com", "pypi.org", "android.com",
                       "sourceforge.net", "apache.org")
_AD_DOMAIN_IN_TITLE = re.compile(
    r"[（(\-—–\s]\s*(?:https?://)?(?:www\.)?((?:[a-z0-9][a-z0-9-]*\.)+"
    r"(?:com|cn|net|org|top|xyz|icu|vip|cc|hk|io|me|info|biz))[)）]?", re.I)


def _domain_mismatch(title, host):
    """标题里有没有「不是本页的、且不是知名站点」的域名。有则返回它。"""
    host = (host or "").lower()
    for m in _AD_DOMAIN_IN_TITLE.finditer(title or ""):
        d = m.group(1).lower()
        if d == host or d.endswith("." + host) or host.endswith("." + d):
            continue
        if any(d == t or d.endswith("." + t) for t in _AD_TRUSTED_DOMAINS):
            continue
        return d
    return ""

#: GitHub 一次性小号仓库的随机后缀：`-91ok3gob`、`digg44`（数字字母混合）。
_AD_REPO_RANDSUF = re.compile(r"-(?:[0-9a-z]{5,8})$|^(?=.*\d)(?=.*[a-z])[a-z0-9]{5,8}$")

_cache = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------- 要不要联网

#: 命中这些词说明答案会随时间变，训练数据里的旧值一定过期。
_TIME_WORDS = (
    "今天", "昨天", "明天", "本周", "这周", "上周", "本月", "这个月", "今年",
    "最新", "最近", "现在", "目前", "当前", "刚刚", "近期", "实时",
    "新闻", "股价", "币价", "汇率", "价格", "多少钱", "涨", "跌", "市值",
    "天气", "比分", "赛程", "发布会", "上市", "开源", "更新", "版本",
    "谁是", "去世", "离职", "上任", "当选", "宣布", "融资", "收购",
    "latest", "today", "news", "current", "now", "price", "release",
    "who is", "when did", "how much", "2025", "2026",
)

#: 命中这些说明用户在**明确要求**联网，不用再猜。
_EXPLICIT = (
    "搜一下", "搜搜", "搜下", "查一下", "查查", "查下", "帮我查", "帮我搜",
    "上网", "联网", "网上", "百度", "谷歌", "google", "search", "look up",
    "根据资料", "引用来源", "有没有资料",
)

#: 命中这些说明是纯本地活，联网只会添乱（还会白等十几秒）。
_NO_WEB = (
    "翻译", "改写成", "润色", "帮我写", "写一段", "写个", "总结这段",
    "这段代码", "报错", "函数", "正则", "排序算法", "计算公式",
    "translate", "rewrite", "summarize this",
)


#: 用户消息里可能夹着「给模型的指令」。手机端会在问题末尾追加
#: 「（直接给答案，不要输出思考过程。）」这类引导，直接拿去搜会把关键词
#: 污染成「直接给答案 不要输出思考过程 安卓版本」—— 搜出来全是噪音。
#: 搜之前必须剥掉。
_INSTRUCTION = re.compile(
    r"[（(](?:先在心里|直接给答案|不要输出|请用中文|用一句话|简短|详细解释)"
    r"[^）)]{0,80}[）)]")


def _clean_query(text):
    """剥掉夹带在问题里的指令，只留真正要搜的东西。

    两层清理：
      1. 多轮转写（`_fold_history` 折出来的「我: … 它: …」）只取最后一条「我」——
         拿整段转写去搜，关键词会变成一堆对话标记。调用方一般已经只传本轮问题，
         这里是兜底（它迟早会有人踩）。
      2. 手机端会追加「（直接给答案，不要输出思考过程。）」这类引导，也要剥掉。
    """
    raw = (text or "").strip()
    t = raw
    if "我:" in t or "我：" in t:
        cut = max(t.rfind("我:"), t.rfind("我："))
        t = t[cut + 2:]
    t = _INSTRUCTION.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or raw


def needs_web(text, mode="auto"):
    """要不要联网。返回 (bool, 原因)。

    mode：
      · "off"   —— 绝不联网
      · "on"    —— 一定联网（用户点了「联网」开关）
      · "auto"  —— 按问题本身判断（默认）

    判定的取舍：**宁可漏，不要错搜**。错搜的代价是每次多等 10~20 秒、
    还会把不相关的网页塞进上下文干扰小模型；漏搜的代价只是答得旧一点，
    下一句用户自己会说「查一下」。
    """
    t = (text or "").strip()
    if not t:
        return False, "空问题"
    low = t.lower()
    m = (mode or "auto").strip().lower()

    if m == "off":
        return False, "用户关了联网"
    if m == "on":
        return True, "用户开了联网"

    for kw in _EXPLICIT:
        if kw in low:
            return True, f"用户明确要求联网（「{kw}」）"
    for kw in _NO_WEB:
        if kw in low:
            return False, f"纯本地任务（「{kw}」），联网无益"
    for kw in _TIME_WORDS:
        if kw in low:
            return True, f"答案会随时间变（「{kw}」），本地知识一定过期"
    # 明显是闲聊/自我描述，不用查
    if len(t) <= 12 and not re.search(r"[?？]", t):
        return False, "短句且非提问"
    return False, "auto：没看到需要时效信息的迹象"


# --------------------------------------------------------------- 搜索 + 抓正文

#: 虚词/疑问用字。bigram 会把「什么」「怎么」「哪些」当关键词，于是**任何**
#: 含「什么」的垃圾页都能通过相关度检查 —— 自测当场抓到这条（「随便什么标题」
#: 因为含「什么」而放行）。含虚词的二字组直接丢掉。
#:
#: 刻意不放「以/为/与/等」：它们会误伤实义词（「以太坊」的「以太」、
#: 「行为分析」的「行为」）。宁可漏挡几个虚词，不能砍掉真关键词。
_STOP_CHARS = set("的了是在有和就不也都一很到说要去你会着没看好这那什么怎何哪些多少吗呢吧啊")


def _keywords(q):
    """从问题里提关键词，用来在正文中挑相关段落、并做相关度校验。

    中文没有空格，分词又要拉 jieba。用**相邻两字组成的 bigram** 代替：
    「小米无障碍被关」→ 小米/米无/无障碍/碍被/被关。不需要词典，
    对中文的召回几乎和分词一样好，代价是偶尔多匹配几个无关词 ——
    含虚词的组会被 _STOP_CHARS 剔掉。
    """
    q = re.sub(r"[^\w\u4e00-\u9fff]+", " ", q or "")
    ks = set()
    for w in q.split():
        if re.fullmatch(r"[\u4e00-\u9fff]+", w):
            if len(w) >= 2:
                ks.update(g for g in (w[i:i + 2] for i in range(len(w) - 1))
                          if not (set(g) & _STOP_CHARS))
        elif len(w) >= 2:
            ks.add(w.lower())
    return ks


def _clean_html(s):
    """把抓回来的网页残片洗成能看的正文。

    为什么必须洗：`fetch_text` 对某些站（CSDN/掘金这类 SSR 页面）返回的
    片段里带完整 `<head>`，于是「正文」能是十几行 `<meta property="og:...">`。
    这些东西含有关键词、会被 _pick_relevant 优先挑中，模型就拿着标签去答题了 ——
    实测第一次跑就是这样，四条资料里三条全是 meta。

    两个刻意的限制：
      · 标签正则限长 `[^>]{0,400}`，避免正文里出现的 `<` 把一大段正常文字吃掉。
      · 清洗后单独剔掉「只剩 meta 字段」的行（og:/content=/charset=…），
        它们洗掉标签后是半截键值串，对模型只有干扰。
    """
    s = s or ""
    s = re.sub(r"(?is)<(script|style|noscript|svg|symbol|defs)\b[^>]*>.*?</\1>", " ", s)
    # SVG 图形元素常常没有闭合标签（<path ... /> 或干脆裸着），上面那条按
    # 「配对标签」删的规则吃不到。实测 android.com：洗完之后正文里冒出
    # `<path fill="currentColor" d="m6.593 9.016-1.88...` —— path data 动辄
    # 几百字符、全是字母数字，会被当成「有信息量的正文」喂给模型。
    s = re.sub(r"(?is)<(path|use|circle|rect|line|polygon|polyline|ellipse|image)\b[^>]*>",
               " ", s)
    s = re.sub(r'(?s)<!--.*?-->', " ", s)
    # 上限从 400 提到 1500：内联 SVG/log 标签的属性经常超 400，
    # 匹配不上就整段留下来（正文里 `<` 后面紧跟 1500 字符还没有 `>` 的情况极少）。
    s = re.sub(r"(?s)<[^>]{0,1500}>", " ", s)
    # 兜底：抓取有字符预算（80000），页面在这里被**截断**时，最后一个标签
    # 往往根本没有闭合的 `>`——实测 android.com 末尾停在 path data 中间
    # `...34.1381 15.1046 32.`，于是上面所有「按标签删」的规则全部失效，
    # 几千字符的路径数据被当成正文留下来。
    # 只锚在**末尾**（截断只可能发生在末尾），不加 `$` 会把正文里带 `<` 的
    # 段落一路吃掉——实测加宽后 android.com 的正文直接归零。
    s = re.sub(r"(?s)<[a-zA-Z/!][^<>]{150,}$", " ", s)
    s = _html.unescape(s)
    # 多行标签被截断时，标签壳没了、属性却留下（实测 CSDN 页面末尾残留
    # `class="layout-menu-item_inner" href="https://www.csdn.net"`）。
    # 只清**有名的 HTML 属性**，不动正文里可能出现的英文引号。
    s = re.sub(r'\b(href|src|srcset|class|id|style|alt|title|rel|target|role|'
               r'data-[\w-]+|aria-[\w-]+|d|fill|stroke|viewBox|points|transform|'
               r'xmlns|stroke-width|stroke-linecap|stroke-linejoin|fill-rule|'
               r'clip-rule)="[^"]{0,1500}"', " ", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    lines = []
    for ln in s.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if re.search(r"(og:|twitter:|property=|content=|charset=|device-width|"
                     r"apple-itunes|msvalidate|theme-color)", ln):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def _extract_paras(raw):
    """从原始 HTML 里提正文段落。

    为什么要有这一步：`skynet` 返回的是**整页 HTML**（几十 KB），直接洗标签
    会得到「导航 + 侧栏 + 版权 + 正文」混在一起的一大坨；而博客类站的正文
    几乎全在 `<p>/<li>/<h*>` 里。只取这些块级元素，噪音一下就掉一个量级。

    兜底：块级元素加起来不足 200 字时（可能是纯文本接口、或页面结构特殊），
    退回整页洗标签 —— 宁可多带噪音，不能把正文丢了。
    """
    raw = raw or ""
    if "<" not in raw:
        return raw                       # 已经是纯文本
    out = []
    for _tag, inner in re.findall(
            r"(?is)<(p|li|h1|h2|h3|h4|blockquote|pre|td)\b[^>]*>(.*?)</\1>", raw):
        t = _clean_html(inner)
        if len(t) >= 20:
            out.append(t)
    if sum(len(x) for x in out) >= 200:
        return "\n".join(out)
    return _clean_html(raw)


def _pick_relevant(text, kws, budget):
    """从一篇正文里挑「跟问题有关」的段落，拼到预算为止。

    为什么要挑而不是直接截前 N 字：网页开头通常是导航条、「登录」「首页」
    这类噪音，正文往往在中段。直接截头会把最有用的部分丢掉，还让小模型
    以为这页是份菜单。
    """
    paras = [p.strip() for p in re.split(r"\n+", _extract_paras(text)) if len(p.strip()) >= 24]
    if not paras:
        return (text or "").strip()[:budget]
    hot, cold = [], []
    for p in paras:
        low = p.lower()
        (hot if any(k in low or k in p for k in kws) else cold).append(p)
    out, n = [], 0
    for p in hot + cold:
        if n >= budget:
            break
        out.append(p)
        n += len(p)
    return "\n".join(out)[:budget]


def _junk_reason(hit, kws):
    """这条结果该不该丢。返回丢弃理由，空串 = 保留。

    四条判据，按「抓错一次代价多大」排序：

    1. 命中黑名单词 —— 最硬的拦截。这类内容不是「观点不同」，是纯粹的
       垃圾/有害内容，进了上下文会把回答整个带偏。
    2. 标题里一个查询关键词都没有 —— **这是最有效的一条**。搜索引擎被
       SEO spam 劫持时，症状就是命中的页面标题跟你问的东西毫无字面重叠
       （它们靠堆别人的热词抢排名）。正常网页多少会重复问题里的词。
    3. 标题超长 —— 关键词堆砌的典型形状。
    4. 标题+描述里黑名单词出现 3 次以上 —— 短标题也可能藏着口号库。
    """
    title = (hit.get("title") or "").strip()
    blob = title + " " + (hit.get("snippet") or hit.get("desc") or "")

    for w in _BLOCK_WORDS:
        if w in blob:
            return f"命中屏蔽词「{w}」"
    if sum(blob.count(w) for w in _BLOCK_WORDS) >= 3:
        return "疑似口号堆砌"

    host = (hit.get("host") or "").lower()
    if host and any(host.endswith(h) for h in _BLOCK_HOSTS):
        return f"屏蔽源 {host}"

    if len(title) > MAX_TITLE_CHARS:
        return f"标题过长（{len(title)}字），疑似关键词堆砌"

    # 标题里堆问号是 SEO spam 最稳的特征：实测搜「今天比特币什么价」，
    # 排第一的是个 GitHub 仓库，标题 96 字、带 6 个问号，把「币价/交易所/
    # 资金费率」全塞进去抢排名 —— 靠长度阈值差一个字没拦住，问号数一看就露。
    # 正常标题极少超过 2 个问号（并列两问已是极限）。
    if title.count("?") + title.count("？") >= 3:
        return "标题堆满问号，疑似关键词堆砌"

    # ── 违法广告的形状判据（样本见 docs/违法广告向量研究报告_2026-10-24.md）

    # 1) 成人内容：字面出现即不进上下文。这类东西进了上下文，小模型会
    #    当成「资料」去模仿，回答直接跑偏——和口号库一个道理，但没有
    #    「批判地看」这个选项，根本不进。**标题和摘要都查**：摘要是话术
    #    栖息地，而正常页的摘要几乎不可能出现这个词（误伤面约等于零）。
    if "色情" in blob or "情色" in blob:
        return "成人内容"

    # 2) 真交易所品牌 + 官方 App 下载话术 = 假 App 导流（诈骗前置）。
    #    只拦「品牌×下载」组合：新闻里提 OKX 是正常事，不误伤。
    low_title = title.lower()
    if (any(x in low_title for x in _AD_EXCHANGE)
            and any(x in low_title for x in _AD_APP_BAIT)):
        return "交易所品牌+官方下载话术，疑似假App导流"

    # 3) 标题内嵌**外部**域名：正常标题提自己的站名，不会把你导向别处。
    #    实测样本：「米兰app官方入口网站-(lyiwush.com)提供最新官方版本下载」
    d = _domain_mismatch(title, host)
    if d:
        return f"标题内嵌外部域名 {d}，典型导流页"

    # 4) 品类词 × 入口词：形状从「介绍软件」变成「给入口」。这条放过
    #    正常技术页——moviecatcher 提「电影/资源站」但通篇讲功能。
    if (any(k in title for k in _AD_KINDS)
            and any(e in title for e in _AD_ENTRY)):
        return "品类词+入口词组合，导流页形状"

    # 5) GitHub 寄生仓库：一次性小号建仓，仓库名即导流词（拼音串/品类词/
    #    随机后缀），描述堆品类词。实测一个账号连开 5 个 `-91ok3gob` 后缀仓。
    if host == "github.com":
        head = title.split(" ", 1)[0]
        if "/" in head:
            repo = head.split("/", 1)[1].lower()
            rest = title[len(head):]
            if (any(k in rest for k in _AD_KINDS)
                    and (any(p in repo for p in _AD_REPO_PINYIN)
                         or _AD_REPO_RANDSUF.search(repo))):
                return "GitHub寄生仓库（仓库名即导流词）"

    # 6) 同一品类词在标题堆 3 次以上：关键词堆砌的另一种形状。
    #    实测：「足球直播_足球免费高清直播_足球直播在线观看-24直播网」直播×4。
    for k in _AD_KINDS:
        if title.count(k) >= 3:
            return f"品类词「{k}」堆砌 {title.count(k)} 次"

    # 相关度兜底：标题里必须至少出现一个来自问题的词/二字组。
    # 只在「问题确实能被字面表达」时启用 —— 问「那它呢」这种指代句没有
    # 可匹配的词，硬套会把所有结果都杀光。
    if kws and len(kws) >= 2:
        low = title.lower()
        if not any(kk in low or kk in title for kk in kws):
            return "标题与问题无字面重叠，疑似 SEO 劫持"
    return ""


def _search_filter(query, hits, want):
    """搜索 + 过滤 + 去站群。返回 (保留的命中, 丢弃记录)。"""
    kws = _keywords(query)
    kept, dropped, per_host = [], [], {}
    for h in hits:
        why = _junk_reason(h, kws)
        if why:
            dropped.append({"url": h.get("url", ""), "why": why})
            continue
        host = (h.get("host") or "").lower()
        if per_host.get(host, 0) >= MAX_PER_HOST:
            dropped.append({"url": h.get("url", ""), "why": f"{host} 超过 {MAX_PER_HOST} 条"})
            continue
        per_host[host] = per_host.get(host, 0) + 1
        kept.append(h)
        if len(kept) >= want:
            break
    return kept, dropped


def research(query, k=DEFAULT_K, chars=DEFAULT_CHARS, budget=DEFAULT_BUDGET,
             fetch_body=True):
    """搜 + 抓正文。返回 dict（不抛异常，失败也带原因）。

    `fetch_body=False` 只回标题和链接（快，适合只想给「去哪儿看」的场景）。
    """
    t0 = time.time()
    out = {"used": False, "query": query, "sources": [], "notes": [],
           "elapsed": 0.0, "block": ""}

    try:
        import fleet.netforce_skynet as NS
    except Exception as e:
        out["notes"].append(f"联网组件加载失败：{type(e).__name__}: {e}")
        return out
    ok, why = NS.available()
    if not ok:
        # 这里必须说清「为什么没网」，不能静默当成「不需要联网」——
        # 后者会让人以为模型不肯查，实际是搜索内核没装。
        out["notes"].append(f"搜索内核不可用：{why}")
        return out

    try:
        # 多搜一些好让过滤后有剩。但别太多 —— 搜索结果条数和耗时是正相关的。
        raw = NS.search(query, limit=max(k * 3, 10))
    except Exception as e:
        out["notes"].append(f"搜索失败：{type(e).__name__}: {e}")
        out["elapsed"] = round(time.time() - t0, 2)
        return out

    if not raw:
        out["notes"].append("没搜到结果（换关键词或直接问）")
        out["elapsed"] = round(time.time() - t0, 2)
        return out

    hits, dropped = _search_filter(query, raw, k)
    if dropped:
        # 必须说出来。静默丢弃会让人以为「网上真没资料」，
        # 实际是搜到了但全是垃圾 —— 这两种情况的应对完全不同。
        out["notes"].append(
            f"从 {len(raw)} 条原始结果里过滤掉 {len(dropped)} 条"
            f"（如：{dropped[0]['why']}）")
    out["dropped"] = dropped[:5]
    if not hits:
        out["notes"].append("搜索结果全是垃圾页或与问题无关，一条都没敢用")
        out["elapsed"] = round(time.time() - t0, 2)
        return out
    kws = _keywords(query)
    bodies = {}

    if fetch_body:
        # 并发抓。单条失败只记一笔，不拖垮整次检索。
        def grab(h):
            try:
                # max_chars 必须调大：默认 4000 只够 head，正文压根不在里面
                d = NS.fetch_text(h["url"], timeout=8.0, budget=12.0, max_chars=80000)
                if d.get("ok") and (d.get("text") or "").strip():
                    body = _pick_relevant(d["text"], kws, chars)
                    # 正文也要过一遍：标题干净、正文烂的情况真有（页头正常，
                    # 正文是转载的口号库）。命中就整条丢掉，不「截一段凑数」。
                    if sum(body.count(w) for w in _BLOCK_WORDS) >= 2:
                        return h["url"], "", "正文命中屏蔽内容"
                    return h["url"], body, ""
                return h["url"], "", (d.get("error") or f"HTTP {d.get('status')}")
            except Exception as e:
                return h["url"], "", f"{type(e).__name__}: {e}"

        left = max(2.0, budget - (time.time() - t0))
        with _fut.ThreadPoolExecutor(max_workers=min(4, len(hits))) as ex:
            futs = [ex.submit(grab, h) for h in hits]
            try:
                for f in _fut.as_completed(futs, timeout=left):
                    u, body, err = f.result()
                    bodies[u] = body
                    if err:
                        out["notes"].append(f"抓取失败（已跳过）：{u[:60]} — {err}")
            except _fut.TimeoutError:
                out["notes"].append("抓正文超时，只用已拿到的")

    for i, h in enumerate(hits, 1):
        body = bodies.get(h["url"], "")
        out["sources"].append({
            "i": i,
            "title": (h.get("title") or "").strip() or h["url"],
            "url": h["url"],
            "host": h.get("host") or "",
            "snippet": (h.get("snippet") or h.get("desc") or "").strip()[:200],
            "chars": len(body),
            "body": body,
        })

    out["used"] = bool(out["sources"])
    out["elapsed"] = round(time.time() - t0, 2)
    with _cache_lock:
        _cache[query.strip().lower()] = (time.time(), out)
    return out


def cached_research(query, **kw):
    """带 TTL 缓存的 research。多轮对话里同一问题不重复搜。"""
    key = (query or "").strip().lower()
    if not key:
        return research(query, **kw)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        d = dict(hit[1])
        d["cached"] = True
        return d
    return research(query, **kw)


def open_page(url, max_chars=1800, timeout=10.0, kws=None):
    """打开指定网页，返回**已清洗**的正文。给 agent loop（模型自主点链接）用。

    与 research 的分工：research 是「搜一批、挑相关段落拼资料块」；
    open_page 是「模型点名了这一页，就看这一页」。

    清洗必须复用 _extract_paras，不能另写一份 —— 实测过另写一份的下场：
    agent loop 直接把 `<meta charset=...><style>...` 当正文喂给模型，
    1500 字预算全被标签吃掉，模型什么也没读到，还把上下文撑大、
    让下一轮 prefill 从 4 秒涨到 73 秒。

    返回 dict，不抛异常：{"ok", "url", "text", "chars", "error"}
    """
    out = {"ok": False, "url": url, "text": "", "chars": 0, "error": ""}
    u = (url or "").strip()
    if not re.match(r"^https?://", u):
        out["error"] = "不是 http(s) 链接"
        return out
    try:
        import fleet.netforce_skynet as NS
    except Exception as e:
        out["error"] = f"联网组件加载失败：{type(e).__name__}: {e}"
        return out
    try:
        d = NS.fetch_text(u, timeout=timeout, budget=timeout + 6, max_chars=80000)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    if not d.get("ok"):
        out["error"] = d.get("error") or f"HTTP {d.get('status')}"
        return out
    raw = d.get("text") or ""
    if not raw.strip():
        out["error"] = "页面没有内容"
        return out
    body = _extract_paras(raw)
    if len(body) < 80:
        # 必须和「抓取失败」分开说：这不是网络问题，是页面用脚本填内容。
        # 而且要说清「换同类站也没用」——实测 4B 拿着「脚本渲染」这句话，
        # 又连着打开 binance / coinbase，结果一模一样，白烧 60 秒。
        # 工具的输出就是模型唯一的判断依据，含糊一句它就会一路试下去。
        out["error"] = ("页面是脚本渲染，HTML 里没有可读正文（不是抓取失败）。"
                        "行情、交易所、社交平台这类站点普遍如此，"
                        "换同类的其他站也一样拿不到，别重复试——"
                        "要么换个信息型站点（新闻/博客/文档），要么直接说拿不到。")
        return out
    if kws:
        body = _pick_relevant(body, kws, max_chars)
    out.update(ok=True, text=body[:max_chars], chars=len(body))
    return out


# --------------------------------------------------------------- 拼资料块

def render_block(res, max_chars=2400):
    """把检索结果拼成给模型看的一段资料。

    格式刻意朴素：编号 + 标题 + 正文。不要 Markdown 表格/多层缩进——
    小模型读到结构化嵌套会开始「模仿格式」而不是「使用内容」。
    """
    srcs = [s for s in (res.get("sources") or []) if s.get("body") or s.get("snippet")]
    if not srcs:
        return ""
    parts = [
        "【联网资料】下面是刚搜到的网页内容，用来回答用户的问题。",
        "规则：",
        "1. 只用**正文**里明确写出的内容作答。网页标题常掺营销词和旧版本号，"
        "标题不能当事实依据。",
        # 这条的措辞很重要：写成「资料没提到就说没提到」时，实测模型会连说
        # 四句「资料里没提到/仅包含页面结构/未给出具体数值」——它在复述规则，
        # 不是在回答。要求「一句话讲清 + 给出路」才像人话。
        "2. 资料里确实没有的，用一句话说清（例如「搜到的几页都没给出今天的"
        "价格」），然后告诉用户哪个来源最值得点进去看。**不要逐条罗列"
        "「资料里没有什么」**。",
        "3. 每个事实后面紧跟 [编号] 标出处。例：「Android 17 是 2026 年的版本 [2]」。",
        # 提示抓取的局限：交易所/行情页普遍是脚本渲染，HTML 里根本没有数字。
        # 不提前说，模型会以为自己漏读了，反复翻同一批资料。
        "4. 抓到的可能只是页面框架（行情页靠脚本填数字，HTML 里就没有）。"
        "遇到这种情况按第 2 条处理，不要编数字。",
        "",
    ]
    for s in srcs:
        body = (s.get("body") or s.get("snippet") or "").strip()
        if not body:
            continue
        parts.append(f"[{s['i']}] {s['title']}")
        parts.append(body)
        parts.append("")
    txt = "\n".join(parts).strip()
    return txt[:max_chars] if max_chars else txt


def augment(system, question, mode="auto", k=DEFAULT_K, chars=DEFAULT_CHARS,
            max_chars=2400):
    """给 system 挂上网。返回 (新 system, 联网报告 dict)。

    联网报告要一路带到前端：人得知道「这句回答是查过网还是凭记忆说的」。
    不标出来的话，模型把旧知识当新闻讲，人也没法判断可信度。
    """
    need, why = needs_web(_clean_query(question), mode)
    rep = {"used": False, "reason": why, "mode": mode, "query": question,
           "sources": [], "notes": [], "elapsed": 0.0}
    if not need:
        return system, rep

    q = _clean_query(question)
    res = cached_research(q, k=k, chars=chars)
    block = render_block(res, max_chars=max_chars)
    rep.update({
        "used": bool(block),
        "query": q,
        "elapsed": res.get("elapsed", 0.0),
        "cached": res.get("cached", False),
        "notes": res.get("notes", []),
        "sources": [{kk: s[kk] for kk in ("i", "title", "url", "host", "chars")}
                    for s in res.get("sources", [])],
    })
    if not block:
        rep["reason"] = f"{why}；但没取到可用资料（{'; '.join(res.get('notes') or ['无结果'])}）"
        return system, rep

    new_sys = (system or "").rstrip() + "\n\n" + block
    rep["reason"] = why
    return new_sys, rep


# --------------------------------------------------------------- 自测

def _selftest():
    """纯离线断言：不联网、不碰设备。"""
    bad = []
    n_ok = [0]        # 动态计数：写死数字迟早和实际断言数对不上

    def eq(a, b, name):
        if a != b:
            bad.append(f"{name}: 期望 {b!r}，实际 {a!r}")
        else:
            n_ok[0] += 1

    # --- needs_web：三种模式
    eq(needs_web("今天比特币多少钱", "auto")[0], True, "auto 抓时效词")
    eq(needs_web("今天比特币多少钱", "off")[0], False, "off 一票否决")
    eq(needs_web("随便聊聊", "on")[0], True, "on 强开")
    eq(needs_web("帮我翻译这段话", "auto")[0], False, "翻译不联网")
    eq(needs_web("查一下 ENA 最近的解锁", "auto")[0], True, "明确要求联网")
    eq(needs_web("", "auto")[0], False, "空问题不联网")
    # 短句不是提问 → 不搜（防止「好的」「继续」触发搜索）
    eq(needs_web("好的", "auto")[0], False, "短句不搜")
    eq(needs_web("Hi", "auto")[0], False, "英文短句不搜")
    # 长句但不是时效问题 → 也不搜
    eq(needs_web("请帮我解释一下什么是以太坊的账户抽象机制", "auto")[0], False,
       "概念题不搜")

    # --- needs_web：理由要能说清（不能只回 True/False）
    need, why = needs_web("最新 iPhone 什么价", "auto")
    eq(bool(why), True, "联网理由非空")

    # --- _keywords：中文 bigram
    ks = _keywords("小米无障碍")
    eq("小米" in ks and "障碍" in ks, True, "中文 bigram")
    eq("小米无障碍" in ks, False, "不该把整串当一个词")

    # --- _clean_query：剥掉夹带的指令
    #     手机端会在问题末尾追加「（直接给答案，不要输出思考过程。）」，
    #     不剥掉的话搜出来的关键词里全是「直接给答案 不要输出思考过程」。
    eq(_clean_query("最新的安卓版本号是什么\n\n（直接给答案，不要输出思考过程。）"),
       "最新的安卓版本号是什么", "剥掉尾部指令")
    eq(_clean_query("（直接给答案，不要输出思考过程。）"),
       "（直接给答案，不要输出思考过程。）", "全是指令时不清空")
    eq(_clean_query("帮我查一下 ENA 最近解锁（简短）"), "帮我查一下 ENA 最近解锁",
       "句中指令也剥")
    # 多轮转写兜底：不取最后一条「我」，关键词里会混进整段对话
    eq(_clean_query("我: 你好\n它: 你好呀\n我: 最新的安卓版本是多少"),
       "最新的安卓版本是多少", "多轮转写只取最后一条我")

    # --- _clean_html：meta/script 必须洗掉，正文必须留（实测踩过：四条资料
    #     里三条全是 og: 标签，模型拿着标签去答题）
    html_src = ('<title>安卓版本对照</title>\n'
                '<meta property="og:title" content="安卓版本">\n'
                '<meta charset=utf-8>\n'
                '<script>var x="安卓版本";</script>\n'
                '最新安卓版本是 16，API 级别 36，这是正文里真正有用的那句话。\n'
                '<!-- 注释 -->\n'
                '第二段正文：Android 17 目前还在开发者预览阶段，尚未正式推送。')
    clean = _clean_html(html_src)
    eq(any(k in clean for k in ("og:", "content=", "charset=", "<script", "var x")),
       False, "HTML 标签/meta 洗干净")
    eq("API 级别 36" in clean and "开发者预览" in clean, True, "正文没被洗掉")

    # --- _pick_relevant：相关段落要排到前面
    text = ("登录 注册 首页 导航\n\n"
            "这一段讲的是完全无关的东西，跟问题没有关系，长度也够二十四字以上。\n\n"
            "小米的无障碍服务会被系统在省电策略下静默关闭，这是已知问题，需要手动写回设置。\n\n"
            "底部版权信息 版权所有 长度也超过二十四字的填充段落。")
    got = _pick_relevant(text, _keywords("小米无障碍服务被关闭"), 200)
    first_line = got.split("\n")[0]
    eq("无障碍" in first_line, True, "相关段落排到首行")
    eq(len(got) <= 200, True, "摘段不超预算")

    # --- render_block：编号与标题必须出现
    blk = render_block({"sources": [
        {"i": 1, "title": "标题一", "url": "u1", "snippet": "", "body": "正文一"},
        {"i": 2, "title": "标题二", "url": "u2", "snippet": "摘要二", "body": ""},
    ]})
    eq("[1] 标题一" in blk, True, "render 编号+标题")
    eq("正文一" in blk and "摘要二" in blk, True, "render 用正文，缺则退回摘要")
    eq(render_block({"sources": []}), "", "无来源返回空串")
    eq(len(render_block({"sources": [
        {"i": 1, "title": "t", "body": "x" * 5000}]}, max_chars=100)) <= 100, True,
       "render 受 max_chars 约束")

    # --- augment 在 off 模式必须原样返回、不改 system
    sys0 = "你是终端。"
    got_sys, rep = augment(sys0, "今天天气", mode="off")
    eq(got_sys, sys0, "off 不改 system")
    eq(rep["used"], False, "off 未联网")
    eq(rep["reason"], "用户关了联网", "off 理由")

    # --- 垃圾过滤：用真机实测到的坑做样本
    # 实测搜「最新的安卓版本号是什么」，DDG 前四条全是靠堆热词抢排名的仓库，
    # 标题里一个「安卓」「版本」都没有。这组断言就是防止这种污染再进上下文。
    kws = _keywords("最新的安卓版本号是什么")
    spam = {"url": "https://github.com/x/y", "host": "github.com",
            "title": "反中共政治宣传库。Anti Chinese government propaganda. "
                     "常见问答集，新闻集和饭店和音乐建议。",
            "snippet": "大陆修宪 台湾 朝鲜 冷战"}
    eq(_junk_reason(spam, kws) != "", True, "政治垃圾仓库被拦住")

    eq(_junk_reason({"title": "安卓 16 版本号与适配说明", "host": "a.com",
                     "snippet": ""}, kws), "", "正常结果要放行")

    eq(_junk_reason({"title": "跟问题完全无关的长标题" * 8, "host": "b.com",
                     "snippet": ""}, kws) != "", True, "超长标题被拦")

    # 真机样本：搜「今天比特币什么价」排第一的那个 GitHub 仓库，96 字带 6 个问号，
    # 长度阈值差一个字没拦住 —— 问号数这条规则的由来。
    spam2 = {"url": "https://github.com/due65/version", "host": "github.com",
             "snippet": "",
             "title": "due65/version 最新比特币价格教程：比特币今天价格在哪里看最准确?"
                      "多家交易所之间为什么有价差?如何设置价格提醒避免踏空?"
                      "限价单与市价单怎么选更稳?资金费率和盘口深度应当如何一起评估?"}
    eq(_junk_reason(spam2, _keywords("今天比特币什么价")) != "", True,
       "问号堆砌被拦（真机样本）")

    eq(_junk_reason({"title": "随便什么标题", "host": "c.com", "snippet": ""},
                    kws) != "", True, "标题无字面重叠被拦")

    # 指代句（没有可匹配的词）不能把所有结果都杀光
    eq(_junk_reason({"title": "随便什么标题", "host": "c.com", "snippet": ""},
                    _keywords("那它呢")), "", "词太少时不套重叠规则")

    # --- 违法广告形状规则：2026-10-24 采集的真机样本（全部曾穿过旧规则）---
    # 这 12 条来自 18 条正常查询的 112 个原始命中，LLM 定性全部是
    # seo_spam / 违法，旧规则因为「标题含查询词」全部放行。
    # 这里传**空 kws**：隔离相关度规则，单独验证新形状判据
    # （它们当初就是靠「有字面重叠」漏过去的，新规则必须不依赖重叠也能拦）。
    ad_samples = [
        # (标题, 域名, 期望被拦的原因关键字；"退款" = 对照组，应放行)
        ("peniteopat-crypto/nba 足球直播_足球免费高清直播_足球直播在线观看-24直播网",
         "github.com", "堆砌"),
        ("alhaikawa/20261618-tech-resources-91ok3gob 足球直播在线观看无插件",
         "github.com", "GitHub寄生"),
        ("alhaikawa/20261618-tech-resources-uv4c857r 足球直播高清在线观看",
         "github.com", "GitHub寄生"),
        ("thethetpvmy/digg44 006足球现场直播,006足球现场直播-免费在线看",
         "github.com", "GitHub寄生"),
        ("z-libraryttg/dianyingwangzhan 2026免费影视在线观看网站，电影动漫连续剧在线看（持续更新）",
         "github.com", "GitHub寄生"),
        ("yuliangmao99/555dianying 555电影官网 - 最新Netflix新剧、韩国电影免费在线观看|最新网站",
         "github.com", "导流"),
        ("chenguanyou/porn_movie 使用Python的web框架django开发的一个能在线搜索和观看色情小电影的Web程序",
         "github.com", "成人"),
        ("jv95697/cryptographic 最新OKX APK教程：如何安全下载OKX官方APK文件？OKX安卓版本APK安装失败怎么办",
         "github.com", "假App"),
        ("mv3978/payment 最新欧易下载教程：欧易OKX怎么下载安装？OKX官方APP在哪里下载？",
         "github.com", "假App"),
        ("adiaodiaopongping/mlmlmlmilan 米兰app官方入口网站-(lyiwush.com)提供最新官方版本下载",
         "github.com", "外部域名"),
        ("pctelegramorg/pctelegramorg 在这里下载Telegram官网最新版，适用于所有主流操作系统。纸飞机",
         "github.com", "导流"),
        ("sud64827/bandwagonhost-refund-guide 搬瓦工退款多久到账？退款条件、申请流程与到账时间完整指南：支付宝/PayPal",
         "github.com", "退款"),
    ]
    # 最后一条是对照组：带问号的退款指南，问号只有 2 个、无品类词、无外部
    # 域名——它该被放行（证明新规则不是「见 GitHub 就杀」）。
    for i, (t, h, want) in enumerate(ad_samples):
        why = _junk_reason({"title": t, "host": h, "snippet": ""}, set())
        if want == "退款":
            eq(why, "", "两问号的正常页要放行（不误伤）")
        else:
            eq(want in (why or ""), True, f"违法广告样本{i + 1}被拦（{why}）")

    # --- 误伤对照：真开源项目/真站点必须放行 ---
    # moviecatcher 提「电影/在线观看/资源站」但通篇讲功能，没有一个入口词；
    # 它是本轮 LLM 判 normal 的真项目，形状规则必须放过它。
    ok_pages = [
        ("EvilCult/moviecatcher 电影美剧搜索及在线观看离线下载软件，集成热门资源站，"
         "借助百度云实现离线下载以及在线播放功能。", "github.com"),
        ("廖雪峰的官方网站 - Python教程", "liaoxuefeng.com"),
        ("Welcome to Python.org", "python.org"),
        ("kuaidi100-api/java-demo 快递100API-Java快递查询接口主要应用在电商商城",
         "github.com"),
        ("经常失眠怎么调理？从睡眠环境到睡前习惯的深度睡眠指南", "zhihu.com"),
        ("guolindev/coolweather 酷欧天气是一款基于Android端开源的天气预报软件",
         "github.com"),
        ("Python 安装教程 - python.org", "python.org"),
        ("欢迎来到 Python.org - Python 编程语言", "pythonlang.cn"),
    ]
    for i, (t, h) in enumerate(ok_pages):
        eq(_junk_reason({"title": t, "host": h, "snippet": ""}, set()),
           "", f"正常页{i + 1}要放行")

    kept, dropped = _search_filter("安卓版本", [
        {"url": "u1", "host": "same.com", "title": "安卓版本 A", "snippet": ""},
        {"url": "u2", "host": "same.com", "title": "安卓版本 B", "snippet": ""},
        {"url": "u3", "host": "same.com", "title": "安卓版本 C", "snippet": ""},
        {"url": "u4", "host": "other.com", "title": "安卓版本 D", "snippet": ""},
    ], 4)
    eq(len(kept), 3, "同站最多 2 条（站群防护）")
    eq(len(dropped), 1, "被丢的记一条")

    ok, n = _search_filter("话题", [
        {"url": "x", "host": "h", "title": "反中共 台独 藏独 疆独", "snippet": ""},
    ], 4)
    eq((len(ok), len(n)), (0, 1), "全垃圾时一条不留")

    if bad:
        print("WEBTOOL SELFTEST FAIL")
        for b in bad:
            print("  -", b)
        return 1
    print(f"WEBTOOL SELFTEST OK  (判定/分词/摘段/拼块/开关 共 {n_ok[0]} 项)")
    return 0


def main(argv=None):
    import json
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return _selftest()
    q = " ".join(argv)
    need, why = needs_web(q)
    print(f"要不要联网：{need}  （{why}）")
    if not need:
        print("加 --force 可强制联网")
        return 0
    res = research(q)
    print(f"用时 {res['elapsed']}s，来源 {len(res['sources'])} 条")
    for s in res["sources"]:
        print(f"  [{s['i']}] {s['title'][:60]}  ({s['chars']}字) {s['url'][:70]}")
    for n in res["notes"]:
        print("  !", n)
    print("-" * 50)
    print(render_block(res)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
