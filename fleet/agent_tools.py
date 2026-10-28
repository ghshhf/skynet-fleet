# -*- coding: utf-8 -*-
"""agent_tools —— 手机助手的工具面：决策 schema + 服务端执行器。

为什么是这个形态（2026-10-24 定，全部有实测依据）：

1. 决策 = schema 枚举 + 两级收窄（2026-10-29 起）。走
   `response_format: json_schema`，先把候选压到域内 4~13 个，再让模型选。
   **别再引用「7 选 1，16/16 全对」当现状**——那是只有 7 个工具时的成绩；
   扩到 58 个之后，prompt 2874 tokens、在这台核显机器上要 52.9 秒，
   超时线 25 秒，等于每次都失败（第 25/26 条，见 docs/总纲）。
   现在：Qwen3.5-2B + 七域两级，产线 decide() 30/30、平均 5.9s。
   小模型守得住的约束形式只有 schema 枚举——OpenAI tool_calls 协议它发不出来
   （0.6B/1.7B 都试过），多轮 agent loop 会崩（BFCL V4：4B 多轮 22%）。

2. 执行 = 服务端调能力注册表（caller callable），模型永远不直接碰 adb。
   模型输出的参数一律视为不可信：arg 只进白名单字段，执行层再校验。

3. 输出预算：工具结果一律 ≤ MAX_RESULT_CHARS(1200) 字符。
   一次搜索塞 31 万字撑爆 4096 上下文的教训（HTTP 400），参数层再防一次。

4. 规则短路：纯代码/算术/翻译/寒暄不进决策——省 4~8s 延迟，
   也补小模型「不该调时乱调」的短板（BFCL：0.6B 只有 76.5%）。
"""
import json
import os
import re
import time
import urllib.request

from . import toolselect

#: 决策模型地址（OpenAI 兼容 /v1/chat/completions）。运行时可用 AT_DECIDER_URL 覆盖。
#:
#: 🔴 2026-10-26：默认回到本地 llama-server（18080）。
#: 中途试过把默认指向 pollinations 免费端点，**实测不可用**：5 次里 4 次失败
#: ——402 Payment Required、读超时、以及空字符串（返回了 200 但 content 是空的），
#: 匿名免费额度扛不住「每说一句话调一次」的频率。第二个理由更重要：决策输入
#: 是用户原话，走云端等于把这句话发给第三方，与项目「数据不出本机」相悖。
#: 现在默认本地（免费、稳定、不外传）；要换就设 AT_DECIDER_URL。
#: 服务没起时 decide() 返回 None，调用方 fire("warn") 如实告知，绝不静默跳过。
DECIDER_URL = "http://127.0.0.1:18080"

#: 工具结果回填进 prompt 的上限（字符）。
MAX_RESULT_CHARS = 1200

#: 决策调用超时（秒）。手机 1.7B 实测平均 5.1s，超了就当没有工具面。
#: 🔴 2026-10-26：10 → 25。本地 Qwen3.5-4B 实测单次决策 4.2s～9.3s（schema 约束解码
#: 比普通生成慢），10 秒会**误杀合法回答**——模型答对了，只是慢一点，decide() 却
#: 返回 None，表现为工具面「时灵时不灵」，且极难查（日志里只剩一句超时，看不出
#: 是模型答错还是没赶上）。25 秒的代价是真挂了要多等一会儿才降级，值。
DECIDE_TIMEOUT = 25.0

#: 🔴 2026-10-26：决策失败原因。用列表容器是故意的——函数内改字符串要 global 声明，
#: 改容器元素不用。
#:
#: 背景：decide() 原来所有失败一律 `return None`，调用方只能发一句「连不上」。
#: 排查时吃过大亏——模型其实答对了，只是慢了 0.3 秒被超时误杀，日志却说是连不上，
#: 于是往「服务没起」「地址填错」的方向查了五六轮。失败原因不分类，等于让下一次
#: 排查的人重新踩一遍。现在分：连不上 / 超时 / 非 JSON / 工具不在清单。
LAST_FAIL = [""]

def _fail(msg):
    LAST_FAIL[0] = msg


#: 这次失败是不是「暂时性」的（换一个端点可能就通了）。
#: 用容器元素而不是全局字符串，理由同 LAST_FAIL。
#:
#: 为什么要这个标记（2026-10-29）：接上外部算力之后第一件事就撞了 429 ——
#: zen-gate 的免费额度被我连跑三轮跑分用光，于是 decide() 直接返回 None，
#: 整个工具面消失。**外部算力必然有限流**，所以「一个端点不给算」不等于
#: 「这件事做不了」。但也不能乱换：
#:   · 429 / 5xx / 超时 / 连不上  → 暂时性，换下一个端点有意义
#:   · 401 / 404 / 返回不是 JSON  → 配置或模型的问题，换过去一样错，
#:                                  还多烧一轮时间和额度
#: 这个区分跟 `ai_backends.classify_failure` 是同一套判据，别各写一份。
LAST_RETRYABLE = [False]

#: 权限三档。定义提前到文件顶部是因为 TOOLS 表要引用它们。
#: auto = 直接跑；ask = 交给审批闸门（无闸门即 fail-closed 拒绝）；forbid = 永不跑。
#: 与 Operit 的三档对齐，但**默认更保守**：有副作用的默认 ask，
#: 短信/联系人/通讯录类能力压根没进工具表（隐私边界，不跟随 Operit 的激进面）。
#: DEFAULT_TIERS 不再是手写字典——它由 TOOLS 表派生（见 _derive）。
PERM_AUTO, PERM_ASK, PERM_FORBID = "auto", "ask", "forbid"

# ---------------------------------------------------------------- 决策

#: DECIDE_SCHEMA / _TOOLS_LINE / DECIDE_SYSTEM / EXECUTORS / DEFAULT_TIERS
#: 全部从文件后面的 TOOLS 表派生（单一事实源）。加工具只改 TOOLS 一处——
#: 曾经分散在四处，加工具必漏一处，且漏了不会报错、只会静默不生效。
#: 这里只放占位，真正的值在 TOOLS 定义之后由 _derive() 写回模块全局。
DECIDE_SCHEMA = None
_TOOLS_LINE = ""
DECIDE_SYSTEM = ""

#: 规则短路：命中的问题直接 none，不花 5 秒问模型。
#: 刻意收窄（只挡高频且无歧义的），宁多问模型也别错杀真需求。
_RE_NONE = re.compile(
    r"写.{0,12}(函数|脚本|代码|程序)|python|java\b|javascript|"
    r"翻译|译成|怎么读|怎么说|"
    r"^(你好|您好|嗨|hi|hello|在吗|谢谢|再见|晚安|早上好)"
    r"|^\s*\d+\s*[+\-*/×÷]\s*\d+[\s=？?]*$",
    re.I,
)


def obvious_none(question):
    """规则短路：True = 明确不需要任何工具。"""
    q = (question or "").strip()
    if not q:
        return True
    return bool(_RE_NONE.search(q))


# ---- 决策缓存：同一句话第二次直接命中，省掉 4–9 秒的模型推理 ----
# 为什么能缓存：决策只取决于「这句话 → 用哪个工具」，和当前时间/状态无关
# （同一句话该调的工具不会变；变了是工具表变了，那得重启）。小模型决策实测
# 4.2–9.3s，命中等于把整段省掉。**失败不缓存**——那是暂时状态（连不上/超时），
# 下次该重试，缓存失败会把「服务重启就能好」变成「永远坏」。
_DECIDE_CACHE = {}
_DECIDE_CACHE_MAX = 128


def _cache_get(key):
    return _DECIDE_CACHE.get(key)


def _cache_put(key, val):
    if len(_DECIDE_CACHE) >= _DECIDE_CACHE_MAX:
        _DECIDE_CACHE.pop(next(iter(_DECIDE_CACHE)))    # FIFO，最老的先走
    _DECIDE_CACHE[key] = dict(val)


def _timeout_msg(timeout, base):
    """超时时说清楚「是模型慢，还是工具面太大」。

    2026-10-29：本机实测 58 个工具 = 6271 tokens、光 prompt 就 56.2 秒，
    而 DECIDE_TIMEOUT=25s。原提示只说「模型答得慢，可加大超时」——照着调
    只会把 25 调到 60，然后每句话都要等一分钟，还是慢，还是不知道为什么。
    超时最常见的真原因不是模型慢，是**清单太长**：本机同一条问句实测
    6 个工具 6.2s / 10 个 8.2s / 20 个 14.9s / 58 个 56.2s。
    所以直接把这几个点报出来，让人一眼看出该裁不该调。
    """
    n = len(DECIDE_SCHEMA["properties"]["tool"]["enum"])
    return ("超时 %.0fs（%s）——当前工具面 %d 个选项。本机实测 6 个 6.2s / "
            "10 个 8.2s / 20 个 14.9s / 58 个 56.2s：先裁工具面，"
            "别急着调大 DECIDE_TIMEOUT" % (timeout, base, n))


#: 工具面能不能一次全给：'' = 两级（规则定域 → 域内选），'1' = 58 个一次给。
#:
#: 🔴 2026-10-29：默认两级，原因是实测不是偏好。这台开发机是 Intel 核显、
#: 全程 CPU（`-ngl 99` 是空转），Qwen3.5-2B 实测 **prompt 处理 54–65 tok/s、
#: 生成 3.3 tok/s**：
#:     58 个工具 2874 tokens ⇒ 52.9s（超时线 25s，必死）
#:     13 个工具  468 tokens ⇒  7.2s + 生成 3.6s ≈ 11s（能过）
#: 一次全给看起来「4 秒就答完」，那是**命中 prompt 缓存**（前缀一模一样才命中），
#: 只要工具清单或前缀变一个字就退回 53 秒——不能拿缓存后的数字当预算。
#: 云端大模型（上下文大、prefill 快）可以设 AT_DECIDE_FULL=1 关掉两级。
def _full_mode():
    return os.environ.get("AT_DECIDE_FULL", "") == "1"


def _schema_for(enum):
    """决策输出 schema。全量模式和域内模式共用这一份，只换 enum。"""
    return {
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "enum": list(enum),
                "description": "选中的名字，必须从 enum 里原样抄写（如 device_status）；"
                               "不需要任何工具时填 none",
            },
            "arg": {
                "type": "string",
                "description": "工具的参数（搜索词/应用名/域名等）；不需要就留空字符串",
            },
        },
        "required": ["tool", "arg"],
    }


def _chat_url(base):
    """把端点地址拼成 chat 完整地址。

    **两种写法都会出现，必须都认**（2026-10-29 踩过）：
      · `http://127.0.0.1:18080`      —— 老的环境变量写法，不带 /v1
      · `http://127.0.0.1:8787/v1`    —— 算力层统一写到版本层

    只按前一种拼，后者会变成 `/v1/v1/chat/completions`（404）；
    只按后一种拼，前者会变成 `/chat/completions`（也 404）。
    而 404 会被误读成「这个厂商不支持」，所以判断收在这一处。
    """
    b = (base or "").rstrip("/")
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def _decider_target(url=None):
    """决策走哪个端点。返回 (base, key, model)。

    三层优先级，每层都说清凭什么：
      ① 调用方显式传 `url`        —— 跑分脚本 / 临时指定
      ② 环境变量 `AT_DECIDER_URL` —— 部署时钉死（老路，保持兼容）
      ③ 算力配置 `compute.route("decide")` —— 桌面端「算力」页里选的

    前两层只给**地址**，第三层还给**key 和模型名**。
    所以前两层要回查一次算力层：用户可能先用 env 写了地址，后来在桌面上
    才补的 key —— 不回查的话，那个 key 永远用不上，表现为 401。

    🔴 这一层存在的原因：在这之前决策端点只能靠环境变量，不认 key。
    于是「外接算力」在代码上根本走不通（云端哪有不鉴权的），
    「58 个工具选不动」就只能一路去抠本地小模型。
    """
    explicit = (url or os.environ.get("AT_DECIDER_URL") or "").strip()
    if explicit:
        base = explicit.rstrip("/")
        try:
            from . import compute as _C
            for ep in _C._load()[0]:
                if ep.base_url.rstrip("/") == base or \
                        ep.base_url.rstrip("/") == base + "/v1" or \
                        ep.base_url.rstrip("/").rsplit("/v1", 1)[0] == base:
                    return base, ep.key, (ep.model or "")
        except Exception:
            pass
        return base, "", ""

    try:
        from . import compute as _C
        ep = _C.route("decide")
        if ep is not None:
            return ep.base_url.rstrip("/"), ep.key, (ep.model or "")
    except Exception:
        pass
    return DECIDER_URL, "", ""


def _content_of(d):
    """从回包里取「它到底说了什么」。返回 (文本, 来源)。

    🔴 2026-10-29 实测（zen-gate / 思考型模型）：
        finish_reason: stop
        content: ''                    ← 空
        reasoning_content: '用户问手机还剩多少电，我需要查手机自身状态，
                            应该用 device_status 工具。'
    **它想对了，答案写在思考区，正文是空的。** 只读 `content` 就等于判它
    「答歪了」，然后一路去调提示词、换模型——全在修一个不存在的毛病。

    这是本项目第三次撞上同一形状：HTTP 200 且 finish_reason=stop，
    正文却不是「答案」。前两次是 jinja 报错文本（模板参数名错）、
    以及 prompt 压根没进去（tokens 恒等 18）。所以**凡是说「模型不会」之前，
    先确认拿到的这段文字是它真的回答**。
    """
    try:
        m = d["choices"][0].get("message") or {}
    except Exception:
        return "", ""
    txt = (m.get("content") or "").strip()
    if txt:
        return txt, "content"
    for alt in ("reasoning_content", "reasoning", "thinking"):
        t = (m.get(alt) or "").strip()
        if t:
            return t, alt
    return "", ""


_JSON_IN_TEXT = re.compile(r"\{[^{}]{0,200}\}")


def _salvage(txt, enum):
    """从「不是 JSON 的回答」里捞出结构化的答案。捞不到就返回 None。

    只在两种情况下认（**不许瞎猜**）：
      ① 文本里有一段合法 JSON 对象，且它的 tool 值在清单里
      ② 文本里恰好只提到一个清单里的工具名（唯一命中）

    第 ② 条要求「唯一」：同时提到两三个工具名的思考过程，一律不认——
    那是在权衡，不是在回答。认错了比认不出更坏：调用方会去执行一个错的工具。
    """
    for frag in _JSON_IN_TEXT.findall(txt):
        try:
            o = json.loads(frag)
        except Exception:
            continue
        if isinstance(o, dict) and _pick_name(o, enum):
            return o
    hits = [n for n in enum if n and n != "none" and n in txt]
    if len(hits) == 1:
        return {"tool": hits[0], "arg": ""}
    return None


def _ask(target, system, enum, q, timeout, max_tokens=96):
    """打一次决策模型。返回 (obj, None) 或 (None, 失败原因)。

    `target` 是 `(base, key, model)`。key 只有走云端端点时才非空；
    本机 llama-server 不鉴权，传空即可。

    失败原因**只在这里生成**：连不上 / 超时 / 非 JSON / 非对象。判断是不是
    自己的问题（端点没起）还是模型的问题（答歪了），全靠这句。
    """
    base, key, model = target if isinstance(target, (tuple, list)) else (target, "", "")
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": q},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object", "schema": _schema_for(enum)},
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    # 只有端点自己报了模型名才传。留空交给端点挑默认模型 ——
    # 硬塞一个它不认识的名字，表现是 404 或「模型不存在」，比不传更坏。
    if model:
        payload["model"] = model
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key

    def _no(msg, retry):
        """失败的唯一出口。顺手记「换个别家能不能救」——漏标一处就等于
        那个分支永不降级，而漏标是看不见的（表现为「偶尔不降级」）。"""
        LAST_RETRYABLE[0] = retry
        return None, msg

    try:
        req = urllib.request.Request(
            _chat_url(base),
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST", headers=headers)
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        # 401/403 要单独说：这是「算力配错了」，不是「模型不会」。
        # 混成一句「HTTP 401」的话，人会去改提示词。
        if e.code in (401, 403):
            return _no("HTTP %s 鉴权失败（端点 %s）—— 算力页里这个端点的 key "
                       "没填或不对" % (e.code, base), False)
        if e.code == 404:
            return _no("HTTP 404（端点 %s）—— 地址少了或多了 /v1，"
                       "或者模型名它不认识" % base, False)
        # 429 = 这一刻不给算，5xx = 它自己坏了。两种都是「换一家可能就行」。
        return _no("HTTP %s（端点 %s）%s" % (
            e.code, base,
            " —— 限流了，换一个算力端点" if e.code == 429 else ""),
            e.code in (429, 500, 502, 503, 504))
    except urllib.error.URLError as e:
        # socket 超时会被 urllib 包成 URLError(reason="timed out")，这时说
        # 「连不上」是错的——服务活着，只是没赶上。分清楚，排查方向才对。
        r = str(getattr(e, "reason", e))
        if "timed out" in r or "timeout" in r.lower():
            return _no(_timeout_msg(timeout, base), True)
        return _no("连不上 %s（%s）——服务起了吗" % (base, r), True)
    except TimeoutError:
        return _no(_timeout_msg(timeout, base), True)
    except Exception as e:
        # 决策层挂了不拖垮对话：这是「暂时状态」，调用方按无工具面继续。
        return _no("%s: %s" % (type(e).__name__, str(e)[:80]), True)
    txt, src = _content_of(d)
    if not txt:
        # 三种空法，得分开说，不然下次排查的人还得重走一遍：
        fr = ""
        try:
            fr = d["choices"][0].get("finish_reason") or ""
        except Exception:
            pass
        if fr == "length":
            return _no("回答被 max_tokens（%d）截断了 —— 思考型模型会先烧一段"
                       "再写正文，预算给小了正文就是空的" % max_tokens, False)
        return _no("回包是空的（content / reasoning_content 都没有，"
                   "finish=%r）" % fr, False)
    try:
        o = json.loads(txt)
    except Exception:
        o = _salvage(txt, enum)
        if o is not None:
            LAST_RETRYABLE[0] = False
            return o, None
        return _no("返回的不是 JSON（来源 %s）：%r" % (src, txt[:80]), False)
    if not isinstance(o, dict):
        return _no("返回不是对象：%r" % txt[:80], False)
    LAST_RETRYABLE[0] = False
    return o, None


def _pick_name(o, enum):
    """从回答里取名字。宽容收键名，但**值必须在清单里**（幻觉名照样拒）。

    2026-10-26 的教训：schema 里明明写 `tool`，免费/小模型相当比例回成
    `{"name": ...}`。只认 `tool` ⇒ 答对了也判非法 ⇒ 工具面时灵时不灵。
    """
    name = str(o.get("tool") or o.get("name") or o.get("tool_name") or "").strip()
    return name if name in enum else None


def _domain_enum():
    return list(toolselect.DOMAINS.keys()) + ["none"]


def _domain_system(only=None):
    """第 1 级的提示：只问「哪一类」，不问具体工具。

    一级只回域，好处是**出错能定位**：域错 = 规则/类别描述有问题，
    域对工具错 = 域内工具描述重叠。混成一级只能看到一个错答案，改起来靠猜。
    `only` 收窄候选（规则并列但并集太大时，只在这几类里挑）。
    """
    keys = list(only) if only else list(toolselect.DOMAINS.keys())
    lines = "".join("- %s：%s\n" % (d, toolselect.DOMAINS[d][0]) for d in keys)
    return ("判断用户这句话该找**哪一类**工具，把类别名原样抄进 tool 字段"
            "（如 \"查\"），不要填具体工具名。\n"
            "可选类别：\n" + lines +
            "- none：不需要任何工具（闲聊、写代码、算术、翻译）\n"
            "输出形如：{\"tool\": \"查\", \"arg\": \"\"}。只输出 JSON。")


#: 最近一次决策走了哪几级、判了什么，给人排查用。决策「时灵时不灵」时
#: 光看 LAST_FAIL 只知道挂了，不知道挂在哪一级。
LAST_TRACE = []


def _decider_targets(url=None):
    """决策候选端点，第 0 个是首选，后面是后备。降级用。

    为什么要「一串」而不是「一个」（2026-10-29 实测撞出来的）：
    接上外部算力后第一件事就是 429 —— 我连跑三轮跑分，zen-gate 的免费额度
    当场用光，于是 decide() 返回 None、整个工具面消失，看起来像「她突然不会
    用工具了」。**外部算力必然有限流**，一个端点不给算 ≠ 这件事做不了。

    但**显式指定时只给一个，不降级**：跑分要考的就是那个端点，
    偷偷换成别家等于成绩作废。`AT_DECIDER_URL` 和参数都算显式。
    """
    explicit = (url or os.environ.get("AT_DECIDER_URL") or "").strip()
    if explicit:
        return [_decider_target(url)]
    try:
        from . import compute as _C
        eps, conf = _C._load()
        pinned = (conf.get("route") or {}).get("decide") or ""
        pool = [e for e in eps if e.ready()]
        pool.sort(key=_C._rank)
        if pinned:
            # 钉死的排第一，剩下的仍按档位当后备（不因为它挂了就让整件事挂）
            pool.sort(key=lambda e: 0 if e.id == pinned else 1)
        out = [(e.base_url.rstrip("/"), e.key, e.model or "") for e in pool[:2]]
        # 只留 2 个候选。3 个以上会把 DECIDE_TIMEOUT=25s 平分到每个不到 8 秒，
        # 连实测 8.6 秒的免 key 端点都跑不完 —— 多一个候选反而让每个都失败。
        # 「试两个都挂」和「试五个都超时」对用户是一样的，但后者要多等 40 秒。
        return out or [(DECIDER_URL, "", "")]
    except Exception:
        return [(DECIDER_URL, "", "")]


def decide(question, url=None, timeout=DECIDE_TIMEOUT):
    """问决策模型：该用哪个工具。返回 {"tool": ..., "arg": ...}。

    返回值约定（调用方必须区分三种）：
      · {"tool": "none"}                —— 模型说不用工具
      · {"tool": "<名字>", "arg": ...}  —— 用这个工具
      · None                            —— 决策层不可用（连不上/超时/输出非法），
                                           调用方按「无工具面」处理，不要重试

    失败时**会顺着候选端点往下试**（见 `_decider_targets`）：429 / 5xx /
    超时算「这家现在不给算」，换下一个还有救；401 / 404 / 答歪了算
    「配置或模型的问题」，换过去一样错，直接返回 None 别白烧时间。
    """
    q = (question or "").strip()
    if not q:
        return {"tool": "none", "arg": ""}
    if obvious_none(q):
        LAST_TRACE[:] = ["规则短路：这句不需要工具"]
        return {"tool": "none", "arg": ""}

    targets = _decider_targets(url)
    full = _full_mode()
    mode = "full" if full else "scoped"
    t_start = time.time()
    tried = []
    last_err = ""

    for i, target in enumerate(targets):
        base, _key, model = target
        # 缓存键必须带端点：换了算力之后，同一句话的答案可能不同（模型不同）。
        # 只按问题缓存的话，切到云端还会拿本地那条旧答案，像「切了没生效」。
        ck = (base, model or "-", mode, q)
        hit = _cache_get(ck)
        if hit is not None:
            return dict(hit)        # 复制：别让调用方改坏缓存里那条

        # 预算：平分剩下的时间，但每个至少给 5 秒（低于这个基本答不完，
        # 试了也只是把总等待拖长）。最后一个用满剩余。
        left_total = max(0.0, timeout - (time.time() - t_start))
        if i == len(targets) - 1:
            budget = max(5.0, left_total)
        else:
            budget = max(5.0, left_total / (len(targets) - i))

        out, err = (_decide_full(q, target, budget) if full
                    else _decide_scoped(q, target, budget))
        if not err:
            LAST_FAIL[0] = ""       # 成功：把上一次的失败原因清掉
            if tried:
                LAST_TRACE.append("（前 %d 个端点不可用：%s；最终由 %s 答）"
                                  % (len(tried), "、".join(tried), base))
            _cache_put(ck, out)
            return dict(out)

        last_err = err
        tried.append(base)
        # 不可重试，或已经是最后一个 → 收手
        if not LAST_RETRYABLE[0] or i == len(targets) - 1:
            _fail(err)
            return None
        LAST_TRACE.append("端点 %s 暂时不可用（%s），换下一个" % (base, err[:40]))

    _fail(last_err or "没有可用的决策端点")
    return None


def _decide_full(q, target, timeout):
    """一级制：58 个工具一次全给。给云端大模型用。"""
    enum = DECIDE_SCHEMA["properties"]["tool"]["enum"]
    LAST_TRACE[:] = ["一级制：%d 个工具一次给" % (len(enum) - 1)]
    # max_tokens 320：思考型模型会先烧一段 reasoning 再写正文，160 会被截断
    # 在思考阶段 ⇒ content 空 ⇒ 看起来像「不会」。实测（zen-gate）160 必空。
    o, err = _ask(target, DECIDE_SYSTEM, enum, q, timeout, 320)
    if err:
        return None, err
    name = _pick_name(o, enum)
    if not name:
        return None, "模型选的工具不在清单里：%r" % str(o.get("tool"))[:40]
    return {"tool": name, "arg": (o.get("arg") or "").strip()}, None


#: 合并两域之后工具数上限。超了就改为「先让模型在这两域里挑一个域」——
#: 21 个工具的 prompt ≈ 700 tokens，在这台机器上要 11s 打底，再叠上生成就顶到
#: 超时线了；而 2~3 个类别的选择只要 30 tokens，便宜得多。
MERGE_TOOL_CAP = 16


def _decide_scoped(q, target, timeout):
    """两级制：先定域（规则优先），再在域内选一个工具。

    域这一级有三种走法，按「能确定多少」递进：
      · 规则高置信 → 直接用（0 延迟，占实测 30 条里的 29 条）；
      · 规则并列   → 把并列域的工具合起来一次问完（不让模型猜域，猜错就不可挽回）；
      · 规则无话   → 才问模型「哪一类」（6 个短标签，约 100 tokens）。
    """
    t0 = time.time()
    trace = []
    doms, hits, why = toolselect.candidate_domains(q)

    if len(doms) > 1:
        merged = [t for d in doms for t in toolselect.tools_of(d)]
        trace.append("① 规则%s（%s）→ 合并 %d 个工具" %
                     (why, "、".join(doms), len(merged)))
        if len(merged) > MERGE_TOOL_CAP:
            # 合并太大就退一步：只在并列的域里问一句「哪一类」
            left = timeout - (time.time() - t0)
            o, err = _ask(target, _domain_system(doms), doms + ["none"], q,
                          max(4.0, min(8.0, left * 0.4)), 32)
            if err:
                LAST_TRACE[:] = trace + ["② 并集太大且定域失败：%s" % err]
                return None, "分域失败（%s）" % err
            one = _pick_name(o, doms + ["none"])
            if one is None:
                LAST_TRACE[:] = trace + ["② 模型回的类别不在并列域里"]
                return None, "分域失败（类别不在并列域里）"
            if one == "none":
                LAST_TRACE[:] = trace + ["② 模型判定不需要工具"]
                return {"tool": "none", "arg": ""}, None
            doms = [one]
            trace.append("② 并列域里模型选「%s」" % one)

    elif not doms:
        # 规则完全没话说，才花钱问模型
        left = timeout - (time.time() - t0)
        o, err = _ask(target, _domain_system(), _domain_enum(), q,
                      max(4.0, min(8.0, left * 0.4)), 32)
        if err:
            trace.append("① 问模型定域失败：%s" % err)
            LAST_TRACE[:] = trace
            return None, "分域失败（%s）" % err
        one = _pick_name(o, _domain_enum())
        if one is None:
            trace.append("① 模型回的类别不在清单：%r" % str(o.get("tool"))[:20])
            LAST_TRACE[:] = trace
            return None, "分域失败（类别不在清单）"
        if one == "none":
            trace.append("① 模型判定不需要工具")
            LAST_TRACE[:] = trace
            return {"tool": "none", "arg": ""}, None
        doms = [one]
        trace.append("① 问模型定域「%s」（规则%s：%s）" % (one, why, "无命中"))

    enum = [t for d in doms for t in toolselect.tools_of(d)] + ["none"]
    left = timeout - (time.time() - t0)
    # 只剩一点点时间就别发了；但门槛要小到「只因真没时间才放弃」。
    # 原来写 2.0 秒，结果调用方传 timeout=1（门禁就这么传）时**一次模型都没请求
    # 就返回 None**，看起来像「地址没生效」——那是把配置问题伪装成超时，最坏的一种。
    if left <= 0.2:
        LAST_TRACE[:] = trace + ["② 没时间了（定域已花 %.1fs）" % (time.time() - t0)]
        return None, "超时（定域就花了 %.1fs）" % (time.time() - t0)
    o, err = _ask(target, _system_with(_lines_for(doms)), enum, q, left, 96)
    if err:
        trace.append("② 域内选择失败：%s" % err)
        LAST_TRACE[:] = trace
        return None, err
    name = _pick_name(o, enum)
    trace.append("② 域「%s」内 %d 选 1 → %s" %
                 ("+".join(doms), len(enum) - 1, name or "非法"))
    LAST_TRACE[:] = trace
    if not name:
        return None, "模型选的工具不在「%s」域清单里：%r" % (
            "+".join(doms), str(o.get("tool"))[:40])
    return {"tool": name, "arg": (o.get("arg") or "").strip()}, None


# ---------------------------------------------------------------- 执行

def _clip(text, limit=MAX_RESULT_CHARS):
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + f"…（已截断，原长 {len(t)} 字符）"


def _d(result):
    """DeviceResult.as_dict() → data 字段（缺省空 dict）。"""
    if not isinstance(result, dict):
        return {}
    return result.get("data") or {}


def _render_app_launch(r, arg):
    if r.get("ok"):
        fg = _d(r).get("foreground") or ""
        extra = f"（已到前台：{fg}）" if fg else ""
        return f"已打开「{arg}」{extra}"
    return f"打开「{arg}」失败：{r.get('err') or '未知原因'}"


def _render_notify(r):
    if not r.get("ok"):
        return f"读通知失败：{r.get('err') or '未知原因'}"
    rows = _d(r).get("notifications") or []
    n = _d(r).get("count", len(rows))
    if not rows:
        return "当前没有通知。"
    lines = [f"共 {n} 条通知："]
    for i, row in enumerate(rows, 1):
        if isinstance(row, dict):
            title = row.get("title") or row.get("pkg") or ""
            text = row.get("text") or ""
            when = row.get("when") or row.get("time") or ""
            seg = f"[{i}] {title}" + (f"｜{when}" if when else "")
            if text:
                seg += f"：{text}"
        else:
            seg = f"[{i}] {row}"
        lines.append(seg)
    return "\n".join(lines)


def _render_screen(r):
    if not r.get("ok"):
        return f"读屏幕失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    fg = d.get("foreground") or ""
    labels = [x for x in (d.get("labels") or []) if x][:30]
    if not labels and not fg:
        return "屏幕上没有读到可读内容（可能是锁屏或全图片界面）。"
    lines = []
    if fg:
        lines.append(f"前台应用：{fg}")
    if labels:
        lines.append("屏幕上的文字：" + " | ".join(labels))
    return "\n".join(lines)


def _render_look(r):
    """视觉观察的渲染：说人话，别甩 JSON。

    为什么不复用 `_render_screen`：那个认的是文字树的键（foreground / labels），
    look 回的是 app / scene / texts / issues——四个键一个都对不上，
    复用会把「她看到了什么」渲染成空字符串。那是最糟的一种失败：
    看着成功了，其实一个字都没说出来。
    """
    if not r.get("ok"):
        return "看画面失败：%s" % (r.get("err") or "未知原因")
    d = _d(r)
    if d.get("caption"):
        return str(d["caption"])
    if d.get("raw") and not (d.get("scene") or d.get("app")):
        return "看是看到了，但模型没按结构回：%s" % str(d["raw"])[:300]
    lines = []
    if d.get("app"):
        lines.append("界面：%s" % d["app"])
    if d.get("scene"):
        lines.append("在做什么：%s" % d["scene"])
    texts = [x for x in (d.get("texts") or []) if x][:8]
    if texts:
        lines.append("画面上的字：" + " | ".join(str(t) for t in texts))
    if d.get("issues"):
        lines.append("注意到：%s" % d["issues"])
    if not lines:
        return "画面里没读出可说的内容。"
    el = (d.get("_meta") or {}).get("elapsed") or d.get("elapsed")
    if el:
        lines.append("（用时 %.1f 秒）" % float(el))
    return "\n".join(lines)


def _render_thermal(r):
    if not r.get("ok"):
        return f"读设备状态失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    parts = []
    b = d.get("battery") or {}
    if isinstance(b, dict) and b.get("level") is not None:
        parts.append(f"电量 {b.get('level')}%")
    h = d.get("hottest") or {}
    if isinstance(h, dict) and h.get("c") is not None:
        parts.append(f"最热部位 {h.get('name')} {h.get('c')}℃")
    if d.get("cpu_headroom_c") is not None:
        parts.append(f"距降频线 {d['cpu_headroom_c']}℃")
    u = d.get("cpu_util_pct")
    if u is not None:
        parts.append(f"CPU 利用率 {u}%")
    if d.get("thermal_status_cn"):
        parts.append(f"热状态 {d['thermal_status_cn']}")
    v = d.get("verdict") or ""
    out = "；".join([p for p in parts if p])
    if v:
        out += f"\n{v}"
    return out or "设备状态读到了，但没有可报告的字段。"


def _render_wifi(r):
    if not r.get("ok"):
        return f"扫 Wi-Fi 失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    nets = d.get("networks") or d.get("nets") or d.get("rows") or []
    if not nets:
        # 数据键名兜底：直接把 data 摘要给出去，别谎报「没有网络」
        keys = {k: v for k, v in d.items() if not isinstance(v, (dict, list))}
        return "扫描完成，但没有解析出网络列表。原始字段：" + _clip(json.dumps(
            keys, ensure_ascii=False), 300)
    lines = [f"扫到 {len(nets)} 个网络："]
    for i, n in enumerate(nets[:15], 1):
        if isinstance(n, dict):
            ssid = n.get("ssid") or "(隐藏)"
            sig = n.get("level", n.get("signal", ""))
            line = f"[{i}] {ssid}"
            if sig != "":
                line += f"（信号 {sig}）"
            if n.get("secure") or n.get("capabilities"):
                line += " 加密"
        else:
            line = f"[{i}] {n}"
        lines.append(line)
    return "\n".join(lines)


def _render_web(res):
    """web_search：复用 webtool 的搜索+正文抽取，render_block 出资料块。"""
    import webtool as WEB
    out = WEB.render_block(res, max_chars=MAX_RESULT_CHARS)
    return out


# ---------------------------------------------------------------- 通用渲染器

def _fmt_val(v, limit=200):
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (dict, list)):
        return _clip(json.dumps(v, ensure_ascii=False), limit)
    return str(v)


def _mk_kv(*fields):
    """字段型渲染器：fields = (data里的键, 中文标签[, 单位])。

    取不到就跳过（不编造），一个都取不到就**明说**并把原始字段给出去——
    静默返回「没有数据」会让模型以为设备真的空，那是谎报。
    """
    def _r(r, arg):
        if not r.get("ok"):
            return f"失败：{r.get('err') or '未知原因'}"
        d = _d(r)
        parts = []
        for f in fields:
            key, label = f[0], f[1]
            unit = f[2] if len(f) > 2 else ""
            v = d.get(key)
            if v is None or v == "":
                continue
            parts.append(f"{label} {_fmt_val(v)}{unit}")
        note = d.get("note")
        if isinstance(note, str) and note:
            parts.append(f"（注：{note}）")
        if not parts:
            return ("读到了，但没有可报告的字段。原始："
                    + _clip(json.dumps(d, ensure_ascii=False), 300))
        return "；".join(parts)
    return _r


def _mk_rows(list_key, label, limit=15, keys=()):
    """列表型渲染器：data[list_key] 是数组，逐条摘出可读字段。

    **不能假设它是数组**：实测 bt.scan 在蓝牙关着的时候 devices 是一个
    {"ok": False, "err": ...} 的字典，直接切片会 KeyError 崩掉整个工具。
    """
    def _r(r, arg):
        if not r.get("ok"):
            return f"失败：{r.get('err') or '未知原因'}"
        d = _d(r)
        rows = d.get(list_key)
        if rows is None:
            rows = []
        if isinstance(rows, dict):
            # 能力把「出错/不可用」也塞在这个键里了：把话说清楚，别当成空列表
            if rows.get("ok") is False or rows.get("err"):
                return f"{label}：不可用——{rows.get('err') or '未知原因'}"
            return f"{label}：" + _clip(json.dumps(rows, ensure_ascii=False), 400)
        if isinstance(rows, str):
            return f"{label}：{_clip(rows, 600)}"
        if not isinstance(rows, list):
            return f"{label}：{_fmt_val(rows, 300)}"
        if not rows:
            return f"{label}：0 条（空）。"
        lines = [f"{label}共 {len(rows)} 条："]
        for i, row in enumerate(rows[:limit], 1):
            if isinstance(row, dict):
                if keys:
                    picked = "｜".join(
                        f"{_fmt_val(row[k], 80)}"
                        for k in keys if row.get(k) not in (None, ""))
                    seg = picked or _fmt_val(row, 120)
                else:
                    seg = _fmt_val(row, 120)
            else:
                seg = str(row)
            lines.append(f"[{i}] {seg}")
        if len(rows) > limit:
            lines.append(f"…还有 {len(rows) - limit} 条")
        return "\n".join(lines)
    return _r


def _render_web2(r, arg):
    return _render_web(r)


def _gb(n):
    """字节 → GB。传进来的可能是字符串（能力层没统一），先试转。"""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return str(n)


def _render_sysinfo(r, arg):
    """电脑系统信息。字段名是**实测**的（pcctl.sysinfo 真返回的键）。"""
    if not r.get("ok"):
        return f"读电脑状态失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    parts = []
    if d.get("hostname"):
        parts.append(f"主机名 {d['hostname']}")
    if d.get("os"):
        parts.append(f"系统 {d['os']} {d.get('release', '')}".strip())
    if d.get("cpu_count"):
        parts.append(f"CPU {d['cpu_count']} 核")
    if d.get("mem_total") is not None:
        mem = f"内存 {_gb(d['mem_total'])}"
        if d.get("mem_percent") is not None:
            mem += f"（已用 {d['mem_percent']}%）"
        parts.append(mem)
    if d.get("disk_free") is not None:
        disk = f"磁盘可用 {_gb(d['disk_free'])}"
        if d.get("disk_total") is not None:
            disk += f"/共 {_gb(d['disk_total'])}"
        parts.append(disk)
    if d.get("user"):
        parts.append(f"用户 {d['user']}")
    return "；".join(parts) or "读到了，但没有可报告的字段。"


def _render_cell(r, arg):
    """蜂窝。subs 是数组，每条含 operator / rat / 信号。"""
    if not r.get("ok"):
        return f"读蜂窝失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    subs = d.get("subs")
    if isinstance(subs, list) and subs:
        lines = []
        for i, s in enumerate(subs[:4], 1):
            if not isinstance(s, dict):
                lines.append(f"[{i}] {s}")
                continue
            seg = f"[{i}] {s.get('operator') or '未知运营商'}"
            if s.get("rat"):
                seg += f"（{s['rat']}）"
            for k in ("signal", "level", "dbm", "band", "freq_mhz"):
                if s.get(k) not in (None, ""):
                    seg += f" {k}={s[k]}"
            lines.append(seg)
        out = "\n".join(lines)
        if isinstance(d.get("note"), str) and d["note"]:
            out += f"\n（注：{d['note']}）"
        return out
    return "没读到蜂窝信息。" + _clip(json.dumps(d, ensure_ascii=False), 200)


def _render_wifi2(r, arg):
    """Wi-Fi 扫描。真返回的键是 aps / summary / table / bands / count。"""
    if not r.get("ok"):
        return f"扫 Wi-Fi 失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    # 键名在不同设备/版本上不统一（实测有 aps，老代码写过 networks/nets/rows）
    aps = d.get("aps")
    if not aps:
        aps = d.get("networks") or d.get("nets") or d.get("rows")
    lines = []
    if d.get("count") is not None:
        lines.append(f"扫到 {d['count']} 个网络")
    bands = d.get("bands")
    if isinstance(bands, dict) and bands:
        lines.append("频段分布：" + "、".join(f"{k} {v} 个" for k, v in bands.items()))
    summary = d.get("summary")
    if isinstance(summary, dict):
        kinds = summary.get("kinds")
        if isinstance(kinds, dict) and kinds:
            lines.append("类型：" + "、".join(f"{k} {v}" for k, v in kinds.items()))
    if isinstance(aps, list) and aps:
        for i, a in enumerate(aps[:12], 1):
            if not isinstance(a, dict):
                lines.append(f"[{i}] {a}")
                continue
            seg = f"[{i}] {a.get('ssid') or '(隐藏)'}"
            for k in ("level", "signal", "freq", "band", "vendor"):
                if a.get(k) not in (None, ""):
                    seg += f"｜{k}={a[k]}"
            lines.append(seg)
        if len(aps) > 12:
            lines.append(f"…还有 {len(aps) - 12} 个")
    elif isinstance(d.get("table"), str) and d["table"].strip():
        lines.append(d["table"].strip()[:600])
    if isinstance(d.get("note"), str) and d["note"]:
        lines.append(f"（注：{d['note']}）")
    return "\n".join(lines) or "扫描完成，但没有解析出网络。"


def _render_node(r, arg):
    """编队节点状态。device 是一大段文本，截断后给，别整段塞。"""
    if not r.get("ok"):
        return f"读节点状态失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    parts = []
    if d.get("endpoint"):
        parts.append(f"服务端点 {d['endpoint']}")
    if d.get("service") is not None:
        parts.append(f"推理服务 {'在跑' if d['service'] else '没在跑'}")
    models = d.get("models")
    if isinstance(models, dict):
        ms = models.get("models")
        if isinstance(ms, list) and ms:
            names = [m.get("name", "?").rsplit("/", 1)[-1] for m in ms[:3]
                     if isinstance(m, dict)]
            parts.append("模型：" + "、".join(names))
    if isinstance(d.get("device"), str) and d["device"].strip():
        parts.append("设备摘要：" + _clip(d["device"].strip(), 300))
    return "\n".join(parts) or "读到了，但没有可报告的字段。"


def _render_tree(r, arg):
    """控件树：tree 是一整段文本，node_count 是节点数。"""
    if not r.get("ok"):
        return f"读控件树失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    out = ""
    if d.get("node_count") is not None:
        out += f"共 {d['node_count']} 个控件\n"
    if isinstance(d.get("tree"), str) and d["tree"].strip():
        out += _clip(d["tree"].strip(), 900)
    return out.strip() or "读到了，但没有可报告的字段。"


# ------------------------------------------------- 应用名 → 包名

#: 模型给的是人话（「微信」），设备要的是包名（com.tencent.mm）。
#: 这台手机拿不到系统给的应用标签（dumpsys 只给资源 ID，机器上也没有 aapt），
#: 所以这里放一张**候选包名表**：命中且不在这台机器上就跳过，继续试下一个。
#: 表是通用的（换手机也能用），不是给这一台硬编码的。
APP_ALIASES = {
    "微信": ["com.tencent.mm"],
    "wechat": ["com.tencent.mm"],
    "weixin": ["com.tencent.mm"],
    "支付宝": ["com.eg.android.AlipayGphone"],
    "alipay": ["com.eg.android.AlipayGphone"],
    "钉钉": ["com.alibaba.android.rimet"],
    "知乎": ["com.zhihu.android"],
    "微博": ["com.sina.weibo"],
    "weibo": ["com.sina.weibo"],
    "京东": ["com.jd.jrapp"],
    "京东金融": ["com.jd.jrapp"],
    "百度地图": ["com.baidu.BaiduMap"],
    "地图": ["com.baidu.BaiduMap", "com.autonavi.minimap"],
    "高德": ["com.autonavi.minimap"],
    "chrome": ["com.android.chrome"],
    "浏览器": ["com.android.chrome", "com.tencent.mtt"],
    "qq浏览器": ["com.tencent.mtt"],
    "termux": ["com.termux"],
    "wps": ["cn.wps.moffice_eng.xiaomi.lite"],
    "网易邮箱": ["com.netease.mail"],
    "邮箱": ["com.netease.mail"],
    "计算器": ["com.miui.calculator", "com.android.calculator2"],
    "天气": ["com.miui.weather2"],
    "便签": ["com.miui.notes"],
    "笔记": ["com.miui.notes", "com.orion.notein"],
    "扫描": ["com.xiaomi.scanner"],
    "小米商城": ["com.xiaomi.shop"],
    "应用商店": ["com.xiaomi.market", "com.android.vending"],
    "市场": ["com.xiaomi.market"],
    "设置": ["com.android.settings"],
    "相机": ["com.android.camera", "com.miui.camera"],
    "拍照": ["com.android.camera", "com.miui.camera"],
    "相册": ["com.miui.gallery", "com.android.gallery3d"],
    "文件管理": ["com.android.fileexplorer", "com.mi.android.globalFileexplorer",
                 "com.android.documentsui"],
    "文件": ["com.android.fileexplorer", "com.android.documentsui"],
    "混元": ["com.tencent.hunyuan.app.chat"],
    "ima": ["com.tencent.ima"],
    "王者荣耀": ["com.tencent.tmgp.sgame"],
    "音乐": ["com.miui.player", "com.tencent.qqmusic"],
    "视频": ["com.miui.video"],
    "日历": ["com.android.calendar"],
    "时钟": ["com.android.deskclock"],
    "闹钟": ["com.android.deskclock"],
    "电话": ["com.android.dialer", "com.android.contacts"],
    "拨号": ["com.android.dialer"],
}

#: 已装包名缓存。`pm list packages` 每次 ~1s，一轮对话里不该跑第二遍。
_INSTALLED = {"ts": 0.0, "pkgs": []}
_INSTALL_TTL = 300.0


def _installed(caller, ttl=_INSTALL_TTL):
    """拿这台手机上装的全部包名（含系统应用），带 5 分钟缓存。"""
    now = time.time()
    if _INSTALLED["pkgs"] and now - _INSTALLED["ts"] < ttl:
        return _INSTALLED["pkgs"]
    try:
        r = caller("apps.list", {"third_only": False})
        pkgs = (_d(r).get("packages") or []) if isinstance(r, dict) else []
    except Exception:
        pkgs = []
    if pkgs:
        _INSTALLED["ts"], _INSTALLED["pkgs"] = now, pkgs
    return _INSTALLED["pkgs"] or pkgs


def resolve_app(arg, caller):
    """「微信」→ com.tencent.mm。返回 (包名, "") 或 (None, 为什么不行)。

    失败也返回**有信息量的文字**（带上相似的候选），因为这条文字会被回填给
    模型：它得知道自己没找到，而不是以为手机里真的没有。
    """
    a = (arg or "").strip()
    if not a:
        return None, "（没说要打开哪个应用）"
    # 1. 看起来本身就是包名 / activity / intent，别多此一举去查表
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]*", a) and (
            "." in a or "/" in a or ":" in a):
        return a, ""
    pkgs = _installed(caller)
    if not pkgs:
        # 拿不到清单也要 fail-open 地试一把：包名解析失败不等于不能启动
        return a, ""
    # 2. 别名表（认得出名字，但要确认真装了）
    for cand in APP_ALIASES.get(a.lower(), []):
        if cand in pkgs:
            return cand, ""
    if a.lower() in APP_ALIASES:
        return None, (f"「{a}」没装在这台手机上"
                      f"（常是 {APP_ALIASES[a.lower()][0]}，但已装列表里没有）。")
    # 3. 包名末段正好等于它（如说「mm」指 com.tencent.mm）
    low = a.lower()
    tail = [p for p in pkgs if p.rsplit(".", 1)[-1].lower() == low]
    if len(tail) == 1:
        return tail[0], ""
    # 4. 子串唯一命中
    fuzzy = [p for p in pkgs if low in p.lower()]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if fuzzy:
        return None, (f"没确定是哪个「{a}」。手机上装的里有这几个相似的："
                      + "、".join(fuzzy[:5]))
    return None, f"没找到应用「{a}」：这台手机没装，或这个名字我认不出。"


# ---------------------------------------------------------------- 工具表（唯一事实源）

class Tool:
    """一个工具 = 一行。schema / 描述 / 权限 / 执行器全部由这张表派生。

    cap=None 表示不走注册表（web_search 走 webtool）。
    side=True 表示有副作用（会改变设备状态），权限默认 ask。
    """

    def __init__(self, name, cap, desc, perm=PERM_AUTO, side=False,
                 render=None, args=None, argkey=None, resolve=None):
        self.name = name
        self.cap = cap
        self.desc = desc
        self.perm = perm
        self.side = side
        self.render = render
        self.args = args or {}
        #: argkey：模型给的 arg 塞进能力的哪个参数（None = 这个工具不吃参数）。
        self.argkey = argkey
        #: resolve(arg, caller) -> (值, "") 或 (None, 为什么不行)。
        #: 用来把「人话」翻译成设备认的东西（如「微信」→ com.tencent.mm）。
        self.resolve = resolve

    @property
    def line(self):
        return f"- {self.name}：{self.desc}\n"


# ------------------------------------------------- 创意能力渲染器

def _render_scene(r, arg):
    """天网之眼：scene.scan 全域快照。先给一句总结，再列最强的若干实体。"""
    if not r.get("ok"):
        return f"扫环境失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    out = []
    if d.get("summary"):
        out.append(d["summary"])
    rows = d.get("rows") or []
    if rows:
        out.append(f"最强的 {min(len(rows), 12)} 个实体：")
        for i, x in enumerate(rows[:12], 1):
            name = x.get("name") or x.get("mac") or x.get("ip") or "?"
            seg = f"[{i}] {name}"
            if x.get("kind"):
                seg += f"（{x['kind']}）"
            if x.get("rssi") is not None:
                seg += f" {x['rssi']}dBm"
            if x.get("ip"):
                seg += f" {x['ip']}"
            out.append(seg)
    if isinstance(d.get("note"), str) and d["note"]:
        out.append(f"（注：{d['note']}）")
    return "\n".join(out) or "扫描完成，但没有可报告的实体。"


def _render_ir(r, arg):
    """红外发射结果。dry_run 没真发要说清，别让人以为家电已经关了。"""
    if not r.get("ok"):
        return f"红外发射失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    if d.get("dry_run"):
        return (f"已算好红外码（{d.get('protocol')}，freq={d.get('freq')}，"
                f"{d.get('pairs')} 对）；dry_run 未发射")
    return (f"已发射红外码（{d.get('protocol')}，freq={d.get('freq')}，"
            f"addr={d.get('addr')}，cmd={d.get('cmd')}）")


def _render_hotspot(r, arg):
    """热点开/关结果。把「手机切蜂窝」这种副作用说清。"""
    if not r.get("ok"):
        return f"热点操作失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    state = "已开启" if d.get("on") else "已关闭"
    seg = f"热点{state}"
    if d.get("ssid"):
        seg += f"，名称 {d['ssid']}"
    if d.get("on") and d.get("clients") is not None:
        seg += f"，已连 {d['clients']} 台"
    if isinstance(d.get("note"), str) and d["note"]:
        seg += f"（注：{d['note']}）"
    return seg


def _render_sentinel(r, arg):
    """环境哨兵：arm 给标定信息，否则只报变化（无变化就明说静默）。"""
    if not r.get("ok"):
        return f"哨兵失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    if d.get("armed"):
        return f"已标定基线，现在在场 {d.get('known')} 个设备；之后只在变化时报告。"
    if not d.get("changed"):
        return "哨兵：无变化（与基线一致）。"
    lines = [f"哨兵报告变化（在场 {d.get('known')} 个）："]
    for x in (d.get("new") or []):
        lines.append(f"＋新设备：{x.get('name') or x.get('mac') or x.get('ip') or '?'}")
    for x in (d.get("gone") or []):
        lines.append(f"－消失：{x.get('name') or x.get('mac') or x.get('ip') or '?'}")
    for x in (d.get("abnormal") or []):
        lines.append(f"！信号异常：{x.get('name') or x.get('key')} "
                     f"从 {x.get('was')} 掉到 {x.get('now')}dBm")
    return "\n".join(lines)


def _render_scene_run(r, arg):
    """场景宏执行结果：哪几步过、哪步没过（fail_fast 会断在第一步失败处）。"""
    if not r.get("ok"):
        return f"场景执行失败：{r.get('err') or '未知原因'}"
    d = _d(r)
    lines = [f"场景「{d.get('name')}」：{d.get('ran')}/{d.get('total')} 步完成"]
    if d.get("desc"):
        lines.append(d["desc"])
    for i, st in enumerate((d.get("steps") or []), 1):
        mark = "✓" if st.get("ok") else "✗"
        cap = st.get("cap") or st.get("step")
        lines.append(f"  {i}. {mark} {cap}：{st.get('note', '')}")
    return "\n".join(lines)


def _render_sense(r, arg):
    if not r.get("ok"):
        return f"传感失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    parts = [f"环境光={d.get('lux')}（等级 {d.get('light_level')}），"
             f"运动={d.get('motion_state')}，在场≈{d.get('occupancy_hint')}"]
    if d.get("battery_pct") is not None:
        parts.append(f"电量={d.get('battery_pct')}% 温度={d.get('battery_temp_c')}℃")
    return "；".join(parts)


def _render_see(r, arg):
    if not r.get("ok"):
        return f"视觉失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    if d.get("caption"):
        return f"看到：{d['caption']}"
    objs = d.get("objects") or []
    return (f"场景={d.get('scene')}｜活动={d.get('activity')}｜"
            f"物品={', '.join(objs[:6])}｜光线={d.get('light')}｜人={d.get('people')}")


def _render_life_digest(r, arg):
    if not r.get("ok"):
        return f"聚合失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    if d.get("summary"):
        return d["summary"]
    lines = [f"生活流共 {d.get('frames_total')} 帧："]
    for g in (d.get("groups") or []):
        lines.append(f"  {g['period']}：{g['frames']}帧，场景={g.get('top_scenes')}，"
                     f"活动={g.get('top_activities')}")
    return "\n".join(lines)


def _render_life_sentinel(r, arg):
    if not r.get("ok"):
        return f"生活哨兵失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    flags = d.get("flags") or []
    if not flags:
        return "生活哨兵：未检测到显著偏离"
    return "生活哨兵告警：" + "；".join(flags)


def _render_hotspot_roam(r, arg):
    if not r.get("ok"):
        return f"热点编排失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    text = d.get("text") or ""
    if d.get("deployed"):
        head = f"热点 {d.get('ssid')} 已开，客户端 {len(d.get('clients') or [])} 台："
        return head + ("\n" + text if text else "")
    if d.get("new") is not None:
        return "与基线差异：\n" + (text or "（无差异）")
    return text or "客户端清单为空（没有设备连进来）"


def _render_ir_trial(r, arg):
    if not r.get("ok"):
        return f"试码：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    if d.get("hit"):
        return (f"✅ {d.get('brand')} {d.get('device_type')} 命中入库："
                f"{d.get('protocol')} 地址={d.get('addr'):#04x} "
                f"基准={d.get('cmd_base'):#04x}。派生了 "
                f"{len(d.get('derived_keys') or [])} 个键（待逐个验证）。"
                f"以后直接说「{d.get('brand')} {d.get('device_type')} 开机」")
    if d.get("stopped"):
        return (f"试码会话已停（试了 {d.get('tried')} 个）。"
                f"未命中的码组已记档，下次同品牌自动跳过")
    if d.get("active") is False:
        return "当前没有进行中的试码会话（说「start 品牌 类型」开一轮）"
    idx, total = d.get("index", 0), d.get("total", 0)
    if idx and total:
        return (f"试码 {d.get('brand')} {d.get('device_type')}："
                f"第 {idx}/{total} 个（{d.get('current', '')}）。"
                f"看设备反应：有 → 说「有」；没有 → 说「没有」")
    return "试码：无有效输出"


def _render_prox_estimate(r, arg):
    if not r.get("ok"):
        return f"估距失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    targets = d.get("targets") or []
    if not targets:
        return "周边没扫到 AP"
    lines = ["周边 AP 距离（区间，近的在前）："]
    for t in targets[:10]:
        name = t.get("ssid") or "(隐藏)"
        lines.append(f"  {name} {t.get('rssi')}dBm → {t.get('text')}")
    if not d.get("calibrated"):
        lines.append("（未校准：默认经验参数。拿已知距离样本跑 proximity.calibrate 可更准）")
    return "\n".join(lines)


def _render_prox_track(r, arg):
    if not r.get("ok"):
        return f"跟踪失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    lines_ = d.get("lines") or []
    if not lines_:
        return "跟踪期内没采到 AP"
    head = f"热冷跟踪 {d.get('seconds')}s / {d.get('target_count')} 个目标："
    return head + "\n" + "\n".join("  " + ln for ln in lines_[:8])


def _render_prox_calib(r, arg):
    if not r.get("ok"):
        return f"校准失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    if d.get("added"):
        return (f"已记录一条样本（{d.get('samples_total')} 条）。"
                f"再记几条不同距离的，然后 calibrate run 出参数")
    if "a" in d:
        warn = "" if d.get("r2", 0) >= 0.8 else "（R²<0.8：样本可能有问题，参数仅供参考）"
        return (f"校准完成：A={d['a']}dBm n={d['n']}（R²={d['r2']}，"
                f"{d.get('samples')} 条样本）{warn}")
    return "校准：无有效输出"


def _render_net_dissect(r, arg):
    if not r.get("ok"):
        return f"流量解剖失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    lines = [f"抓了 {d.get('captured')} 个包 / {d.get('seconds')}s"]
    lines.append("协议：" + "，".join(f"{k}×{v}" for k, v in (d.get("protocols") or {}).items()))
    tk = d.get("talkers") or []
    if tk:
        lines.append("对话对：" + "，".join(f"{t['src']}↔{t['dst']}({t['pkts']})" for t in tk[:5]))
    ph = d.get("plaintext_hits") or []
    if ph:
        lines.append(f"⚠ 明文凭据疑似 {len(ph)} 处（仅自有设备排查用）")
    return "\n".join(lines)


def _render_net_survey(r, arg):
    if not r.get("ok"):
        return f"无线侦察失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    return f"bettercap 跑通：回传事件流 {d.get('raw_len')} 字节"


def _render_wallprobe(r, arg):
    if not r.get("ok"):
        return f"暴露面探测失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    probed = d.get("probed", 0)
    src = d.get("source")
    head = f"设备暴露面探测：{probed} 个目标"
    if src:
        head += f"（来源 {src}）"
    stats = d.get("stats") or {}
    if stats:
        head += " → " + "，".join(f"{k} {v}" for k, v in stats.items())
    parts = [head]
    text = d.get("text")
    if text:
        parts.append(text)
    note = d.get("note")
    if note:
        parts.append(note)
    return "\n".join(parts)


def _render_osint(r, arg):
    if not r.get("ok"):
        return f"情报查询失败：{r.get('err') or '未知'}"
    d = r.get("data") or {}
    if d.get("subdomains") is not None:
        return f"crt.sh 子域（{d.get('count')}）：" + "，".join(d["subdomains"][:12])
    if d.get("records") is not None:
        return "DNS：" + "；".join(f"{k}={v}" for k, v in d["records"].items() if v)
    if d.get("ldhName") is not None:
        return (f"RDAP：{d.get('ldhName')} 状态={d.get('status')} "
                f"NS={d.get('nameservers')} 事件={d.get('events')}")
    if d.get("matches") is not None:
        return (f"Shodan（共 {d.get('total')}）：" +
                "，".join(f"{m.get('ip')}:{m.get('ports')}" for m in d["matches"][:8]))
    return str(d)[:200]


def _render_ssh_banner(r, arg):
    if not r.get("ok"):
        return "SSH 探测失败：%s" % (r.get("err") or "未知")
    d = r.get("data") or {}
    if not d.get("is_ssh"):
        first = d.get("first_line") or "（没吐任何东西）"
        return "那个端口开着，但不是 SSH：%s" % first
    return "确认是 sshd：SSH-%s-%s" % (d.get("proto"), d.get("software"))


def _render_mqtt_probe(r, arg):
    if not r.get("ok"):
        return "MQTT：%s" % (r.get("err") or "不可用")
    d = r.get("data") or {}
    return "MQTT 握手通过（%s:%s，会话=%s）" % (
        d.get("broker"), d.get("port"),
        "有" if d.get("session_present") else "新建")


def _render_relay(r, arg):
    if not r.get("ok"):
        return "中继：%s" % (r.get("err") or "不可用")
    d = r.get("data") or {}
    devs = d.get("devices") or []
    if not devs:
        return "中继在线，但暂无手机拨入（让手机跑 phone_reverse_client.py 反连）"
    lines = ["中继在线，%d 台手机拨入：" % len(devs)]
    for dv in devs:
        caps = dv.get("caps") or []
        lines.append("· %s（%d 项能力）" % (dv.get("device_id"), len(caps)))
    return "\n".join(lines)


def _render_re_ir(r, arg):
    if not r.get("ok"):
        return "红外拆解失败：%s" % (r.get("err") or "拿不到脉冲")
    d = r.get("data") or {}
    head = "协议 %s（置信 %s）addr=%s cmd=%s" % (
        d.get("protocol"), d.get("confidence"), d.get("addr"), d.get("cmd"))
    cb = d.get("codebook") or {}
    if cb.get("known"):
        hits = "、".join("%s/%s/%s" % (h["brand"], h["device"], h["key"])
                         for h in cb["hits"][:4])
        head += "\n码库命中：" + hits
    elif cb.get("proposal"):
        head += "\n新码（人工确认后可入库）：%s" % cb["proposal"]
    hyp = d.get("hypothesis") or {}
    if hyp.get("ok"):
        head += "\n【假设】" + hyp.get("text", "")[:220]
    return head


def _render_re_ble(r, arg):
    if not r.get("ok"):
        return "BLE 拆解失败：%s" % (r.get("err") or "不是广播原文")
    d = r.get("data") or {}
    parts = []
    if d.get("name"):
        parts.append("设备名 %s" % d["name"])
    if d.get("flags"):
        parts.append("flags %s" % d["flags"])
    if d.get("uuids"):
        parts.append("UUID " + "/".join(d["uuids"][:6]))
    m = d.get("manufacturer") or {}
    if m:
        parts.append("厂商 %s（%s）" % (m.get("company"), m.get("company_id")))
    ib = d.get("ibeacon") or {}
    if ib:
        parts.append("iBeacon major=%s minor=%s" % (ib.get("major"), ib.get("minor")))
    hyp = d.get("hypothesis") or {}
    if hyp.get("ok"):
        parts.append("【假设】" + hyp.get("text", "")[:200])
    return "；".join(parts) or "拆出来了但没识别出有名有姓的字段"


def _render_re_bits(r, arg):
    if not r.get("ok"):
        return "位层拆解失败：%s" % (r.get("err") or "样本没解析出来")
    d = r.get("data") or {}
    lines = ["%d 帧 / 每帧 %d 位；位序 %s；常量位 %d（%.0f%%）%s"
             % (d.get("frames") or 0, d.get("total_bits") or 0,
                d.get("bit_order") or "?", d.get("const_bits") or 0,
                100.0 * (d.get("const_ratio") or 0),
                "" if d.get("byte_aligned") else "；**字节层拆不出，位层能**")]
    if not d.get("found"):
        lines.append("  没找到强的位域结构（退回按常量/变化分段）")
    for f in (d.get("fields") or [])[:10]:
        detail = f.get("bits") or ("值域 %d 种" % f.get("uniq", 0))
        lines.append("  bit %d +%d = %s（%s）"
                     % (f.get("bit_offset", 0), f.get("bit_len", 0),
                        f.get("label") or f.get("kind"), detail))
    for n in (d.get("notes") or [])[:2]:
        lines.append("  注意：" + n[:100])
    return "\n".join(lines)


def _render_re_bits_extract(r, arg):
    if not r.get("ok"):
        return "位段取值失败：%s" % (r.get("err") or "参数不对")
    d = r.get("data") or {}
    lines = ["位段 bit %d +%d（%s）：%d 帧里出现 %d 种值"
             % (d.get("bit_offset", 0), d.get("bit_len", 0), d.get("order"),
                d.get("frames") or 0, d.get("distinct") or 0)]
    vs = d.get("value_set") or []
    if vs:
        lines.append("  值域：%s%s" % (vs[:16], " …" if len(vs) > 16 else ""))
    rows = d.get("rows") or []
    if rows:
        lines.append("  前几帧：%s" % [[x["hex"], x["value"]] for x in rows[:8]])
    return "\n".join(lines)


def _render_re_proto(r, arg):
    if not r.get("ok"):
        return "协议拆解失败：%s" % (r.get("err") or "样本没解析出来")
    d = r.get("data") or {}
    head = d.get("head") or {}
    lines = ["%d 帧 / %d 字节一帧；帧头 %s" %
             (d.get("frames") or 0, d.get("analyzed_len") or 0,
              head.get("hex") or "无（无公共前缀）")]
    for f in (d.get("fields") or [])[:10]:
        tag = f.get("checksum") or f.get("label") or f.get("kind")
        lines.append("  +%d %d字节 %s" % (f.get("offset", 0), f.get("length", 0), tag))
    for h in (d.get("checksum_hits") or [])[:3]:
        lines.append("  校验：偏移 %d = %s（%d 字节 %s，%d 帧全中）"
                     % (h["pos"], h["algo"], h["width"], h["endian"], h["frames_ok"]))
    if not d.get("checksum_hits"):
        lines.append("  校验：没找到全帧吻合的算法（可能无校验 / 带 key / 跨帧）")
    for c in (d.get("caveats") or [])[:2]:
        lines.append("  注意：" + c[:100])
    hyp = d.get("hypothesis") or {}
    if hyp.get("ok"):
        lines.append("【假设】" + hyp.get("text", "")[:200])
    return "\n".join(lines)


def _render_re_binary(r, arg):
    if not r.get("ok"):
        return "二进制拆解失败：%s" % (r.get("err") or "文件读不了")
    d = r.get("data") or {}
    s = d.get("strings") or {}
    lines = ["%s（%s，%d 字节）" % (d.get("path"), d.get("magic") or "未知格式",
                                  d.get("size") or 0)]
    for k, label in (("url", "URL"), ("ip", "IP"), ("email", "邮箱"),
                     ("hex64", "64位hex"), ("version", "版本"), ("path", "路径")):
        if s.get(k):
            lines.append("  %s：%s" % (label, "，".join(s[k][:4])[:120]))
    hyp = d.get("hypothesis") or {}
    if hyp.get("ok"):
        lines.append("【假设】" + hyp.get("text", "")[:200])
    return "\n".join(lines)


#: 描述是**给 1.7B 看的**，写得好不好直接决定选对选错。实测（tmp/probe_scale.py）：
#: 枚举从 6 涨到 18 准确率没掉（94%→90~98%），失败**全部**是「两个工具边界重叠」
#: 而非容量不够。所以每条描述都显式点名容易混的那个邻居（见 radio_cell /
#: wifi_survey、file_pull / screenshot_describe、apps_list / app_launch）。
TOOLS = (
    Tool("web_search", None,
         "联网搜索：最新事实/价格/新闻/版本号/不知道的知识性问题。"
         "纯代码、算术、翻译、闲聊不用它。",
         render=_render_web2),

    Tool("notify_read", "notify.read",
         "读手机最近的通知（谁发消息了、几条未读）。只管通知栏，"
         "要看当前界面选 screenshot_describe。",
         render=_render_notify),

    Tool("app_launch", "app.launch",
         "打开/启动手机上的某个应用（会给应用名）。只是查看装了什么选 apps_list。",
         perm=PERM_ASK, side=True, render=_render_app_launch,
         argkey="target", resolve=resolve_app),

    Tool("screenshot_describe", "screen.read",
         "读手机屏幕上的文字（前台是哪个应用、有哪些字）。"
         "要看画面本身（图片、视频、图表、报错的图形）选 screen_look。",
         render=_render_screen),

    Tool("screen_look", "screen.look",
         "真正看一眼手机屏幕的**画面**（截图送视觉模型）。"
         "图片、视频、图表、报错弹窗的图形部分，只有它能看见；"
         "纯粹想读界面文字还是用 screenshot_describe——更快，而且不出本机。",
         perm=PERM_ASK, side=True, render=_render_look),

    Tool("device_status", "thermal.read",
         "查手机自身状态：电量、温度、存储、CPU。电脑的状态选 pc_sysinfo。",
         render=_render_thermal),

    Tool("wifi_survey", "radio.wifi.survey",
         "扫描附近的 Wi-Fi 无线网络（会明说 Wi-Fi/无线网/wlan/热点）。"
         "手机卡的「信号」不是这个，选 radio_cell。",
         render=_render_wifi2),

    Tool("radio_cell", "radio.cell",
         "查蜂窝移动网络：运营商、几 G、信号格（手机卡的信号）。"
         "问「信号」默认是这个；只有明说 Wi-Fi 才是 wifi_survey。",
         render=_render_cell),

    Tool("bt_scan", "bt.scan",
         "扫描附近的蓝牙设备（耳机、手表、音箱）。Wi-Fi 选 wifi_survey，"
         "蜂窝选 radio_cell。",
         render=_mk_rows("devices", "蓝牙设备", keys=("name", "addr", "rssi"))),

    Tool("apps_list", "apps.list",
         "列出手机上都装了哪些应用（只要清单）。要打开某个应用选 app_launch。",
         render=_mk_rows("packages", "已装应用", limit=25), args={"third_only": True}),

    Tool("app_foreground", "app.foreground",
         "查当前前台是哪个应用、用了多久（只看现在这一个）。"
         "要全部清单选 apps_list。",
         render=_mk_kv(("foreground", "前台应用"), ("screen", "界面"))),

    Tool("node_status", "phone.node.status",
         "查编队里各设备（其他手机/板子/电脑/本机）是否在线、忙不忙。"
         "问「其他设备」「哪些在线」「别的机器」都选这个。",
         render=_render_node),

    Tool("clipboard_read", "pcctl.clipboard.read",
         "读电脑剪贴板的内容（复制了什么）。手机屏幕上的字选 screenshot_describe。",
         render=_mk_kv(("text", "剪贴板"), ("preview", "内容"))),

    Tool("pc_sysinfo", "pcctl.sysinfo",
         "查电脑的系统信息：CPU、内存、磁盘、开机时长。手机的状态选 device_status。",
         render=_render_sysinfo),

    Tool("pc_proc_list", "pcctl.proc.list",
         "列出电脑上正在运行的进程/程序。",
         render=_mk_rows("procs", "进程", keys=("name", "pid"))),

    Tool("pc_windows", "pcctl.windows",
         "列出电脑上打开的窗口标题（问「开了哪些窗口」「窗口列表」就选这个）。"
         "要看屏幕画面选 pc_screenshot，手机的界面选 screenshot_describe。",
         render=_mk_rows("windows", "窗口", keys=("title", "pid"))),

    Tool("pc_screenshot", "pcctl.screenshot",
         "把电脑屏幕存成一张 PNG 文件（要「看一眼屏幕上有什么」选 pc_look；"
         "只列窗口标题选 pc_windows）。",
         render=_mk_kv(("path", "截图文件"), ("size", "尺寸"))),

    Tool("pc_look", "pcctl.look",
         "真正看一眼这台电脑屏幕的**画面**（截图送视觉模型）。"
         "「我屏幕上写了什么/有没有报错/这界面是不是卡住了」都用它；"
         "只是想存个截图文件才用 pc_screenshot。",
         perm=PERM_ASK, side=True, render=_render_look),

    Tool("net_scan", "net.scan",
         "扫描局域网里有哪些设备（IP、端口、主机名）。"
         "编队设备的在线状态选 node_status。",
         render=_mk_rows("devices", "局域网设备",
                         keys=("ip", "mac", "vendor", "hostname"))),

    Tool("artifact_list", "artifact.list",
         "列出编队产物中心里已经存了哪些东西（只要清单，不搬运）。",
         render=_mk_rows("artifacts", "产物", keys=("id", "kind", "size", "name"))),

    Tool("job_list", "job.list",
         "列出编队里的作业/任务及其状态（在跑、完成、失败）。",
         render=_mk_rows("jobs", "作业", keys=("id", "state", "cap", "owner"))),

    Tool("tree_read", "tree.read",
         "读手机当前界面的控件树：有哪些按钮、输入框及其位置。"
         "问「某某按钮/控件在哪个位置、什么坐标」选这个。"
         "只要可见文字选 screenshot_describe。",
         render=_render_tree),

    Tool("input_tap", "input.tap",
         "点击手机屏幕上某个按钮或文字（会真的点下去）。"
         "只是看看屏幕上有什么选 screenshot_describe。",
         perm=PERM_ASK, side=True, argkey="text",
         render=_mk_kv(("ok", "已点击"), ("text", "目标"), ("err", "错误"))),

    Tool("input_type", "input.type",
         "在手机当前输入框里输入文字（会真的打字）。"
         "「在搜索框里输入…」「往输入框填…」都选这个，不是 web_search。",
         perm=PERM_ASK, side=True, argkey="text",
         render=_mk_kv(("ok", "已输入"), ("len", "输入长度"), ("err", "错误"))),

    # ---- 创意能力（天网之眼 + 信号外放；计划书 2026-10-24）----
    # 决策模型只能给一个字符串参数，所以「动手+多参数」的能力拆成定向工具：
    # 热点分 on/off、哨兵分 arm/check，红外用短语「品牌 设备 键」兜底。
    Tool("scene_scan", "scene.scan",
         "天网之眼：一键给身边拍全域快照（WiFi+蜂窝+蓝牙+局域网，合并成存在感表）。"
         "问「周围有什么设备」「谁在家附近」「扫一下环境」选这个。只读，不改任何东西。",
         render=_render_scene),

    Tool("ir_command", "ir.command",
         "用手机红外遥控家电（电视/空调）。arg 写目标和动作，如「小米 电视 关」"
         "「空调 26度」——品牌不确定也能发，用户说「把空调调到二十六度」就走这个。"
         "会真发红外信号（动手级，要确认）。能不能发红外用 device_status 查。",
         perm=PERM_ASK, side=True, argkey="phrase", render=_render_ir),

    Tool("hotspot_on", "phone.hotspot",
         "开手机热点（反向供网/断网自救：别的设备经手机入网，手机自己切蜂窝）。"
         "动手级要确认；问「开热点」「手机当路由器」选这个。",
         perm=PERM_ASK, side=True, args={"on": True}, render=_render_hotspot),

    Tool("hotspot_off", "phone.hotspot",
         "关手机热点（恢复手机 WiFi 客户端模式，不再当路由器）。动手级要确认；"
         "问「关热点」「别共享网络了」选这个。",
         perm=PERM_ASK, side=True, args={"on": False}, render=_render_hotspot),

    Tool("sentinel_arm", "sentinel.watch",
         "标定环境哨兵基线（把当前在场设备记成「已知」）。之后只在有进出/信号异常时报告。",
         args={"arm": True}, render=_render_sentinel),

    Tool("sentinel_check", "sentinel.watch",
         "环境哨兵检查：对比基线，报告有没有新设备/设备消失/信号骤降。"
         "只报变化，没变就静默。问「有陌生人进来吗」「环境变了吗」选这个。",
         args={"arm": False}, render=_render_sentinel),

    Tool("scene_run", "scene.run",
         "执行一个场景宏（Jarvis 化一键，如「离家」「回家」，串起关电视/热点/哨兵等）。"
         "动手级要确认；问「离家模式」「回家了」「执行 XX 场景」选这个。",
         perm=PERM_ASK, side=True, argkey="name", render=_render_scene_run),

    # ---- 采集层（L2/L3/L5：让编队真的去「获取」，不只是分析已有信息）----
    Tool("phone_sense", "phone.sense",
         "读手机传感：环境光/运动/电量温度 → 此刻房间明暗与是否在场。"
         "问「房间亮不亮」「人在不在动」「手机热不热」选这个。只读，不改任何东西。",
         render=_render_sense),

    Tool("phone_see", "phone.see",
         "用手机摄像头拍一张，本机视觉模型(Qwen3-VL) 描述画面：你在哪、周围有什么、"
         "在做什么、光线如何。问「桌上有什么」「看看我现在在哪」「拍一张分析」选这个。"
         "只读采集（拍照=采样），不上云。",
         render=_render_see),

    Tool("life_log", "life.log",
         "拍一帧追加到「生活流」（照片事实的时序库），供之后聚合/告警。"
         "问「记一笔此刻」「存一张生活流」选这个。只读采集+本地簿记。",
         render=_render_see),

    Tool("life_digest", "life.digest",
         "聚合生活流：按日/周汇总场景/活动/光线/陪伴情况。"
         "问「这周我都在干嘛」「生活流日报」选这个。只读聚合。",
         render=_render_life_digest),

    Tool("life_sentinel", "life.sentinel",
         "生活哨兵：对比基线，报告显著偏离（如「今晚房间持续很暗」）。"
         "问「生活流有异常吗」选这个。只读，只看可观测偏离。",
         render=_render_life_sentinel),

    # ---- 距离感知（P0：从「只能报方向」到「米数 + 靠近/远离」）----
    # ---- P1 交互式逐个试码（万能遥控同款 UX）----
    # ---- P2 热点叠加编队（开热点 → 设备自己进来 → 手机侧组网视角）----
    Tool("hotspot_roam", "hotspot.roam",
         "热点叠加编队：开手机热点，等设备连进来，自动读客户端名单（IP/MAC/厂商）"
         "并 ping 探活，与基线比对报「谁新入网/谁掉线」。问「开热点让设备连进来」"
         "「现在谁连着我热点」选这个。开热点要确认；只读名单说「看客户端」即可。",
         render=_render_hotspot_roam),

    Tool("ir_trial", "ir.trial",
         "交互式逐个试码：对码库里没有的品牌/型号，系统按协议常见参数排好候选"
         "一个个发射，你站在设备旁看反应——「有反应」就记下入库，「没有」就下一个。"
         "试中的码以后直接说「格力 风扇 开机」就能用。问「帮我试试这个遥控器」"
         "「码库里没有格力」选这个。开始试要确认，会话内不重复问。",
         perm=PERM_ASK, side=True, argkey="phrase", render=_render_ir_trial),

    Tool("proximity_estimate", "proximity.estimate",
         "估距：扫周边 AP，报每个的距离区间（如「3~8 米」），近的在前。"
         "问「那个路由器离我多远」「这信号算近吗」选这个。只读。"
         "报的是区间不是精确米数——室内多径让单点误差可达一倍，这是物理限制。",
         render=_render_prox_estimate),

    Tool("proximity_track", "proximity.track",
         "热冷跟踪：拿着手机边走边采几十秒，报「正在靠近/正在远离」+ 当前距离区间。"
         "问「我是在走近还是走远」「帮我找信号最强的方向」选这个。只读。"
         "整段净变化 ≥2dBm 才判趋势，防抖。",
         render=_render_prox_track),

    Tool("proximity_calibrate", "proximity.calibrate",
         "距离校准：拿已知距离（如贴着路由器 1 米）记样本，回归出本机 A/n 参数。"
         "之后估距更准。问「校准一下距离」选这个。只读+落 ~/.fleet/。",
         render=_render_prox_calib),

    # ---- P3 设备暴露面探测下放（手机连哪个网就探哪个网）----
    Tool("wallprobe", "net.wallprobe",
         "设备暴露面探测：扫网里开了 RTSP/HTTP/ONVIF 等端口的设备，只读元数据/"
         "墙形态——拿到 SDP ≠ 拿到视频（后者要凭据），401/403 即停、绝不发 SETUP/"
         "PLAY、不带任何凭据。两种跑法：电脑侧扫本机 ARP 邻居；手机侧让桥 App 去"
         "探电脑到不了的网段（商场 WiFi/办公室/楼道）。问「这网里有什么摄像/摄像头」"
         "「帮我探一下设备暴露面」选这个。只读，红线在 fleet/wallprobe.py。",
         render=_render_wallprobe),

    Tool("net_dissect", "net.dissect",
         "流量解剖：抓一段本机流量，抽对话对/协议占比/明文凭据告警。"
         "问「这网络里在跑什么」「有没有明文口令」选这个。只读，需 scapy+Npcap。",
         render=_render_net_dissect),

    Tool("net_survey", "net.survey",
         "无线侦察：用 bettercap 发现周边 BLE/WiFi 设备。"
         "问「周围有什么无线设备」选这个。"
         "问「局域网里有哪些机器/主机」选 net_scan，不是这个。只读，需 bettercap。",
         render=_render_net_survey),

    # ---- 外部情报（L4：OSINT，拆解公开面拿数据）----
    Tool("osint_crt", "osint.crt",
         "证书透明度枚举子域（crt.sh，免 key）：拿一个域名暴露过的所有子域。"
         "问「example.com 有哪些子域」选这个。只读，仅查自有/授权域名。",
         argkey="domain", render=_render_osint),

    Tool("osint_dns", "osint.dns",
         "公共 DNS 解析（A/AAAA/MX/TXT/NS/CNAME）。问「example.com 解析到哪」选这个。只读。",
         argkey="domain", render=_render_osint),

    Tool("osint_rdap", "osint.rdap",
         "RDAP 注册信息：注册局/注册商/创建时间/NS。问「这个域名谁注册的」选这个。只读。",
         argkey="domain", render=_render_osint),

    Tool("osint_shodan", "osint.shodan",
         "Shodan 暴露面测绘（需 SHODAN_KEY，仅查自有资产）。"
         "问「我的资产开了哪些端口」选这个。只读；无 key 时如实说不可用。",
         argkey="query", render=_render_osint),
    # ---- 远程 shell / 物联网总线两条通道（只读那一半才下放；执行/发布留给人叫）----
    Tool("ssh_banner", "ssh.banner",
         "SSH 版本探测：连上去读 `SSH-2.0-xxx` 版本行，确认那个端口上跑的是不是 sshd。"
         "问「这台机器是 SSH 吗」「22 端口开的是不是 ssh」选这个。只读："
         "端口开着但不吐版本行会如实说「不是 sshd」，不冒充。只连自有/授权机器。",
         argkey="host", render=_render_ssh_banner),

    Tool("mqtt_probe", "mqtt.probe",
         "MQTT 真握手：把 CONNACK 返回码翻成人话（通过 / 未授权 / 密码错 / client id 被拒）。"
         "问「这个 broker 通不通」「IoT 总线能不能进」选这个。只读；"
         "「有服务但不让你进」和「连不上」会分开说。",
         argkey="broker", render=_render_mqtt_probe),

    # ---- 远程可触达层（L6）----
    Tool("relay_status", "relay.status",
         "家里常开中继现在连着几台手机、各上报了什么能力。问「我的手机在线吗」"
         "「现在能远程够到哪台」选这个。只读；中继未开时如实说不可用。",
         render=_render_relay),
    # ---- 逆向拆解层（不拆就没有的数据）----
    Tool("re_ir", "re.ir",
         "拆红外遥控脉冲：给一串微秒通断（或 Flipper 捕获文本），判定 NEC/三星/SIRC "
         "协议并取出地址/命令码，再对照码库。问「这串脉冲是什么码」「这个遥控器是什么"
         "协议」选这个。只读；像 scene_scan 但那是看空气，这是拆信号。",
         argkey="capture", render=_render_re_ir),
    Tool("re_ble", "re.ble",
         "拆 BLE 广播原文 hex：AD 结构遍历出设备名/服务 UUID/厂商数据/iBeacon。"
         "问「这个广播里有什么」「这是什么设备发的」选这个。只读；"
         "与 radio.ble_scan 的分工：那个去扫，这个拆扫到的原文。",
         argkey="hex", render=_render_re_ble),
    Tool("re_binary", "re.binary",
         "拆二进制/固件/APK：抽 strings 分类出 URL/IP/邮箱/疑似密钥/版本横幅/路径，"
         "认 ELF/PE/zip/dex 魔数。问「这个固件里硬编码了什么」「APK 里有什么地址」"
         "选这个。只读且只在本机；文件内容不外传。",
         argkey="path", render=_render_re_binary),
    Tool("re_proto", "re.proto",
         "拆**未知二进制协议**：给多帧样本（hex 列表，或一段含重复帧头"
         "的长流会自动切帧），推帧头、字段边界（常量/递增/步长/枚举/长度）、"
         "校验算法与位置（crc8/crc16-ccitt/crc16-modbus/sum/xor）和字节序。"
         "问「这段数据什么结构」「校验怎么算」选这个。只读；只做字节对齐，"
         "校验只报全帧吻合的算法；字段语义仍要人确认。",
         argkey="samples", render=_render_re_proto),
    Tool("re_bits", "re.bits",
         "拆**位打包**协议：字段不落在字节边界时用（传感器报文、SPI/I2C、无线遥控 "
         "常见 4 位版本+6 位长度+2 位命令塞一个字节）。给多帧 hex，"
         "按常量位切段再对变化段递归细分，出「位偏移+位宽+语义」的字段表。"
         "问「这段数据位层面什么结构」选这个。只读；没找到强证据时会明说「用字节层就够」。",
         argkey="samples", render=_render_re_bits),
    Tool("re_bits_extract", "re.bits_extract",
         "按给定位段抽出每帧的值并列值域。已知某几位是命令码/状态码时，"
         "用这个把它出现过的所有取值列出来 —— 这是「照着重写协议」的必备一步。"
         "需要 samples + bit_offset + bit_len。只读。",
         argkey="samples", render=_render_re_bits_extract),
    Tool("re_ask", "re.ask",
         "逆向问答：把拆解得到的事实交给 LLM 出人话假设。问「这说明什么」"
         "「帮我看看怎么解」选这个。回答一律标「模型回答」，可能错，"
         "关键结论要交叉验证；没额度时如实说不可用。",
         argkey="question", render=lambda r, a: (
             (r.get("data") or {}).get("answer", r.get("err") or "")[:300])),
)


# ------------------------------------------------- 从 TOOLS 派生出各处常量

def _system_with(lines):
    """拼决策系统提示。**只有这一份模板**，全量模式和域内模式都走它。

    抽出来是因为 2026-10-29 那次事故的形状：schema 里有 `tool` 字段、
    提示词里却只提 `arg`，两处各自维护、谁也不核对，模型于是把工具名塞进
    arg。同一个规矩写在两个地方，必然漂。现在文案只有一处，域内模式只换
    后半段的工具清单。
    """
    return (
        "你能操作手机和电脑上的工具。从下面的可选工具里选**一个**：\n"
        "· 把工具名原样抄进 tool 字段（如 \"device_status\"），不要改写、不要翻译；\n"
        "· 需要参数就填在 arg（搜索词/应用名/域名等），不需要就留空字符串。\n"
        "输出形如：{\"tool\": \"device_status\", \"arg\": \"\"}\n"
        "可选工具：\n" + lines +
        "纯代码、算术、翻译、闲聊一律选 none。只输出 JSON。"
    )


def _lines_for(doms):
    """只取这些域的工具说明行。复用 TOOLS 表的 line，不另抄一份描述。

    接多个域是为「规则并列」那一档：把两个域合起来让模型一次选完，
    比先让模型猜域再选工具少一次出错机会（猜错域是不可挽回的）。
    """
    if isinstance(doms, str):
        doms = [doms]
    want = set()
    for d in doms:
        want.update(toolselect.tools_of(d))
    return "".join(t.line for t in TOOLS if t.name in want) + \
        "- none：以上都不需要\n"


def _derive():
    """把 TOOLS 表展开成老代码用的四个结构。加工具只改 TOOLS。"""
    names = [t.name for t in TOOLS]
    enum = names + ["none"]
    schema = _schema_for(enum)
    lines = "".join(t.line for t in TOOLS) + "- none：以上都不需要\n"
    system = _system_with(lines)
    executors = {t.name: t for t in TOOLS}
    tiers = {t.name: t.perm for t in TOOLS}
    return schema, lines, system, executors, tiers


(DECIDE_SCHEMA, _TOOLS_LINE, DECIDE_SYSTEM,
 EXECUTORS, DEFAULT_TIERS) = _derive()


# ---------------------------------------------------------------- 权限三档

#: auto = 直接跑；ask = 交给审批闸门（无闸门即 fail-closed 拒绝）；forbid = 永不跑。
#: 与 Operit 的三档对齐，但**默认更保守**：唯一有副作用的 app_launch 默认 ask，
#: 短信/联系人/通讯录类能力压根没进工具表（隐私边界，不跟随 Operit 的激进面）。
AUDIT_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "tool_audit.log")


def load_tiers(env=None):
    """从环境变量读权限覆盖：`FLEET_TOOL_TIERS="app_launch=auto,wifi_survey=forbid"`。

    只接受已知工具名 + 已知档位；写错的项**忽略并打印**，不静默也不降级成 auto。
    """
    raw = (env if env is not None else os.environ.get("FLEET_TOOL_TIERS", ""))
    tiers = dict(DEFAULT_TIERS)
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip().lower()
        if k not in DEFAULT_TIERS or v not in (PERM_AUTO, PERM_ASK, PERM_FORBID):
            print(f"[agent_tools] 忽略非法的权限配置：{item}")
            continue
        tiers[k] = v
    return tiers


def permission_for(tool, tiers=None):
    """查工具权限档位。

    白名单有两层，**都要过**：
      1. 工具名必须在 DEFAULT_TIERS 里登记过（没登记 = 不存在 = forbid）；
      2. 档位本身必须是三档之一。
    第 1 层不能靠外部 tiers 开口子——否则「配一个没见过的工具名并给它 auto」
    就成了提权后门，而配置来源是环境变量，比代码更容易被动手脚。
    """
    if tool not in DEFAULT_TIERS:
        return PERM_FORBID
    t = (tiers or DEFAULT_TIERS).get(tool)
    return t if t in (PERM_AUTO, PERM_ASK, PERM_FORBID) else PERM_FORBID


def _audit(tool, arg, tier, outcome):
    """审计日志：谁在什么时候想用什么工具、结果如何。

    只追加、不回删；写失败就算了（审计不该拖垮对话）。
    """
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        row = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool, "arg": (arg or "")[:200], "tier": tier,
            "outcome": (outcome or "")[:300],
        }
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def execute(tool, arg, caller, tiers=None):
    """执行工具。caller(cap, args) -> DeviceResult.as_dict()。

    返回 (label, text, ok)：label 是给 SSE/日志看的短标签，
    text 是回填给模型的资料（已限长）。失败永远返回文字说明，不抛异常——
    工具失败也是「事实」，模型应该拿到它并告诉用户。

    权限在**执行层**再查一次（模型输出的任何东西都不可信，
    决策层说用什么不代表就该让它用什么）。
    """
    tier = permission_for(tool, tiers)
    if tier == PERM_FORBID:
        msg = f"工具 {tool} 当前被禁止执行（权限档位 forbid）。"
        _audit(tool, arg, tier, "denied")
        return (tool, msg, False)

    if tool == "web_search":
        q2 = (arg or "").strip()
        if not q2:
            _audit(tool, arg, tier, "no-query")
            return ("web_search", "（没给搜索词）", False)
        try:
            import webtool as WEB
            res = WEB.cached_research(q2)
            _audit(tool, q2, tier, "ok")
            return ("web_search", _render_web(res), True)
        except Exception as e:
            _audit(tool, q2, tier, f"error: {type(e).__name__}")
            return ("web_search",
                    f"搜索失败：{type(e).__name__}: {e}（暂时状态，可稍后重试）", False)

    spec = EXECUTORS.get(tool)
    if spec is None:
        _audit(tool, arg, tier, "unknown-tool")
        return (tool, f"未知工具 {tool}（内部错误）", False)
    cap, render = spec.cap, spec.render
    args = dict(spec.args)
    if spec.argkey:
        if not (arg or "").strip():
            _audit(tool, arg, tier, "no-arg")
            return (cap, f"（没说要{spec.desc[:4]}什么）", False)
        val, why = arg.strip(), ""
        if spec.resolve is not None:
            val, why = spec.resolve(val, caller)
        if val is None:
            _audit(tool, arg, tier, "unresolved")
            return (cap, why, False)
        args[spec.argkey] = val
    try:
        r = caller(cap, args)
        # 调用方给的是对象还是字典都要能干活。曾经踩过：拿到 DeviceResult
        # 对象而不转 as_dict()，每个工具都回「读到了但没有可报告的字段」——
        # 看起来像设备真的空，其实是类型不对。**静默的空比报错危险得多**。
        if hasattr(r, "as_dict"):
            r = r.as_dict()
    except Exception as e:
        _audit(tool, arg, tier, f"exception: {type(e).__name__}")
        return (cap, f"调用 {cap} 异常：{type(e).__name__}: {e}", False)
    text = _clip(render(r, arg) if render.__code__.co_argcount > 1 else render(r))
    ok = bool(r.get("ok")) if isinstance(r, dict) else False
    # ask 档 + 失败 ⇒ 是闸门没放行的正常结果，不是内部错误：文案里说清。
    _audit(tool, arg, tier, "ok" if ok else f"denied/failed: {r.get('code')}")
    return (cap, text, ok)


# ---------------------------------------------------------------- 资料块

def render_tool_block(label, text):
    """工具结果 → system 资料块。规则与 webtool 资料块一致：标题不算事实。"""
    return (
        f"\n\n【工具 {label} 的实时结果】\n"
        f"{text}\n"
        f"（以上是刚从设备/网络取得的实时数据，回答时优先引用；"
        f"结果里的应用名/标题不算事实，正文才算）"
    )


# ---------------------------------------------------------------- 多轮

#: 工具轮次硬上限。为什么是 2 而不是「跑到模型说停」：
#: BFCL V4 实测 4B 多轮 22%、本机 1.7B 多轮必崩——小模型没有「够了」的自觉，
#: 让它自己决定什么时候停 = 让它自己决定什么时候开始胡说。
MAX_ROUNDS = 2

#: 整个工具面的时间预算（秒）。超了立刻收手，用已有资料作答。
TOOL_BUDGET_S = 60.0

#: 第二轮只在「用户明说了两件事」时才问模型。
#: 这不是偷懒——是真机实测的结论（2026-10-24，1.7B，10 用例）：
#: 无条件问第二轮，**9/10 都回 none**，多轮白做，每条还白搭 3.6s。
#: 小模型没有「我还需要什么」的自觉，只有「看起来够了」的默认。
#: 所以什么时候需要第二轮，必须由**问题里的并列/顺承词**决定，不由模型决定。
_RE_COMPOUND = re.compile(
    r"顺便|并且|以及|还有|然后|接着|同时|再者|另外|外加|"
    r"再(帮我|看看|看下|看|读|扫|打开|查)|"
    r"一边.{0,8}一边|既.{0,6}又|先.{0,10}再",
)


def compound_intent(question):
    """问题里有没有「两件事」的明确信号。True 才值得问第二轮。"""
    return bool(_RE_COMPOUND.search(question or ""))


def split_compound(question):
    """复合问题 → (前半句, 后半句)。没有并列/顺承信号就返回 (原句, '')。"""
    q = (question or "").strip()
    m = _RE_COMPOUND.search(q)
    if not m:
        return q, ""
    # 后半句**从连接词开始**，不切掉它：round-1 的 decide 对「顺便告诉我电量」
    # 这种带连接词的说法本来就稳（16/16 里就有这类），切干净反而丢信息。
    return q[:m.start()].strip(" ,，。、"), q[m.start():].strip(" ,，。、")


def decide_followup(question, url=None, timeout=DECIDE_TIMEOUT):
    """第二轮决策 = **把后半句当成一条新问题，走第一轮那套枚举决策**。

    为什么不问模型「还要不要别的工具」（第一版就是这么写的，实测废了）：
    真机 1.7B 跑 10 条复合句，**10/10 回 none**——小模型没有
    「我还缺什么」的自觉，只有「看起来够了」的默认。改提示词也没用
    （把「明说两件事就必须选另一个」写进去，none 反而从 9/10 升到 10/10）。

    所以第二轮**复用已经证明过的能力形状**：把「看看通知，顺便告诉我电量」
    拆成后半句「顺便告诉我电量」，交给同一个 7 选 1 枚举决策——
    那一套实测 16/16。新形状（自问自答「够了吗」）一律不交给小模型。
    """
    _first, second = split_compound(question)
    if not second:
        return None
    return decide(second, url=url, timeout=timeout)
def run_tools(question, caller, url=None, max_rounds=MAX_ROUNDS,
              budget_s=TOOL_BUDGET_S, tiers=None, on_event=None):
    """跑完整工具面：决策 → 执行 →（必要）再决策一次 → 再执行。

    返回 [{tool, label, text, ok, round}, ...]（可能为空数组 = 不用工具）。

    缰绳三条：
      1. 轮次硬上限 MAX_ROUNDS(2)——不让模型自己决定什么时候停。
      2. 总预算 budget_s(60)——决策+执行都算在内，超了立刻收手。
      3. 同一个工具不重复选、上一轮失败就不再往下试（重试同一类没意义）。
    """
    t0 = time.time()
    out = []
    used = set()

    def fire(phase, payload=None):
        if on_event:
            try:
                on_event(phase, payload or {})
            except Exception:
                pass

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 >= budget_s:     # >= 不是 >：0 预算必须一轮都不跑
            fire("budget", {"round": rnd})
            break
        d = (decide(question, url=url) if rnd == 1
             else decide_followup(question, url=url))
        if not d:
            # 决策层不可用（连不上/超时/输出非法）：如实告知，不静默。
            # 否则「决策模型消失」在界面上表现为「什么都没发生」，极难排查。
            # 🔴 2026-10-26：带上具体原因（见 LAST_FAIL）。原来这里硬编码「连不上」，
            # 但超时误杀、模型返回非 JSON 也走同一条分支 —— 日志骗人，排查方向全错。
            fire("warn", {"msg": "工具面决策不成功，本次未调用手机工具",
                                 "reason": LAST_FAIL[0] or "未知（decide 返回空）"})
            break
        if d.get("tool") == "none":
            break
        tool = d["tool"]
        if tool in used:                       # 模型没听话 → 直接收手
            break
        used.add(tool)
        # ask 档这一步会挂起等人工确认（最长 30 秒）。不先说一句，界面上就是
        # 两分钟的死寂——看着像卡死，其实是在等人点头。先推一条，让人知道在等什么。
        if permission_for(tool, tiers) == PERM_ASK:
            fire("awaiting", {"tool": tool, "round": rnd,
                              "note": "这个工具要你确认才执行"})
        fire("searching", {"tool": tool, "round": rnd})
        label, text, ok = execute(tool, d.get("arg", ""), caller, tiers)
        out.append({"tool": tool, "label": label, "text": text,
                    "ok": bool(ok), "round": rnd})
        fire("done", {"tool": tool, "label": label, "ok": bool(ok),
                      "round": rnd})
        if not ok:
            break
        # 第二轮只在「用户明说了两件事」时才问——真机实测：
        # 无条件问，1.7B 九成回 none，多轮白做还白搭 3.6s。
        if not compound_intent(question):
            break
    return out


def render_blocks(results):
    """多个工具结果 → 一段 system 追加内容。"""
    return "".join(render_tool_block(r.get("label") or r.get("tool"),
                                     r.get("text", "")) for r in results)


# ---------------------------------------------------------------- 自测

def _selftest():
    import io
    from contextlib import redirect_stdout
    fails = []

    def check(name, cond):
        print(("OK  " if cond else "XX  ") + name)
        if not cond:
            fails.append(name)

    # 1. 规则短路
    for q in ("写一个 Python 函数反转字符串", "你好", "1+1 = ?", "把这段话翻译成英文",
              "谢谢"):
        check(f"短路 none：{q[:16]}", obvious_none(q)["tool"] if isinstance(
            obvious_none(q), dict) else obvious_none(q))
    for q in ("比特币现在多少钱", "打开微信", "谁给我发消息了", "手机烫不烫"):
        check(f"不短路：{q[:16]}", not obvious_none(q))

    # 2. decide 对不可达端点返回 None（不抛）
    t0 = time.time()
    r = decide("比特币现在多少钱", url="http://127.0.0.1:1", timeout=1.5)
    check("不可达端点 → None", r is None)
    check("不可达端点快速返回", time.time() - t0 < 5)

    # 3. 执行器渲染（fake caller）
    fake_notify = {"ok": True, "code": "ok", "data": {"count": 2, "notifications": [
        {"title": "微信", "text": "张三: 明天见", "when": "2分钟前"},
        {"title": "邮件", "text": "账单已出", "when": "1小时前"}]}}
    label, text, _ok = execute("notify_read", "", lambda cap, args: fake_notify)
    check("notify 渲染条数", "共 2 条" in text and "张三" in text)

    fake_screen = {"ok": True, "code": "ok", "data": {
        "foreground": "com.example/clock", "labels": ["10:24", "闹钟", "设置"]}}
    _, text, _ok = execute("screenshot_describe", "", lambda cap, args: fake_screen)
    check("screen 渲染前台与标签", "com.example" in text and "闹钟" in text)

    fake_thermal = {"ok": True, "code": "ok", "data": {
        "battery": {"level": 66}, "hottest": {"name": "CPU", "c": 43.2},
        "cpu_headroom_c": 41.8, "cpu_util_pct": 12, "thermal_status_cn": "正常",
        "verdict": "一切正常"}}
    _, text, _ok = execute("device_status", "", lambda cap, args: fake_thermal)
    check("thermal 渲染电量/温度", "66%" in text and "43.2℃" in text and "正常" in text)

    fake_wifi = {"ok": True, "code": "ok", "data": {"networks": [
        {"ssid": "Home-5G", "level": -52, "secure": True},
        {"ssid": "CoffeeShop", "level": -71}]}}
    _, text, _ok = execute("wifi_survey", "", lambda cap, args: fake_wifi)
    check("wifi 渲染列表", "Home-5G" in text and "CoffeeShop" in text)

    fail_call = {"ok": False, "code": "unavailable",
                 "err": "没有可用的手机序列（adb 无设备）", "data": {}}
    _, text, okf = execute("notify_read", "", lambda cap, args: fail_call)
    check("失败也返回文字不抛异常", "失败" in text and "adb" in text)

    # 4. 截断
    long_r = {"ok": True, "code": "ok", "data": {"notifications": [
        {"title": "t" * 2000, "text": "x" * 2000, "when": ""}]}}
    _, text, _ok = execute("notify_read", "", lambda cap, args: long_r)
    check("结果限长 ≤1200+尾注", len(text) <= MAX_RESULT_CHARS + 60)

    # 5. 决策缓存：同句第二次不再打模型（省 4–9s）
    import urllib.request as _ur
    orig_open = _ur.urlopen
    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

    def fake_open(req, timeout=None):
        calls.append(1)
        return _Resp(json.dumps({"choices": [{"message": {"content": json.dumps(
            {"tool": "device_status", "arg": ""})}}]}).encode())

    _ur.urlopen = fake_open
    try:
        a = decide("手机烫不烫", url="http://fake:1")
        b = decide("手机烫不烫", url="http://fake:1")
    finally:
        _ur.urlopen = orig_open
    check("决策缓存命中", bool(a) and a == b and a.get("tool") == "device_status")
    check("同句只打一次模型", len(calls) == 1)

    # 5. 资料块
    blk = render_tool_block("notify.read", "共 1 条通知：…")
    check("资料块含来源与规则", "工具 notify.read 的实时结果" in blk and "标题不算事实" in blk)

    # 6. app_launch 参数进白名单字段
    seen = {}
    def cap_recorder(cap, args):
        seen.update({"cap": cap, "args": args})
        return {"ok": True, "code": "ok", "data": {"foreground": "com.tencent.mm"}}
    _, text, _ok = execute("app_launch", "微信", cap_recorder)
    check("app_launch 参数映射 target", seen == {"cap": "app.launch",
                                                 "args": {"target": "微信"}}
          and "com.tencent.mm" in text)

    # 7. 权限三档
    check("未知工具 → forbid（白名单制）",
          permission_for("shell_exec") == PERM_FORBID)
    check("app_launch 默认 ask", permission_for("app_launch") == PERM_ASK)
    check("只读工具默认 auto", permission_for("notify_read") == PERM_AUTO)
    _, tdeny, okdeny = execute("shell_exec", "rm -rf /",
                               lambda cap, args: {"ok": True, "code": "ok"})
    check("forbid 工具不执行且给文字", (not okdeny) and "forbid" in tdeny)
    _, _, ok2 = execute("notify_read", "", lambda cap, args: fake_notify,
                        tiers={"notify_read": PERM_FORBID})
    check("外部 tiers 可覆盖为 forbid", not ok2)

    # 8. 多轮：轮次上限 / 失败不再往下 / 同工具不重复 / 复合句才问第二轮
    class _Scripted:
        """假装是决策模型：按脚本返回（第二轮走 decide_followup 间接调到这里）。"""
        def __init__(self, script):
            self.script = list(script)
            self.seen = []
        def __call__(self, question, url=None, timeout=None):
            self.seen.append(question)
            return self.script.pop(0) if self.script else {"tool": "none"}

    _g = globals()          # 跑成脚本时没有 fleet 包可 import，直接改模块全局
    orig_decide = _g['decide']
    try:
        # (a) 复合句 → 两轮各拿一个工具
        sd = _Scripted([{"tool": "notify_read", "arg": ""},
                        {"tool": "device_status", "arg": ""}])
        _g['decide'] = sd
        got = run_tools("看看通知，顺便告诉我电量",
                        lambda cap, args: fake_notify
                        if cap == "notify.read" else fake_thermal)
        check("复合句两轮各拿一个工具", len(got) == 2 and
              got[0]["round"] == 1 and got[1]["round"] == 2)
        check("第二轮只把后半句喂给决策（不是整段对话史）",
              len(sd.seen) == 2 and "顺便" in sd.seen[1] and
              "看看通知" not in sd.seen[1])

        # (b) 单句 → 不问第二轮（真机实测：问了也是 none，白搭 3.6s）
        sd2 = _Scripted([{"tool": "notify_read", "arg": ""}])
        _g['decide'] = sd2
        run_tools("看看通知", lambda cap, args: fake_notify)
        check("单句不问第二轮", len(sd2.seen) == 1)

        # (c) 首轮失败即止
        sd3 = _Scripted([{"tool": "notify_read", "arg": ""}])
        _g['decide'] = sd3
        got2 = run_tools("看看通知", lambda cap, args: fail_call)
        check("首轮失败就不再试第二轮", len(got2) == 1 and not got2[0]["ok"])

        # (d) 模型重复要同一个工具 → 收手
        sd4 = _Scripted([{"tool": "notify_read", "arg": ""},
                         {"tool": "notify_read", "arg": ""}])
        _g['decide'] = sd4
        got3 = run_tools("看看通知，再读一遍通知",
                         lambda cap, args: fake_notify, max_rounds=5)
        check("同一工具不被重复执行", len(got3) == 1)
    finally:
        _g['decide'] = orig_decide

    # 9. 预算：超了立刻收手
    check("预算常量合理", 0 < TOOL_BUDGET_S <= 120 and MAX_ROUNDS == 2)

    print(f"\n{'FAIL ' + str(len(fails)) if fails else 'ALL PASS'}: {fails or ''}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
