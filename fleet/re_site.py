"""站点逆向：把「拿不到 API 的站点」变成我们自己的引擎——「搜索叠加逆向」的发动机。

为什么需要它（2026-10-24 同一会话内的实测依据）：
  公共引擎池已经封顶。实测存活率：github 278/293（95%）、ddg 237/330（72%，
  中文查询说限流就归零）、bing 中文桶 76/460（17%，RSS 端在投喂串味缓存，
  全靠相关度闸门硬扛）、searx 0/0（实例池没探活）。加更多公共引擎是陪跑，
  花钱买 API 是别人的游戏。胜场只能建在付费 API 结构上给不了的东西上：
  我们自己逆向出来的私有引擎。

  这和扫网段是同一个道理：设备可以对群播装死，但只要开着生意，每个连上来
  的客户端它都得递东西——公开行情页为了把数据渲染进浏览器，不得不暴露自己
  的数据接口。身份在招牌上，不在自我介绍里。这个模块就是把「招牌」翻译成引擎。

三件套：
  probe()    端点发现：抓页面 → 提 JS bundle → 按模式认 API 端点（带证据行）
  quote()    站点适配器：一个站点一份「请求形状 + 字段解析」
  as_hits()  转成搜索命中 (url, title, desc) 三元组，插进聚合层当 pseudo-engine

🔴 纪律（写死在模块里，和扫描网络同一条红线）：
  - 只读公开数据：不登录、不碰需要鉴权的接口、不绕过付费墙
  - 不破验证码、不暴力尝试、不做任何写操作
  - 每个适配器注释里必须写清「这个接口是谁为了做生意不得不暴露的」
  - 被拦的端点（403/空响应/错误页）标「待刷新」不删除——免费额度会自己恢复，
    写死「不能用」等于自废（本地网关池铁律同源）
"""

import json
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: 各适配器的「人类可读页面」——命中三元组的 URL 指向人能看的页，不是接口本身
PAGE_EM_QUOTE = "https://quote.eastmoney.com/{secid}.html"
PAGE_EM_FUND = "https://fundf10.eastmoney.com/jjjz_{code}.html"
PAGE_TX = "https://gu.qq.com/{symbol}"


# ---------------------------------------------------------------- 基础抓取

def _get(url, referer=None, timeout=12.0):
    """最小 GET：返回 (ok, text, status, err)。不抛异常，失败也带原因。

    不走 skynet 的 net 层：这层的端点是**国内**的，net 层的代理/通道逻辑
    是给外网站群准备的，套上去只会坏菜。gb18030/utf-8 双尝试——腾讯的
    行情端点和东财的页面都是 GBK，硬解 utf-8 会得到一片问号。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": referer or "https://www.eastmoney.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = getattr(r, "status", 200) or 200
            ctype = r.headers.get("Content-Type") or ""
    except Exception as e:  # 超时/被拦/ DNS —— 一律转成可观测的失败
        return False, "", 0, f"{type(e).__name__}: {e}"
    # 解码顺序是**会静默出错**的坑：gb18030 几乎不抛 UnicodeDecodeError，
    # UTF-8 的中文字节会被它解成一堆错字——实测基金名「华夏成长混合」
    # 变成「鍗庡鎴愰暱娣峰悎」，不报错，只是错。所以顺序必须是：
    # Content-Type 声明 > utf-8 严格 > gb18030。腾讯端点声明 GBK，
    # 东财 JSON 声明 utf-8，各归其位；没声明的（push2）按 utf-8 先试。
    order = []
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        order.append(m.group(1).lower())
    order += ["utf-8", "gb18030"]
    for enc in order:
        try:
            return True, raw.decode(enc), status, ""
        except (UnicodeDecodeError, LookupError):
            continue
    return True, raw.decode("utf-8", "ignore"), status, ""


def _f(x, nd=2):
    """安全转 float，失败返回 None——宁可没有字段，不许编一个。"""
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _sign(x):
    """涨跌额/涨跌幅统一带符号。"""
    if x is None:
        return ""
    return f"+{x}" if x > 0 else f"{x}"


# ---------------------------------------------------------------- 端点发现

#: 认端点用的模式。每条都配一句「为什么这个形状像数据接口」。
_ENDPOINT_PATTERNS = [
    ("绝对接口", re.compile(
        r"https?://[a-z0-9.-]+/(?:api|qt|hq|push|v\d)/[^\s\"'<>\\]{4,160}", re.I),
     "域名后直接跟 /api /qt /hq /push 这类数据段"),
    ("取数调用", re.compile(
        r"(?:fetch|axios(?:\.\w+)?|\$\.ajax|getJSON|\.get)\s*\(\s*[\"'`]([^\"'`]{8,200})[\"'`]", re.I),
     "JS 里发请求的调用点，参数常是接口路径"),
    ("相对接口", re.compile(
        r"[\"'`](/[a-z0-9_\-/]*(?:api|json|list|query|getdata)[a-z0-9_\-/.]*)[\"'`]", re.I),
     "引号包着的站内路径，含数据段名"),
    ("JSONP", re.compile(
        r"(?:callback|cb|jsonp)\s*[=:]\s*[\"'`]?([a-zA-Z_]\w{2,20})", re.I),
     "JSONP 回调名——有它说明对面是给script标签用的数据接口"),
]


def probe(url, max_js=5, js_bytes=400_000, timeout=12.0):
    """端点发现：抓页面 → 提 JS bundle → 认 API 端点，每条带证据行。

    这是「逆向即引擎」的第一跳：面对一个从没见过的站点，先问它
    「你的 JS 里藏着哪些数据接口」。产出的是**候选端点 + 命中证据**，
    不是结论——候选要人工过一眼才能写成适配器（见各 adapter 的注释）。

    返回 {"ok", "target", "scripts", "endpoints", "error"}。
    """
    out = {"ok": False, "target": url, "scripts": [], "endpoints": [], "error": ""}
    ok, html, status, err = _get(url, timeout=timeout)
    if not ok or not html:
        out["error"] = err or f"HTTP {status}"
        return out
    srcs = re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
    urls = [urllib.parse.urljoin(url, s) for s in srcs]
    out["scripts"] = urls[:max_js]
    seen = set()
    for js_url in urls[:max_js]:
        ok, js, _, err = _get(js_url, referer=url, timeout=timeout)
        if not ok:
            continue
        js = js[:js_bytes]
        for kind, pat, why in _ENDPOINT_PATTERNS:
            for m in pat.finditer(js):
                cand = (m.group(1) or m.group(0)).strip()
                if len(cand) < 8 or cand in seen:
                    continue
                seen.add(cand)
                # 证据行：命中处前后各截一段，让人能回原文件核对
                s = max(0, m.start() - 40)
                out["endpoints"].append({
                    "endpoint": cand[:200],
                    "kind": kind,
                    "why": why,
                    "evidence": js[s:m.end() + 40].replace("\n", " ")[:160],
                    "from": js_url,
                })
        if len(out["endpoints"]) > 120:  # 防刷屏：够了就停
            break
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 东财 suggest（名称→代码）

#: searchapi 的 token 是**公开写在东财搜索页 JS 里的**（这正是 probe() 该抓的东西，
#: 这里先手工固化，等 probe 跑通东财页面就换成自动发现）。
_EM_SUGGEST = ("https://searchapi.eastmoney.com/api/suggest/get"
               "?input={q}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8")


def suggest(name):
    """名称 → (secid, code, 标准名, 市场标签, 是否基金)。查不到返回 None。

    东财的搜索建议接口：quote.eastmoney.com 的搜索框为了给用户补全，
    不得不把全市场代码表开放出来。返回的 QuoteID 字段**就是 push2 的 secid**，
    一个接口同时喂两只适配器。
    """
    url = _EM_SUGGEST.format(q=urllib.parse.quote(name))
    ok, txt, _, err = _get(url, referer="https://quote.eastmoney.com/")
    if not ok:
        return None
    try:
        d = json.loads(txt)
        row = (d.get("QuotationCodeTable") or {}).get("Data") or []
        if not row:
            return None
        r = row[0]
        return {
            "secid": r.get("QuoteID") or "",
            "code": r.get("Code") or "",
            "name": r.get("Name") or "",
            "market": r.get("SecurityTypeName") or "",
            "is_fund": "Fund" in (r.get("Classify") or ""),
            "classify": r.get("Classify") or "",
        }
    except Exception:
        return None


# ---------------------------------------------------------------- 适配器 1：东财 push2（A股/港股 结构化行情）

_EM_PUSH2 = ("https://push2.eastmoney.com/api/qt/stock/get"
             "?secid={secid}&fields=f43,f57,f58,f46,f44,f45,f60,f47,f48,"
             "f168,f169,f170,f86")

#: 字段表（2026-10-24 实测定稿，f43/f57/f58/f60/f169/f170 已对账）：
#:   f43 最新价×100  f57 代码  f58 名称  f46 今开×100  f44 最高×100
#:   f45 最低×100    f60 昨收×100  f47 成交量(手)  f48 成交额(元)
#:   f168 换手率×100 f169 涨跌额×100  f170 涨跌幅×100  f86 行情时间(unix)


def _em_push2(secid):
    """A股/港股实时行情。结构化 JSON，不用解 GBK——适配器里最稳的一个。

    为什么能白拿：quote.eastmoney.com 的行情看盘页要把价格渲染进浏览器，
    这个接口就是它自己的数据源。Referer 校验只认站内，带一个即可。

    ⚠ 2026-10-24 实测：连续请求约 6 次后被限流（返回空/断连），
    按窗口自行恢复——状态标「待刷新」，适配器不删（腾讯那条路当时
    无缝兜住了全部查询）。"""
    ok, txt, _, err = _get(_EM_PUSH2.format(secid=secid),
                           referer="https://quote.eastmoney.com/")
    if not ok:
        return None, err
    try:
        d = (json.loads(txt) or {}).get("data") or {}
        if not d.get("f57"):
            return None, "空数据"
        p100 = lambda k: _f(d[k], 2) / 100 if d.get(k) not in (None, "") else None
        ts = d.get("f86")
        return {
            "kind": "hk" if secid.startswith("116.") else "stock",
            "code": str(d.get("f57") or ""),
            "name": d.get("f58") or "",
            "price": p100("f43"),
            "open": p100("f46"),
            "high": p100("f44"),
            "low": p100("f45"),
            "prev_close": p100("f60"),
            "change": p100("f169"),
            "change_pct": p100("f170"),
            "volume_lots": _f(d.get("f47"), 0),
            "amount": _f(d.get("f48"), 0),   # f48 单位：元
            "turnover": p100("f168"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(int(float(ts)))) if ts else "",
            "source": "eastmoney/push2",
        }, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 适配器 2：腾讯 qt.gtimg.cn（A股/港股 富字段）

_TX_QT = "https://qt.gtimg.cn/q={symbols}"

#: v_ 负载的 ~ 分隔字段表（2026-10-24 用 sz000001/sh600519/hk00700 实测定稿）：
#:   1 名称  2 代码  3 现价  4 昨收  5 今开  6 成交量(手)  30 行情时间串
#:   31 涨跌额  32 涨跌幅%  33 最高  34 最低  37 成交额(万)  38 换手率%
#:   39 市盈率  43 振幅%  44 流通市值(亿)  45 总市值(亿)  46 市净率


def _tx_qt(symbols):
    """腾讯行情：富字段（市值/市盈率/振幅），GBK 编码，`v_xxx="..."` 一行一个。

    为什么能白拿：gu.qq.com 的页面就是拿这个接口刷行情的，浏览器不登录
    也看得见。腾讯侧没有 Referer 校验，是最「干净」的一个适配器。
    """
    ok, txt, _, err = _get(_TX_QT.format(symbols=",".join(symbols)))
    if not ok:
        return {}, err
    out = {}
    for m in re.finditer(r'v_([a-z0-9.]+)="([^"]*)"', txt):
        sym, payload = m.group(1), m.group(2)
        f = payload.split("~")
        if len(f) < 40 or not f[1]:
            continue
        g = lambda i: f[i] if i < len(f) else ""
        out[sym] = {
            "kind": "hk" if sym.startswith("hk") else "stock",
            "code": g(2) or sym[2:],
            "name": g(1),
            "price": _f(g(3)),
            "prev_close": _f(g(4)),
            "open": _f(g(5)),
            "high": _f(g(33)),
            "low": _f(g(34)),
            "change": _f(g(31)),
            "change_pct": _f(g(32)),
            "volume_lots": _f(g(6), 0),
            "amount": (_f(g(37), 0) or 0) * 10000,   # 腾讯是万元，归一成元
            "turnover": _f(g(38)),
            "pe": _f(g(39)),
            "amplitude": _f(g(43)),
            "mcap_flow": _f(g(44)),          # 单位：亿元
            "mcap_total": _f(g(45)),
            "pb": _f(g(46)),
            "time": (g(30)[:4] + "-" + g(30)[4:6] + "-" + g(30)[6:8] + " "
                     + g(30)[8:10] + ":" + g(30)[10:12] + ":" + g(30)[12:14])
            if len(g(30)) >= 14 else g(30),
            "source": "tencent/qt",
        }
    return out, "" if out else "无匹配代码"


# ---------------------------------------------------------------- 适配器 3：东财 f10（场外基金净值）

_EM_FUND_NAV = ("https://api.fund.eastmoney.com/f10/lsjz"
                "?fundCode={code}&pageIndex=1&pageSize=1")


def _em_fund_nav(code):
    """场外基金最新净值。fundf10 的净值页面自己的数据源。

    为什么能白拿：天天基金的历史净值页要把表格渲染出来，就得问这个接口。
    注意 000001 这种代码**股票基金重号**（平安银行 vs 华夏成长）——
    走基金路必须显式给 kind，不能靠代码猜。
    """
    ok, txt, _, err = _get(_EM_FUND_NAV.format(code=code),
                           referer=f"https://fundf10.eastmoney.com/jjjz_{code}.html")
    if not ok:
        return None, err
    try:
        rows = ((json.loads(txt) or {}).get("Data") or {}).get("LSJZList") or []
        if not rows:
            return None, "无净值记录"
        r = rows[0]
        return {
            "kind": "fund",
            "code": code,
            "name": "",                       # 这个接口不返名称，由调用方补
            "nav": _f(r.get("DWJZ"), 4),
            "nav_acc": _f(r.get("LJJZ"), 4),
            "nav_date": r.get("FSRQ") or "",
            "nav_growth": _f(r.get("JZZZL")),
            "sub_status": r.get("SGZT") or "",
            "redeem_status": r.get("SHZT") or "",
            "source": "eastmoney/f10",
        }, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 统一入口

#: 行情意图关键词。命中就多问一句适配器——问不到不影响主搜索（聚合层只做前置注入）。
_QUOTE_KW = ("股价", "股票", "股市", "行情", "净值", "基金", "港股", "a股", "a 股",
             "大盘", "市价", "涨跌", "收盘", "开盘价", "什么价", "多少钱一股",
             "市净值", "市盈率", "成交额", "自选")

_FUND_KW = ("基金", "净值", "收益", "赎回", "申购")
_STOCK_KW = ("股", "股价", "股市", "行情", "a股", "港股", "大盘")


def is_quote_query(q):
    """这句话是不是在问行情。宁可多问（问不到不丢人），不可漏问。"""
    s = (q or "").strip().lower()
    if not s:
        return False
    if re.search(r"(?:^|\s)(?:sz|sh|hk|bj)\d{5,6}(?:\s|$)", s):
        return True
    if re.search(r"(?<!\d)\d{6}(?!\d)", s):
        return True
    return any(k in s for k in _QUOTE_KW)


def _secid_of(code):
    """6 位代码 → 东财 secid。北交所(4/8 开头)和深市同走 0. 前缀。"""
    if code.startswith(("6", "9")):
        return "1." + code
    return "0." + code


def _tx_symbol_of(code):
    """6 位代码 → 腾讯 symbol。"""
    return ("sh" if code.startswith("6") else "sz") + code


def quote(query, kind=None):
    """一次查询 → 归一化行情。返回 dict（不抛异常，失败也带原因）。

    kind: None=自动（关键词+重号默认股票）/ "stock" / "fund"
    """
    q = (query or "").strip()
    errors = []
    result = {"ok": False, "query": q, "errors": errors}
    if not q:
        result["errors"].append("空查询")
        return result

    # 1) 抠代码：显式市场前缀优先，否则纯 6 位
    code, prefix = None, ""
    m = re.search(r"(sz|sh|hk|bj)\s*(\d{5,6})", q, re.I)
    if m:
        prefix, code = m.group(1).lower(), m.group(2)
    else:
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", q)
        code = m.group(1) if m else None

    want_fund = kind == "fund" or any(k in q for k in _FUND_KW)

    # 2) 有代码：直接走适配器（重号默认股票，除非显式要基金）
    if code and not want_fund:
        if prefix == "hk":
            secid, sym = "116." + code.zfill(5), "hk" + code.zfill(5)
        else:
            secid, sym = _secid_of(code), _tx_symbol_of(code)
        d, err = _em_push2(secid)
        if not d:
            errors.append(f"push2: {err}")
        else:
            result.update(d)
            result["ok"] = True
            result["market"] = _market_label(secid)
            # 腾讯补富字段（市值/市盈率）——失败不耽误主结果
            tx, _ = _tx_qt([_tx_symbol_of(code)])
            if tx:
                result["extra"] = tx.get(_tx_symbol_of(code), {})
            return result
        # push2 挂了：腾讯兜底（它才是那个不校验 Referer 的）
        sym = _tx_symbol_of(code)
        tx, err2 = _tx_qt([sym])
        if sym in tx:
            result.update(tx[sym])
            result["ok"] = True
            result["market"] = _market_label(secid)
            return result
        errors.append(f"qt.gtimg: {err2}")
        # 股票路全挂：万一这是只场外基金（代码重号，如 005827 只有基金）——
        # 再问一次净值接口，还不行为止。
        nav, err3 = _em_fund_nav(code)
        if nav:
            result.update(nav)
            result["ok"] = True
            result["market"] = "场外基金"
        else:
            errors.append(f"基金净值: {err3}")
        return result

    # 3) 没代码或要基金：先 suggest 把名称翻成代码
    name = re.sub(r"(股价|股票|行情|净值|基金|多少钱|什么价|现在|最新|查询|"
                  "一下|的|是|多少|a股|港股|今天)", "", q).strip()
    if not name:
        result["errors"].append("没抠出代码，也没抠出名称")
        return result
    hit = suggest(name)
    if not hit:
        result["errors"].append(f"suggest 查不到「{name}」")
        return result

    if want_fund or hit.get("is_fund") and kind != "stock":
        nav, err = _em_fund_nav(hit["code"])
        if nav:
            nav["name"] = hit["name"] or nav["name"]
            result.update(nav)
            result["ok"] = True
            result["market"] = hit.get("market") or "场外基金"
            return result
        errors.append(f"基金净值: {err}")
        # 基金路挂了别急着退——万一 suggest 给的是股票呢
        if hit.get("secid"):
            d, err = _em_push2(hit["secid"])
            if d:
                result.update(d)
                result["ok"] = True
                result["market"] = hit.get("market") or ""
                return result
            errors.append(f"push2: {err}")
        return result

    if hit.get("secid"):
        d, err = _em_push2(hit["secid"])
        if d:
            result.update(d)
            result["ok"] = True
            result["market"] = hit.get("market") or ""
            if hit["secid"].startswith("116."):
                sym = "hk" + (hit.get("code") or "").zfill(5)
                tx, _ = _tx_qt([sym])
                if tx:
                    result["extra"] = tx.get(sym, {})
            return result
        errors.append(f"push2: {err}")
        # 腾讯兜底：A股用 sh/sz 前缀，港股 hk+5 位补零
        code0 = hit.get("code") or ""
        sym = ("hk" + code0.zfill(5) if hit["secid"].startswith("116.")
               else _tx_symbol_of(code0) if code0 else "")
        if sym:
            tx, err2 = _tx_qt([sym])
            if sym in tx:
                result.update(tx[sym])
                result["ok"] = True
                result["market"] = hit.get("market") or ""
                return result
            errors.append(f"qt.gtimg: {err2}")
    else:
        errors.append("suggest 没给 secid")
    return result


def _market_label(secid):
    if secid.startswith("116."):
        return "港股"
    if secid.startswith("1."):
        return "沪A"
    return "深A"


def hit_from(d):
    """一份 quote() 结果 → 单条搜索命中（没查出东西返回 None）。

    标题把结论写全（名称/价格/涨跌幅），摘要把证据写全（开高低量额/时间），
    URL 指向人类可读页面——和 skynet 的 Hit 一个形状，下游零改造。
    """
    if not d.get("ok"):
        return None
    name, code = d.get("name") or "", d.get("code") or ""
    market = d.get("market") or ""
    if d.get("kind") == "fund":
        title = (f"{name}（{code}）最新净值 {d.get('nav')}"
                 f"（{d.get('nav_date')}）日涨跌 {_sign(d.get('nav_growth'))}%")
        desc = (f"单位净值 {d.get('nav')} · 累计净值 {d.get('nav_acc')} · "
                f"日增长率 {_sign(d.get('nav_growth'))}% · 净值日期 {d.get('nav_date')} · "
                f"{d.get('sub_status')}/{d.get('redeem_status')} · 来源 {d.get('source')}")
        url = PAGE_EM_FUND.format(code=code)
        host = "fundf10.eastmoney.com"
    else:
        pct = d.get("change_pct")
        title = (f"{name}（{code}）{d.get('price')} "
                 f"{_sign(d.get('change'))}（{_sign(pct)}%）· {market}")
        ex = d.get("extra") or {}
        bits = [f"今开 {d.get('open')}", f"最高 {d.get('high')}",
                f"最低 {d.get('low')}", f"昨收 {d.get('prev_close')}"]
        if d.get("volume_lots"):
            bits.append(f"成交量 {d['volume_lots'] / 10000:.1f} 万手")
        if d.get("amount"):
            bits.append(f"成交额 {d['amount'] / 1e8:.2f} 亿元")
        if d.get("turnover"):
            bits.append(f"换手 {d['turnover']}%")
        if ex.get("mcap_total"):
            bits.append(f"总市值 {ex['mcap_total']} 亿")
        bits.append(f"行情时间 {d.get('time')} · 来源 {d.get('source')}")
        desc = " · ".join(bits)
        url = (PAGE_TX.format(symbol="hk" + code.zfill(5)) if d.get("kind") == "hk"
               else PAGE_EM_QUOTE.format(secid=_secid_of(code)))
        host = "gu.qq.com" if d.get("kind") == "hk" else "quote.eastmoney.com"
    return {
        "url": url,
        "host": host,
        "title": title,
        "desc": desc,
        "engines": ["re_site"],
        "rel": 1.0,
        "score": 999.0,     # 自建引擎的命中是精确答案，永远压过公网泛结果
    }


def as_hits(query):
    """一次查询 → 搜索命中列表（空 = 没问出来）。见 hit_from()。"""
    h = hit_from(quote(query))
    return [h] if h else []


# ---------------------------------------------------------------- 自测

def _selftest(live=False):
    """离线用真机样本对账（2026-10-24 实采），--live 再加真实网络检查。"""
    n = [0, 0]

    def eq(got, want, msg):
        n[1] += 1
        if got == want:
            n[0] += 1
        else:
            print(f"  FAIL {msg}: got={got!r} want={want!r}")

    # --- 腾讯 GBK 负载解析（实采：sz000001 平安银行，2026-10-23 收盘）---
    tx_raw = ('v_sz000001="51~平安银行~000001~11.20~11.30~11.27~1448806~695504~753301~'
              '11.20~9557~11.19~12196~11.18~7747~11.17~8122~11.16~9094~11.21~554~11.22~2801~'
              '11.23~2036~11.24~2851~11.25~2394~~20261023161500~-0.10~-0.88~11.35~11.17~'
              '11.20/1448806/1628277173~1448806~162828~0.75~5.00~~11.35~11.17~1.59~2173.44~'
              '2173.46~0.46~12.43~10.17~1.26~36080~11.24~4.16~5.10~~~0.18~162827.7173~'
              '25.5360~228~   A~GP-A~3.69~-2.61~5.44~7.93~0.72~11.89~9.74~-3.36~-1.42~3.41~'
              '19405684991~19405918198~62.91~7.06~19405684991~~~1.62~0.09~~CNY~0~~11.11~'
              '16500~";')
    _orig = _get

    def _fake_get(url, referer=None, timeout=12.0):
        if "qt.gtimg.cn" in url:
            return True, tx_raw, 200, ""
        return _orig(url, referer=referer, timeout=timeout)

    globals()["_get"] = _fake_get
    try:
        tx, err = _tx_qt(["sz000001"])
        eq(tx.get("sz000001", {}).get("name"), "平安银行", "腾讯负载名称")
        eq(tx.get("sz000001", {}).get("price"), 11.20, "腾讯负载现价")
        eq(tx.get("sz000001", {}).get("change_pct"), -0.88, "腾讯负载涨跌幅")
        eq(tx.get("sz000001", {}).get("mcap_total"), 2173.46, "腾讯负载总市值")
        eq(tx.get("sz000001", {}).get("time"), "2026-10-23 16:15:00", "腾讯负载时间串")
    finally:
        globals()["_get"] = _orig

    # --- 意图识别 ---
    eq(is_quote_query("000001"), True, "裸代码是行情意图")
    eq(is_quote_query("平安银行股价"), True, "关键词是行情意图")
    eq(is_quote_query("hk00700"), True, "港股代码是行情意图")
    eq(is_quote_query("python 安装教程"), False, "技术查询不是行情意图")
    eq(is_quote_query(""), False, "空查询不是行情意图")

    # --- secid / symbol 映射 ---
    eq(_secid_of("600519"), "1.600519", "沪市 secid")
    eq(_secid_of("000001"), "0.000001", "深市 secid")
    eq(_secid_of("430047"), "0.430047", "北交所 secid")
    eq(_tx_symbol_of("600519"), "sh600519", "沪市腾讯代码")

    # --- 基金命中形状（假数据走完整条 as_hits 路径）---
    def _fake_quote(q):
        return {"ok": True, "kind": "fund", "code": "000001", "name": "华夏成长混合A",
                "nav": 1.046, "nav_acc": 3.619, "nav_date": "2026-10-23",
                "nav_growth": 2.05, "sub_status": "开放申购", "redeem_status": "开放赎回",
                "source": "eastmoney/f10", "market": "场外基金"}
    _orig_quote = quote
    globals()["quote"] = _fake_quote
    try:
        hits = as_hits("华夏成长 净值")
        eq(len(hits), 1, "基金命中一条")
        h = hits[0]
        eq("华夏成长混合A" in h["title"] and "1.046" in h["title"], True, "基金标题带结论")
        eq("累计净值 3.619" in h["desc"], True, "基金摘要带证据")
        eq(h["url"], "https://fundf10.eastmoney.com/jjjz_000001.html", "基金 URL 指人页")
        eq(h["engines"], ["re_site"], "引擎标记")
    finally:
        globals()["quote"] = _orig_quote

    # --- 边界：查不到就返回空，不编 ---
    eq(as_hits(""), [], "空查询无命中")
    eq(quote(""), {"ok": False, "query": "", "errors": ["空查询"]}, "空查询如实失败")

    if live:
        print("  --live：真实网络检查 --")
        for q in ["000001", "平安银行股价", "600519", "腾讯控股股价"]:
            d = quote(q)
            print(f"    {q!r:16s} -> ok={d.get('ok')} "
                  f"{d.get('name', '')} {d.get('price', '')} {d.get('change_pct', '')}% "
                  f"[{d.get('source', '')}] {d.get('errors', '')}")
        d = quote("华夏成长混合 净值")
        print(f"    {'基金 000001':16s} -> ok={d.get('ok')} "
              f"{d.get('name', '')} nav={d.get('nav', '')} [{d.get('source', '')}] "
              f"{d.get('errors', '')}")

    print(f"re_site 自测：{n[0]}/{n[1]} 通过")
    return n[0] == n[1]


if __name__ == "__main__":
    import sys
    ok = _selftest(live="--live" in sys.argv)
    sys.exit(0 if ok else 1)
