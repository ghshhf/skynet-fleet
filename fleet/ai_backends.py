"""免费 AI 后端：按协议分类，不按厂商（学 Cherry 的 provider 四元组）。

一个后端 = (id, protocol, base_url, model, key_env)。

为什么按协议分类：pollinations 与本地 llama.cpp/Ollama 是「同一个协议类」的
两个实例——都是 OpenAI 兼容的 /v1/chat/completions。所以一个调用器吃遍所有，
不必为每家厂商写一套。这就是 Cherry 把 provider 按协议聚类的精髓。

免 key 当一等公民（Cherry 也内置了免费入口）：pollinations 默认就在，零配置开箱即用。
本地模型（openai 兼容端点）有 FLEET_LLM_URL 才活；llm7 有 FLEET_LLM7_TOKEN 才活。
外部 token（FLEET_LLM_KEY / FLEET_LLM7_TOKEN）永远从环境变量读，绝不下落盘、绝不进注册表。
本地模型若端点要鉴权读 FLEET_LLM_KEY（可选）；model 留空则首次对话时自动从 /models 取第一个。

探测契约（学 Cherry 的「检测」按钮 = probe）：ai.status 逐个后端真发一条请求，
不通就报原因，而不是默默把能力从列表里消失。
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request
import configparser
from pathlib import Path

from . import metrics as M   # 零依赖可观测性（请求/失败/冷却/降级/延迟/健康）

_CUSTOM = []   # 来自仓库根 local.env [ai_custom] 的自定义后端（gitignored，只本机）


def _load_local_env():
    """读仓库根 local.env（gitignored），把 [ai] 段注入环境变量、[ai_custom] 段
    解析成自定义后端。这是『落地但不外泄』——用户给的免费 token 写进本机配置，
    不进仓库，也不用手工每回设环境变量。"""
    p = Path(__file__).resolve().parent.parent / "local.env"
    if not p.exists():
        return
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(p, encoding="utf-8")
    except Exception:
        return
    if cp.has_section("ai"):
        for k in ("FLEET_LLM_URL", "FLEET_LLM_KEY", "FLEET_LLM_MODEL",
                  "FLEET_LLM7_TOKEN", "FLEET_LLM7_MODEL", "FLEET_OVH_MODEL"):
            if cp.has_option("ai", k) and not os.environ.get(k):
                v = cp.get("ai", k).strip()
                if v:
                    os.environ[k] = v
    if cp.has_section("ai_custom"):
        groups = {}
        for key, val in cp.items("ai_custom"):
            name, _, field = key.partition(".")
            groups.setdefault(name, {})[field] = val.strip()
        for name, g in groups.items():
            base = g.get("base_url")
            if not base:
                continue
            _CUSTOM.append({
                "id": name, "title": "自定义 " + name,
                "protocol": "openai", "base_url": base.rstrip("/"),
                "model": g.get("model") or None,
                "key_env": None, "need_key": bool(g.get("key")),
                "key": g.get("key") or None,
            })


_load_local_env()


def backends():
    """当前配置下可用的后端清单（只读环境变量，无网络）。"""
    bs = []
    # ① pollinations：真·免 key，实测 0.6s 回话，chat 接口也通
    bs.append({
        "id": "pollinations", "title": "Pollinations（免key）",
        "protocol": "openai", "base_url": "https://text.pollinations.ai/v1",
        "model": "openai", "key_env": None, "need_key": False,
    })
    # ② 本地模型：openai 兼容端点（llama.cpp / Ollama / vLLM / LM Studio / 本地聚合网关）
    #    有 FLEET_LLM_URL 才活；若端点要鉴权再读 FLEET_LLM_KEY（可选，绝不落盘）；
    #    model 留空则首次对话时自动从 /models 取第一个（见 _resolve_model）。
    url = os.environ.get("FLEET_LLM_URL")
    if url:
        key = os.environ.get("FLEET_LLM_KEY")
        bs.append({
            "id": "local", "title": "本地模型 " + url,
            "protocol": "openai", "base_url": url.rstrip("/"),
            "model": os.environ.get("FLEET_LLM_MODEL") or None,
            "key_env": "FLEET_LLM_KEY" if key else None,
            "need_key": bool(key), "key": key,
        })
    # ③ OVHcloud AI Endpoints：真·免 key 的第二个端点（pollinations 的替补）
    #    实测 2026-10-21：/v1/models 免 key 可列 22 个模型；匿名 chat 200 成功。
    #    限流 2 req/min per IP per model——是「保命符」不是主力。
    #    model 默认 gpt-oss-20b：实测 Qwen3.5-9B / Qwen3.6-27B 匿名调用是推理模型，
    #    600 预算被 thinking 烧光 → finish_reason=length 且正文空；gpt-oss-20b 正常收尾。
    bs.append({
        "id": "ovh", "title": "OVHcloud（免key·低频兜底）",
        "protocol": "openai",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "model": os.environ.get("FLEET_OVH_MODEL") or "gpt-oss-20b",
        "key_env": None, "need_key": False,
    })
    # ④ llm7：要 token 才活。
    #    🔴 model 原来硬编码 "gpt-4o-mini"——该模型已不在 llm7 当前的 70 个模型里
    #    （2026-10-21 实测命中 false），调必 404。改成 turbo 档的 gpt-oss:20b。
    tok = os.environ.get("FLEET_LLM7_TOKEN")
    if tok:
        bs.append({
            "id": "llm7", "title": "llm7（token）",
            "protocol": "openai", "base_url": "https://api.llm7.io/v1",
            "model": os.environ.get("FLEET_LLM7_MODEL") or "gpt-oss:20b",
            "key_env": "FLEET_LLM7_TOKEN",
            "need_key": True, "key": tok,
        })
    # ⑤ 自定义端点：来自 local.env [ai_custom]（用户给的免费 token 落地处）
    for c in _CUSTOM:
        bs.append(c)
    return bs


def _headers(b):
    h = {"Content-Type": "application/json", "User-Agent": "skynet-fleet"}
    if b.get("need_key") and b.get("key"):
        h["Authorization"] = "Bearer " + b["key"]
    return h


_MODEL_CACHE = {}      # base_url -> 自动挑到的 model（避免每次对话都打 /models）


def _resolve_model(b):
    """model 留空时自动从 /models 取第一个；按 base_url 缓存一次。"""
    if b.get("model"):
        return b["model"]
    base = b["base_url"].rstrip("/")
    if base in _MODEL_CACHE:
        return _MODEL_CACHE[base]
    try:
        req = urllib.request.Request(base + "/models",
                                     headers=_headers(b), method="GET")
        obj = json.loads(urllib.request.urlopen(req, timeout=10).read())
        models = [m.get("id") for m in obj.get("data", []) if m.get("id")]
        pick = models[0] if models else "local-model"
    except Exception:
        pick = "local-model"
    _MODEL_CACHE[base] = pick
    return pick


def probe(b, timeout=8):
    """真发一条请求验连通（学 Cherry 的「检测」按钮 = probe 契约）。

    先试 /models（GET）；有些端点不让裸 GET，退化成一次极小 chat 探活。
    """
    try:
        req = urllib.request.Request(
            b["base_url"].rstrip("/") + "/models",
            headers=_headers(b), method="GET")
        urllib.request.urlopen(req, timeout=timeout).read()
        return True, ""
    except Exception:
        try:
            _chat_once(b, "ping", max_tokens=1, timeout=timeout)
            return True, ""
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


def chat(b, prompt, system="", on_token=None, max_tokens=2048, timeout=120):
    """流式对话：逐 token 调 on_token(text)；返回完整文本。

    SSE 解析做了容错（Cherry 专门 patch 过 SDK 来容忍残缺流片段）——遇到
    解析不出的行就跳过，绝不整段丢；[DONE] 正常收尾。

    timeout 默认 120s（本地网关 + 512 max_tokens 时 60s 偏紧，实测撞过）。
    流中途超时不会丢已收到的内容——见 _chat_stream。

    🔴 max_tokens 默认 2048：推理模型会先烧一段 token 思考再吐正文，
    预算给小了会 finish_reason=length 且正文 0 字符（实测 300 必空、1024 起正常）。
    """
    return _chat_stream(b, prompt, system, on_token, max_tokens, timeout)


def _chat_once(b, prompt, max_tokens=1, timeout=8):
    return _chat_stream(b, prompt, "", None, max_tokens, timeout)


def _chat_stream(b, prompt, system, on_token, max_tokens, timeout):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": _resolve_model(b), "messages": msgs,
               "stream": True, "max_tokens": max_tokens}
    req = urllib.request.Request(
        b["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(b), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {e.code}: {body}")
    full = []
    finish = None
    # 🔴 流中途超时/断开：**已经收到的内容不许丢**。上游偶发卡死（实测本地网关
    # 8 次里撞到 1 次 timeout），原实现让异常直接冲出函数，full 里的半截回答
    # 全部作废，用户只看到一句「出错」——其实答案已经写了一半。
    stalled = None
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue                        # 容错：残缺片段跳过
            # 有的端点末尾发一个 choices:[] 的 usage chunk —— 跳过，别越界
            choices = obj.get("choices") or []
            if not choices:
                continue
            fr = choices[0].get("finish_reason")
            if fr:
                finish = fr
            # 只取正文 content；reasoning 是模型内部思考，不进答案流
            delta = (choices[0].get("delta") or {}).get("content")
            if delta:
                full.append(delta)
                if on_token:
                    on_token(delta)
    except (socket.timeout, TimeoutError) as e:
        stalled = f"读取超时（{timeout}s 内上游没再发数据）: {e}"
    except urllib.error.URLError as e:
        stalled = f"连接中断: {e}"
    except OSError as e:
        stalled = f"连接中断: {e}"
    finally:
        try:
            resp.close()
        except Exception:
            pass
    text = "".join(full)
    if stalled:
        if text.strip():
            # 有半截答案：给出去，并在末尾标一句「上游卡住了」——半截真话比空话有用
            return text + f"\n\n[流被中断] {stalled}（以上是中断前收到的内容）"
        # 一个字都没有：这不是「模型没话说」，是链路死了——必须报原因，不能交空串
        raise RuntimeError(f"{stalled}；上游一个字都没给")
    if not text.strip() and finish == "length":
        # 🔴 空回的真因（2026-10-21 实测抓到的）：**推理模型把 max_tokens 预算
        # 全烧在 reasoning 上了，正文一个字没吐就被截断**。
        # 证据：minimax-m3.1-flash 带 system prompt 时 max_tokens=300 → finish_reason=length
        # 且正文 0 字符；同样参数 1024/2048 → finish_reason=stop，正文正常。
        # 所以「空回」不是上游抽风，是**预算不够**——这时给一句「上游临时抽风」是
        # 误导，会把人引到错误的排查方向上（重试没用，加预算才有用）。
        raise RuntimeError(
            f"预算被推理烧光：max_tokens={max_tokens} 不够这个推理模型思考，"
            f"正文被截断（finish_reason=length）。加大 max_tokens 再试"
            f"（本项目建议 ≥1024）。")
    return text


# =================== 失败分类 + 冷却 + 健康门控 + 降级链 ===================
# 学 dsh-our-free-model（src/route/classify.js + src/combos.js）：
# 免费端点会死/限流/地区拦截/偶发抽风。不能「单押一条」——一条挂了要透明切到下一条，
# 且挂掉的端点要被「踢出轮转」一段时间（冷却），别反复硬砸把限流/封禁坐实。
# 本地模型（FLEET_LLM_URL）只在 fallback 链垫底：先走云端免费、本地不做主力。

# 冷却缓存：backend id -> {"until": 绝对秒, "level": 退避级数}
_COOLDOWN = {}
# round-robin 游标（等效模型分摊负载，学 dsh combos round-robin）
_RR = {"cursor": 0}

FAIL_TRANSIENT = "transient"   # 临时抖动：超时/断网/5xx → 可重试 + 短冷却
FAIL_RATE = "rate_limit"       # 限流 429/quota → 可重试 + 指数退避
FAIL_AUTH = "auth"             # 401/403/404 → 记死，踢出轮转
FAIL_REQUEST = "request"       # 未匹配 4xx → 请求本身的问题，不冷却（但跨端点仍可试）
FAIL_ABORTED = "aborted"       # 用户取消 → 不重试

_COOLDOWN_SHORT = 30    # 秒：transient 默认
_COOLDOWN_LONG = 2 * 60  # 秒：auth 类记死时长
_BACKOFF_BASE = 2        # 秒：rate_limit 指数退避基数
_BACKOFF_MAX = 5 * 60    # 秒：退避上限


def classify_failure(exc):
    """把一个异常归一成 {kind, retryable, cooldown, bench, advance}。

    - bench=True  → 该端点踢出轮转 cooldown 秒（auth 记死，transient 短冷却）
    - advance=True → 降级链可以切到下一条候选（跨端点重试同一条请求有意义）
    未匹配 4xx 不冷却（是请求的问题，别误伤端点），但仍 advance（换端点可能就通了）。
    """
    msg = ""
    status = None
    if isinstance(exc, urllib.error.HTTPError):
        status = getattr(exc, "code", None)
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        msg = f"{getattr(exc, 'reason', '')} {body}"
    else:
        msg = str(exc)
    low = msg.lower()

    # 1) 限流/配额/容量（文字优先于状态码，学 dsh：wording beats status）
    if any(k in low for k in ("rate limit", "too many requests", "quota",
                              "capacity", "overloaded")):
        return {"kind": FAIL_RATE, "retryable": True, "cooldown": _BACKOFF_BASE,
                "bench": True, "advance": True}
    # 2) 状态码
    if status == 429:
        return {"kind": FAIL_RATE, "retryable": True, "cooldown": _BACKOFF_BASE,
                "bench": True, "advance": True}
    if status in (401, 403, 404):
        return {"kind": FAIL_AUTH, "retryable": False, "cooldown": _COOLDOWN_LONG,
                "bench": True, "advance": True}
    if status in (408, 425):
        return {"kind": FAIL_TRANSIENT, "retryable": True, "cooldown": _COOLDOWN_SHORT,
                "bench": True, "advance": True}
    if status and 500 <= status <= 599:
        return {"kind": FAIL_TRANSIENT, "retryable": True, "cooldown": _COOLDOWN_SHORT,
                "bench": True, "advance": True}
    # 3) 未匹配 4xx：请求本身的问题，不冷却，但跨端点仍可试下一条
    if status and 400 <= status < 500:
        return {"kind": FAIL_REQUEST, "retryable": False, "cooldown": 0,
                "bench": False, "advance": True}
    # 4) 超时/断网/其他 → 临时抖动
    return {"kind": FAIL_TRANSIENT, "retryable": True, "cooldown": _COOLDOWN_SHORT,
            "bench": True, "advance": True}


def mark_failure(bid, verdict):
    """按 verdict 给端点记冷却（rate_limit 用指数退避，级数累加）。"""
    if not verdict.get("bench"):
        return
    cd = verdict.get("cooldown", 0)
    if cd <= 0:
        return
    st = _COOLDOWN.get(bid, {"until": 0, "level": 0})
    if verdict.get("kind") == FAIL_RATE:
        level = min(st.get("level", 0) + 1, 15)
        cd = min(_BACKOFF_BASE * (2 ** (level - 1)), _BACKOFF_MAX)
    else:
        level = 0
    _COOLDOWN[bid] = {"until": time.time() + cd, "level": level}
    # 可观测性：每次进入冷却都记一笔（按后端与触发类型），看限流/封禁坐实频率
    M.ai_cooldown_total.inc({"backend": bid, "kind": verdict.get("kind", "")})


def mark_success(bid):
    """成功一次就清冷却（端点活了）。"""
    _COOLDOWN.pop(bid, None)


def cooldown_remaining(bid):
    st = _COOLDOWN.get(bid)
    if not st:
        return 0
    return max(0, int(st["until"] - time.time()))


def is_healthy(b):
    """端点是否可进轮转（冷却期内视为不健康）。"""
    return cooldown_remaining(b["id"]) <= 0


def _fallback_order():
    """降级链优先级：免 key 在前、token 其次、本地模型（仅测试）垫底。
    对应「先走云端免费、本地不做主力」的定调。"""
    rank = {"pollinations": 0, "ovh": 1, "llm7": 2}
    bs = backends()

    def key(b):
        if b["id"] == "local":
            return 99
        if b["id"] in rank:
            return rank[b["id"]]
        if str(b.get("id", "")).startswith("custom"):
            return 3
        return 5
    return sorted(bs, key=key)


def healthy_backends():
    """backends() 里健康（未在冷却）的子集，按 fallback 优先级排。

    顺便刷新可观测性瞬时量：健康数 / 冷却中数——每调一次就对齐一次。
    """
    all_b = _fallback_order()
    bs = [b for b in all_b if is_healthy(b)]
    M.ai_healthy_backends.set(len(bs))
    M.ai_cooldown_active.set(len(all_b) - len(bs))
    return bs


def chat_fallback(prompt, system="", on_token=None, max_tokens=2048, timeout=120,
                  candidates=None, strategy="fallback"):
    """降级链（学 dsh combos fallback）：依次试候选端点，第一个产出非空白正文的胜出，
    切换对调用方不可见（只在 via 字段暴露实际用了谁）。全部失败则抛出带每条原因的 RuntimeError。

    candidates: 候选端点列表（backend dict）。None = 用 healthy_backends()（云端免费优先）。
    strategy:
      "fallback"     — 固定从优先级最高的健康端点开始（质量阶梯：免费 key 优先）
      "round-robin" — 从轮转游标处开始，等效模型分摊负载（解决「全挤一个端点抢额度」）
    """
    if candidates is None:
        candidates = healthy_backends()
    if not candidates:
        raise RuntimeError("没有可用后端（全部冷却中或不存在）")

    if strategy == "round-robin" and len(candidates) > 1:
        idx = _RR["cursor"] % len(candidates)
        candidates = candidates[idx:] + candidates[:idx]
        _RR["cursor"] = (idx + 1) % len(candidates)

    last_err = None
    for i, b in enumerate(candidates):
        t0 = time.time()
        try:
            text = _chat_stream(b, prompt, system, on_token, max_tokens, timeout)
            dt = time.time() - t0
            if text and text.strip():
                mark_success(b["id"])
                M.ai_request_total.inc({"backend": b["id"], "result": "ok"})
                M.ai_latency.observe(dt, {"backend": b["id"]})
                if i > 0:
                    M.ai_fallback_total.inc()     # 切到第 i 条才成功 ⇒ 记一次降级
                return text, b["id"]
            # 空回：算这次失败（上游抽风/推理烧光），冷却 + 试下一条
            mark_failure(b["id"], {"kind": FAIL_TRANSIENT, "bench": True,
                                   "cooldown": _COOLDOWN_SHORT})
            M.ai_request_total.inc({"backend": b["id"], "result": "empty"})
            M.ai_failure_total.inc({"backend": b["id"], "kind": "empty"})
            M.ai_latency.observe(dt, {"backend": b["id"]})
            last_err = f"{b['id']} 空回（上游无正文）"
        except Exception as e:
            v = classify_failure(e)
            mark_failure(b["id"], v)
            M.ai_request_total.inc({"backend": b["id"], "result": "err"})
            M.ai_failure_total.inc({"backend": b["id"], "kind": v["kind"]})
            M.ai_latency.observe(time.time() - t0, {"backend": b["id"]})
            last_err = f"{b['id']} [{v['kind']}] {e}"
            if not v["advance"]:
                raise RuntimeError(last_err)
    raise RuntimeError("全部后端失败：" + " → ".join(b["id"] for b in candidates)
                       + f" ；末因：{last_err}")

