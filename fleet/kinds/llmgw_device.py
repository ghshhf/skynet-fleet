#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""天网 · LLM 网关自有协议栈（llmgw.*）

来源（MIT，可合法融入）：
  - OFM service：`D:/Our Free Model/service`（Ebony-Vinyl，MIT），源码本机
  - zen-gate：`D:/code/vendor-src/zen-gate`（LAGcomcom，MIT），Go 源码
  - FreeLLMAPI：`D:/code/vendor-src/freellmapi`（tashfeenahmed，MIT）——
    学习其设计模式：匿名端点聚合、per-key cooldown 升级阶梯、model-level
    failure benching、bandit 路由评分、错误体自学习限制、cooldown-probe
    早期恢复、hedge 并发、X-Routed-Via 可溯源、状态持久化
  - Free-GPT4-WEB-API：`D:/code/vendor-src/Free-GPT4-WEB-API`（aledipa，基于 g4f）——
    provider 健康监控 + 多层 fallback，错误体分类（401/403/429/5xx/timeout/context_too_large/transport）

本模块不依赖上面三者运行时，只复用它们的协议/设计，用 Python 标准库
实现一个 OpenAI 兼容客户端 + 路由器，自动发现 OFM/zen-gate 本地 key，
并聚合匿名端点（pollinations/llm7/kilo/OVH），支持跨网关·跨模型 fallback、
配额追踪、cooldown、粘性会话、家族路由。

能力（7 项）：
  llmgw.endpoints   — 列出所有可用网关（OFM/zen-gate/匿名）+ 健康探测
  llmgw.models      — 按网关列出模型清单（含 family 分类）
  llmgw.chat        — OpenAI 兼容 chat，跨网关·跨模型 fallback + cooldown + bandit 路由 + hedge 并发
  llmgw.embeddings  — embeddings，家族内 failover（不同模型向量不混）
  llmgw.probe       — 单模型首字延迟（TTFB）
  llmgw.cooldowns   — 查看当前冷却中的端点/模型（带 source 标记）
  llmgw.sticky      — 查看/清除粘性会话
"""
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core import BaseDevice, DeviceResult as R
from ..vendored_llmgw import LLMGW_CAPABILITIES

# ---------- 路径 / 编码 ----------
try:
    sys_stdout = __import__("sys").stdout
    if hasattr(sys_stdout, "reconfigure"):
        sys_stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    sys_stderr = __import__("sys").stderr
    if hasattr(sys_stderr, "reconfigure"):
        sys_stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- 端点发现 ----------
OFM_SETTINGS = os.path.join(os.path.expanduser("~"), ".our-free-model", "settings.json")
ZENGATE_CONFIG = os.path.join(os.environ.get("APPDATA") or "", "zen-gate", "config.json")

# 匿名端点（FreeLLMAPI 设计：no key，直接打）
ANON_ENDPOINTS = [
    {"name": "pollinations", "base": "https://text.pollinations.ai/openai/v1",
     "key": "", "anon": True, "default_model": "openai-fast",
     "note": "GPT-OSS 20B on OVH，匿名可用"},
    {"name": "llm7", "base": "https://api.llm7.io/v1",
     "key": "", "anon": True, "default_model": "gpt-oss-20b",
     "note": "100 req/hr 匿名，聚合多上游"},
    {"name": "kilo", "base": "https://api.kilo.ai/api/gateway/v1",
     "key": "", "anon": True, "default_model": None,
     "note": "kilo :free 路由，匿名可用"},
    {"name": "ovh", "base": "https://api.ai.endpoints.ovh.net/v1",
     "key": "", "anon": True, "default_model": "Qwen3-235B-A22B",
     "note": "OVH AI Endpoints，匿名可用"},
]


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def discover_gateways():
    gws = []
    # OFM
    s = _load_json(OFM_SETTINGS)
    if s and s.get("forwardKey"):
        gws.append({"name": "ofm", "base": "http://127.0.0.1:18900",
                    "key": s["forwardKey"], "anon": False})
    # zen-gate
    c = _load_json(ZENGATE_CONFIG)
    if c and (c.get("mainKey") or c.get("MainKey")):
        key = c.get("mainKey") or c.get("MainKey")
        gws.append({"name": "zen-gate", "base": "http://127.0.0.1:8787",
                    "key": key, "anon": False})
    # 匿名端点（永远可用，不需 key）
    for a in ANON_ENDPOINTS:
        gws.append({"name": a["name"], "base": a["base"],
                    "key": a["key"], "anon": True,
                    "default_model": a.get("default_model"),
                    "note": a.get("note", "")})
    return gws


# =====================================================================
# 配额追踪 / cooldown / bandit / model-level bench（FreeLLMAPI 模式）
# =====================================================================

_LOCK = threading.RLock()
_NOW = time.time

# per-key cooldown 状态：(gw, model) -> {"until": ts, "source": "heuristic|authoritative|credit|tier", "hits": [ts,...], "set_at": ts}
_COOLDOWN = {}
# model-level failure bench：model -> [ts, ...]（滑动窗口内的失败时间戳）
_MODEL_FAILURES = {}
# 模型级冷却：model -> 解冻时间戳（所有 key 上该模型被 bench）
_MODEL_COOLDOWN = {}
# bandit 统计：(gw, model) -> {"success": N, "failure": N, "tok_per_sec": [], "ttfb_ms": [], "last_used": ts}
_BANDIT = {}
# learned limits 从错误体解析：(gw, model) -> {"tpm": N, "rpd": N, ...}（只降不升）
_LEARNED_LIMITS = {}
# rate 窗口：(gw, model) -> {"rpm": 0, "rpd": 0, "tpm": 0, "tpd": 0, "window": ts}
_RATE = {}
# in-flight leases（check-then-act 竞态关闭）：lease_id -> (gw, model, tokens, created_at)
_LEASES = {}
_LEASE_ID = [0]
LEASE_MAX_AGE_S = 120

# 持久化路径
_STATE_DIR = os.path.join(os.path.expanduser("~"), ".fleet")
_STATE_PATH = os.path.join(_STATE_DIR, "llmgw_state.json")

# cooldown 阶梯（FreeLLMAPI ratelimit.ts COOLDOWN_DURATIONS）：2m→10m→1h→1d
COOLDOWN_DURATIONS = [2 * 60, 10 * 60, 60 * 60, 24 * 60 * 60]
COOLDOWN_HIT_WINDOW_S = 24 * 60 * 60

# model-level failure bench（FreeLLMAPI fallback-loop.ts）
MODEL_FAILURE_WINDOW_S = 15 * 60      # 滑动窗口 15 分钟
MODEL_FAILURE_THRESHOLD = 3           # 3 次失败 → bench
MODEL_FAILURE_COOLDOWN_S = 10 * 60    # bench 10 分钟

# bandit preset 权重（FreeLLMAPI scoring.ts BANDIT_PRESETS）
BANDIT_PRESETS = {
    "balanced":   {"reliability": 0.5,  "speed": 0.25, "intelligence": 0.25},
    "smartest":   {"reliability": 0.35, "speed": 0.1,  "intelligence": 0.55},
    "fastest":    {"reliability": 0.35, "speed": 0.55, "intelligence": 0.1},
    "reliable":   {"reliability": 0.7,  "speed": 0.15, "intelligence": 0.15},
}
BANDIT_PRESET = "balanced"

# 模型能力层（FreeLLMAPI intelligence：tier × 1000 − √rank × 31）
TIER_VALUE = {"Frontier": 4, "Large": 3, "Medium": 2, "Small": 1}
MODEL_TIER = {}       # model -> tier label
MODEL_RANK = {}       # model -> intelligence rank (1..1000, 1 = best)

# hedge：并发发前 N 个候选，取第一个成功（FreeLLMAPI 设计）
HEDGE_FANOUT = 3
# fanout 单次尝试超时上限：一个抖动的网关不能拖死整次调用（默认 chat timeout=120s）
HEDGE_ATTEMPT_TIMEOUT = 40

# cooldown-probe：只探测 heuristic cooldown，不碰 authoritative/credit/tier
COOLDOWN_PROBE_MIN_ELAPSED_FRACTION = 0.5
COOLDOWN_PROBE_MIN_REMAINING_S = 60

# speed scoring（FreeLLMAPI scoring.ts）
SPEED_SCALE_TOK_S = 60
TTFB_BEST_MS = 300
TTFB_WORST_MS = 5000
THROUGHPUT_WEIGHT = 0.6
TTFB_WEIGHT = 0.4
SPEED_PRIOR = 0.6

# headroom guardrail（FreeLLMAPI scoring.ts headroomFactor）
HEADROOM_FLOOR = 0.1
HEADROOM_RAMP_START = 0.2

# rate-limit penalty（FreeLLMAPI scoring.ts rateLimitFactor）
MAX_PENALTY = 10
RATE_LIMIT_MAX_DAMP = 0.6


def _ensure_state_dir():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
    except Exception:
        pass


def _save_state():
    """持久化 cooldown / model bench / bandit / learned limits 到磁盘。"""
    _ensure_state_dir()
    with _LOCK:
        payload = {
            "cooldown": {f"{g}|{m or '*'}": v for (g, m), v in _COOLDOWN.items()},
            "model_failures": {m: ts for m, ts in _MODEL_FAILURES.items()},
            "model_cooldown": _MODEL_COOLDOWN,
            "bandit": {f"{g}|{m or '*'}": v for (g, m), v in _BANDIT.items()},
            "learned_limits": {f"{g}|{m or '*'}": v for (g, m) in _LEARNED_LIMITS.keys() for v in [_LEARNED_LIMITS[(g, m)]]},
            "rate": {f"{g}|{m or '*'}": v for (g, m), v in _RATE.items()},
        }
    try:
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def _load_state():
    """从磁盘加载持久化状态。"""
    global _COOLDOWN, _MODEL_FAILURES, _MODEL_COOLDOWN, _BANDIT, _LEARNED_LIMITS, _RATE
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return
    with _LOCK:
        _COOLDOWN = {}
        for k, v in payload.get("cooldown", {}).items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                _COOLDOWN[(parts[0], parts[1] if parts[1] != "*" else None)] = v
        _MODEL_FAILURES = payload.get("model_failures", {})
        _MODEL_COOLDOWN = payload.get("model_cooldown", {})
        _BANDIT = {}
        for k, v in payload.get("bandit", {}).items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                _BANDIT[(parts[0], parts[1] if parts[1] != "*" else None)] = v
        _LEARNED_LIMITS = {}
        for k, v in payload.get("learned_limits", {}).items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                _LEARNED_LIMITS[(parts[0], parts[1] if parts[1] != "*" else None)] = v
        _RATE = {}
        for k, v in payload.get("rate", {}).items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                _RATE[(parts[0], parts[1] if parts[1] != "*" else None)] = v


_load_state()


# ---------- 时间 / key ----------
def _now():
    return _NOW()


def _ckey(gw, model):
    return (gw, model or "*")


def _mkey(model):
    return model or "*"


# ---------- 错误体安全提取（兼容 error 是 str / dict / 裸文本） ----------
def _extract_err(body):
    """从响应体安全提取错误文本。error 字段可能是 dict、str，或整个 body 是裸文本。"""
    if not isinstance(body, dict):
        return (str(body) or "")[:600]
    e = body.get("error")
    if isinstance(e, dict):
        return (e.get("message") or e.get("text") or str(e))[:600]
    if isinstance(e, str):
        return e[:600]
    raw = body.get("_raw") or body.get("_err")
    if raw:
        return str(raw)[:600]
    return ""


# ---------- 错误体分类（Free-GPT4-WEB-API 模式） ----------
def _classify_error(status_code, err_text):
    """把 HTTP 状态 + 错误体分类为：auth / rate / forbidden / server / timeout / context / transport / other"""
    low = (err_text or "").lower()
    if status_code == 0:
        if "timeout" in low or "timed out" in low:
            return "timeout"
        return "transport"
    if status_code in (401, 403):
        if "credit" in low or "balance" in low or "quota" in low or "payment" in low:
            return "credit"
        if status_code == 403:
            return "forbidden"
        return "auth"
    if status_code == 429 or "rate" in low or "quota" in low:
        return "rate"
    if status_code == 413 or "too large" in low or "context" in low or "maximum context" in low:
        return "context"
    if 500 <= status_code < 600:
        return "server"
    if status_code >= 400:
        return "client"
    if "timeout" in low:
        return "timeout"
    return "other"


# ---------- 错误体自学习限制（FreeLLMAPI parseProviderLimit / learnLimitFromError） ----------
_LIMIT_AXIS_PATTERNS = [
    ("tpd",  r"tokens?\s*per\s*day|\btpd\b"),
    ("otpm", r"output\s+tokens?\s*per\s*min(?:ute)?|\botpm\b"),
    ("itpm", r"input\s+tokens?\s*per\s*min(?:ute)?|\bitpm\b"),
    ("tpm",  r"tokens?\s*per\s*min(?:ute)?|\btpm\b"),
    ("rpd",  r"requests?\s*per\s*day|\brpd\b"),
    ("rpm",  r"requests?\s*per\s*min(?:ute)?|\brpm\b"),
]
_LIMIT_RE = re_compile_cache = None


def _import_re():
    import re
    return re


def _parse_provider_limit(message):
    """从错误体解析供应商声明的真实限额：Limit N tokens per minute 等。
    只降不升：填 NULL 或降低现有值，绝不提高。"""
    if not message:
        return None
    re = _import_re()
    m = re.search(r"\blimit[:\s]+([\d,]+)", message, re.I)
    if not m:
        return None
    try:
        limit = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if limit <= 0:
        return None
    for kind, pat in _LIMIT_AXIS_PATTERNS:
        if re.search(pat, message, re.I):
            return {"kind": kind, "limit": limit}
    return None


def _learn_limit(gw, model, err_text):
    """从错误体学习限额，持久化，只降不升。"""
    parsed = _parse_provider_limit(err_text)
    if not parsed:
        return
    k = _ckey(gw, model)
    with _LOCK:
        cur = _LEARNED_LIMITS.get(k, {})
        key = parsed["kind"]
        if key in ("otpm",):
            # OTPM 无对应列，跳过（避免把输出配额误写成 tpm_limit 导致大 prompt 被尺寸拒）
            return
        prev = cur.get(key)
        if prev is None or parsed["limit"] < prev:
            cur[key] = parsed["limit"]
            _LEARNED_LIMITS[k] = cur
    _save_state()


# ---------- cooldown 决策（FreeLLMAPI getCooldownDecisionForLimit） ----------
def _cooldown_decision(gw, model, reason, retry_after_s=None):
    """按原因分派 cooldown 时长 + source 标记。
    source: heuristic（可被 cooldown-probe 早期恢复）/ authoritative（供应商 Retry-After，不碰）
            / credit（402 欠费，不碰）/ tier（403 模型不在该层，不碰）
    """
    now = _now()
    k = _ckey(gw, model)

    # 本地端点豁免（loopback / LAN）：无供应商配额，短 bench
    if gw in ("ofm", "zen-gate") and "127.0.0.1" in _gw_base(gw):
        return {"seconds": 5, "source": "heuristic"}

    if reason == "auth":
        return {"seconds": 300, "source": "authoritative"}
    if reason == "credit":
        return {"seconds": 24 * 3600, "source": "credit"}
    if reason == "forbidden" or reason == "tier":
        return {"seconds": 24 * 3600, "source": "tier"}
    if reason == "context":
        return {"seconds": 24 * 3600, "source": "authoritative"}

    # rate / 429：按 24h 命中次数走升级阶梯
    entry = _COOLDOWN.get(k, {"until": 0, "source": "heuristic", "hits": [], "set_at": 0})
    hits = [t for t in entry.get("hits", []) if now - t < COOLDOWN_HIT_WINDOW_S]
    hits.append(now)
    entry["hits"] = hits
    entry["set_at"] = now
    idx = min(len(hits) - 1, len(COOLDOWN_DURATIONS) - 1)
    base = COOLDOWN_DURATIONS[idx]
    # 供应商 Retry-After：服务端明确告知要等多久 → 标 authoritative（不可被 cooldown-probe 提前恢复），
    # 时长取 max(升级阶梯, Retry-After)，既尊重服务端声明又不低于我们的阶梯（封顶 1 天）。
    if retry_after_s is not None:
        dur = min(max(base, retry_after_s), 24 * 3600)
        return {"seconds": dur, "source": "authoritative"}
    entry["until"] = now + base
    entry["source"] = "heuristic"
    _COOLDOWN[k] = entry
    return {"seconds": base, "source": "heuristic"}


def _gw_base(gw_name):
    for g in discover_gateways():
        if g["name"] == gw_name:
            return g["base"]
    return ""


def is_cooled(gw, model):
    """返回 (cooled: bool, remaining_s: float, source: str|None)。"""
    now = _now()
    k = _ckey(gw, model)
    entry = _COOLDOWN.get(k)
    if not entry:
        return False, 0.0, None
    until = entry.get("until", 0)
    if until and now < until:
        return True, round(until - now, 1), entry.get("source", "heuristic")
    # 过期则清理
    with _LOCK:
        _COOLDOWN.pop(k, None)
    return False, 0.0, None


def set_cooldown(gw, model, reason="429", seconds=None, retry_after_s=None, source=None):
    """设置 cooldown。若 seconds 给定则优先使用，否则走 _cooldown_decision。"""
    now = _now()
    k = _ckey(gw, model)
    if seconds is not None:
        entry = {"until": now + seconds, "source": source or "heuristic",
                 "hits": [], "set_at": now}
        with _LOCK:
            _COOLDOWN[k] = entry
    else:
        dec = _cooldown_decision(gw, model, reason, retry_after_s)
        entry = {"until": now + dec["seconds"], "source": dec["source"],
                 "hits": [], "set_at": now}
        with _LOCK:
            _COOLDOWN[k] = entry
    _save_state()


def clear_cooldown_early(gw, model):
    """cooldown-probe 早期恢复：只清 heuristic cooldown。"""
    k = _ckey(gw, model)
    with _LOCK:
        entry = _COOLDOWN.get(k)
        if entry and entry.get("source") == "heuristic":
            _COOLDOWN.pop(k, None)
            _save_state()
            return True
    return False


# ---------- model-level failure benching（FreeLLMAPI fallback-loop.ts） ----------
def record_model_failure(model):
    """记录一次模型级失败。滑动窗口 ≥ 阈值 → bench 所有 key 上的该模型。"""
    now = _now()
    mk = _mkey(model)
    with _LOCK:
        window = [t for t in _MODEL_FAILURES.get(mk, []) if now - t < MODEL_FAILURE_WINDOW_S]
        window.append(now)
        _MODEL_FAILURES[mk] = window
        if len(window) >= MODEL_FAILURE_THRESHOLD:
            _MODEL_COOLDOWN[mk] = now + MODEL_FAILURE_COOLDOWN_S
            _MODEL_FAILURES[mk] = []  # 重置，一条 bench  per streak
    _save_state()


def is_model_benched(model):
    """模型是否被 model-level bench。"""
    now = _now()
    mk = _mkey(model)
    until = _MODEL_COOLDOWN.get(mk, 0)
    if until and now < until:
        return True, round(until - now, 1)
    if until:
        with _LOCK:
            _MODEL_COOLDOWN.pop(mk, None)
    return False, 0.0


def record_model_success(model):
    """成功请求清空该模型的 failure 窗口（可逆性：服务了就证明配额未耗尽）。"""
    mk = _mkey(model)
    with _LOCK:
        _MODEL_FAILURES.pop(mk, None)
    _save_state()


# ---------- bandit 路由评分（FreeLLMAPI scoring.ts） ----------
def _bandit_entry(gw, model):
    k = _ckey(gw, model)
    with _LOCK:
        return dict(_BANDIT.get(k, {"success": 0, "failure": 0,
                                     "tok_per_sec": [], "ttfb_ms": [],
                                     "last_used": 0}))


def _beta_posterior(success, failure):
    """Beta(1,1) 先验，后验期望可靠性 = (success+1)/(success+failure+2)。"""
    return (success + 1) / (success + failure + 2)


def _throughput_score(tok_per_sec):
    if tok_per_sec <= 0:
        return 0
    return 1 - math.exp(-tok_per_sec / SPEED_SCALE_TOK_S)


def _ttfb_score(ttfb_ms):
    if ttfb_ms <= TTFB_BEST_MS:
        return 1.0
    if ttfb_ms >= TTFB_WORST_MS:
        return 0.0
    return 1 - (ttfb_ms - TTFB_BEST_MS) / (TTFB_WORST_MS - TTFB_BEST_MS)


def _speed_score(tok_per_sec, ttfb_ms):
    if tok_per_sec <= 0 and ttfb_ms is None:
        return SPEED_PRIOR
    tp = _throughput_score(tok_per_sec)
    if ttfb_ms is None:
        return tp
    if tok_per_sec <= 0:
        return _ttfb_score(ttfb_ms)
    return THROUGHPUT_WEIGHT * tp + TTFB_WEIGHT * _ttfb_score(ttfb_ms)


def _intelligence_composite(model):
    """tier × 1000 − √rank × 31：tier 严格分级，rank 在 tier 内微调。"""
    tier = MODEL_TIER.get(model, "Small")
    rank = MODEL_RANK.get(model, 1000)
    return TIER_VALUE.get(tier, 1) * 1000 - math.sqrt(max(1, rank)) * 31


def _intelligence_score(composite, models_composites):
    if not models_composites:
        return 1.0
    mn = min(models_composites.values())
    mx = max(models_composites.values())
    if mx <= mn:
        return 1.0
    return (composite - mn) / (mx - mn)


def _headroom_factor(gw, model):
    """基于 learned limits + rate 窗口的配额 headroom，只降不升。"""
    k = _ckey(gw, model)
    lim = _LEARNED_LIMITS.get(k, {})
    rate = _RATE.get(k, {"rpm": 0, "rpd": 0, "tpm": 0, "tpd": 0, "window": _now()})
    now = _now()
    factors = []
    # tpm headroom
    tpm = lim.get("tpm")
    if tpm:
        used = rate.get("tpm", 0)
        if now - rate.get("window", now) > 60:
            used = 0
        rem = 1 - used / tpm
        factors.append(_headroom_ramp(rem))
    # rpd headroom
    rpd = lim.get("rpd")
    if rpd:
        used = rate.get("rpd", 0)
        rem = 1 - used / rpd
        factors.append(_headroom_ramp(rem))
    if not factors:
        return 1.0
    return min(factors)


def _headroom_ramp(remaining, ramp_start=HEADROOM_RAMP_START, floor=HEADROOM_FLOOR):
    remaining = max(0.0, min(1.0, remaining))
    if remaining >= ramp_start:
        return 1.0
    return floor + (1 - floor) * (remaining / ramp_start)


def _rate_limit_factor(gw, model):
    """基于最近 429 次数的速率限制惩罚。"""
    k = _ckey(gw, model)
    entry = _COOLDOWN.get(k, {})
    hits = [t for t in entry.get("hits", []) if _now() - t < COOLDOWN_HIT_WINDOW_S]
    penalty = min(len(hits), MAX_PENALTY)
    return 1 - (penalty / MAX_PENALTY) * RATE_LIMIT_MAX_DAMP


def bandit_score(gw, model, models_for_intel=None):
    """综合 bandit 得分：convex combination × headroom × rate-limit 守 rail。"""
    entry = _bandit_entry(gw, model)
    rel = _beta_posterior(entry["success"], entry["failure"])

    tps_list = entry.get("tok_per_sec", [])
    tok_per_sec = sum(tps_list) / len(tps_list) if tps_list else 0
    ttfb_list = entry.get("ttfb_ms", [])
    ttfb_ms = sum(ttfb_list) / len(ttfb_list) if ttfb_list else None
    spd = _speed_score(tok_per_sec, ttfb_ms)

    comp = _intelligence_composite(model)
    intel_map = {model: comp}
    if models_for_intel:
        for m in models_for_intel:
            intel_map[m] = _intelligence_composite(m)
    intel = _intelligence_score(comp, intel_map)

    weights = BANDIT_PRESETS[BANDIT_PRESET]
    wsum = weights["reliability"] + weights["speed"] + weights["intelligence"] or 1
    base = (weights["reliability"] * rel + weights["speed"] * spd +
            weights["intelligence"] * intel) / wsum

    headroom = _headroom_factor(gw, model)
    ratelimit = _rate_limit_factor(gw, model)
    return base * headroom * ratelimit


def record_success(gw, model, tokens=0, tok_per_sec=0, ttfb_ms=None):
    """记录一次成功：bandit 成功计数 + rate 窗口 + 清空 model failure 窗口。"""
    now = _now()
    k = _ckey(gw, model)
    with _LOCK:
        e = _BANDIT.setdefault(k, {"success": 0, "failure": 0,
                                   "tok_per_sec": [], "ttfb_ms": [],
                                   "last_used": 0})
        e["success"] += 1
        e["last_used"] = now
        if tok_per_sec > 0:
            e.setdefault("tok_per_sec", []).append(tok_per_sec)
            e["tok_per_sec"] = e["tok_per_sec"][-50:]
        if ttfb_ms is not None:
            e.setdefault("ttfb_ms", []).append(ttfb_ms)
            e["ttfb_ms"] = e["ttfb_ms"][-50:]
        r = _RATE.setdefault(k, {"rpm": 0, "rpd": 0, "tpm": 0, "tpd": 0,
                                 "window": now})
        if now - r["window"] > 60:
            r["rpm"] = 0
            r["tpm"] = 0
            r["window"] = now
        r["rpm"] += 1
        r["rpd"] += 1
        r["tpm"] += tokens
        r["tpd"] += tokens
    record_model_success(model)
    _save_state()


def record_failure(gw, model, err_text="", reason="other", retry_after_s=None):
    """记录一次失败：bandit 失败计数 + cooldown + model-level bench（速率/服务器错误）。"""
    now = _now()
    k = _ckey(gw, model)
    with _LOCK:
        e = _BANDIT.setdefault(k, {"success": 0, "failure": 0,
                                   "tok_per_sec": [], "ttfb_ms": [],
                                   "last_used": 0})
        e["failure"] += 1
        e["last_used"] = now
    # 错误体自学习限额
    if err_text:
        _learn_limit(gw, model, err_text)
    # cooldown（透传 Retry-After → authoritative，不可被 cooldown-probe 提前恢复）
    set_cooldown(gw, model, reason=reason, retry_after_s=retry_after_s)
    # model-level bench：只有速率/服务器/上下文错误才计入模型级 failure
    if reason in ("rate", "server", "context"):
        record_model_failure(model)
    _save_state()


# ---------- in-flight lease（check-then-act 竞态关闭） ----------
def _acquire_lease(gw, model, tokens=0):
    with _LOCK:
        _LEASE_ID[0] += 1
        lid = _LEASE_ID[0]
        _LEASES[lid] = (gw, model or "*", tokens, _now())
        # 清理过期租约
        cutoff = _now() - LEASE_MAX_AGE_S
        for lid2, (g2, m2, t2, c2) in list(_LEASES.items()):
            if c2 < cutoff:
                _LEASES.pop(lid2, None)
        return lid


def _release_lease(lid):
    with _LOCK:
        _LEASES.pop(lid, None)


def _in_flight_for(gw, model):
    with _LOCK:
        mk = model or "*"
        return sum(1 for (g, m, _t, _c) in _LEASES.values() if g == gw and m == mk)


# ---------- HTTP 请求（标准库 urllib） ----------
def _req_json(base, path, key, timeout=15, method="GET", body=None,
              extra_headers=None):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, dict(r.headers), json.loads(raw), None
            except Exception:
                return r.status, dict(r.headers), {"_raw": raw}, None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, dict(e.headers), json.loads(raw), "HTTP %s" % e.code
        except Exception:
            return e.code, dict(e.headers), {"_raw": raw}, "HTTP %s" % e.code
    except Exception as e:
        return 0, {}, {"_err": str(e)}, str(e)[:200]


# ---------- chat 单次尝试 ----------
def _chat_once(gw, payload, timeout):
    """统一返回 7 元组：(body|None, err, via, tokens, cls, reason, retry_after_s)。

    成功：body 非 None，err=None，cls/reason="ok"；
    传输错（st==0）：body=None，cls="transport"，reason="timeout"；
    HTTP 错：body=None，cls/reason 由 _classify_error 决定。
    """
    st, hdrs, body, err0 = _req_json(
        gw["base"], "/v1/chat/completions", gw["key"],
        timeout=timeout, method="POST", body=payload)
    if err0 and st == 0:
        return None, err0, None, 0, "transport", "timeout", None
    if st == 200 and isinstance(body, dict) and "choices" in body:
        via = (hdrs or {}).get("X-Routed-Via") or gw["name"]
        body["_routed_via"] = via
        tokens = (body.get("usage") or {}).get("total_tokens", 0)
        return body, None, via, tokens, "ok", "ok", None
    err = _extract_err(body)
    if not err:
        err = "HTTP %s" % st
    cls = _classify_error(st, err)
    reason = {"auth": "auth", "credit": "credit", "forbidden": "forbidden",
              "tier": "tier", "rate": "429", "server": "5xx",
              "context": "context", "timeout": "timeout",
              "transport": "timeout", "client": "429", "other": "5xx"}.get(cls, "5xx")
    retry_after_s = None
    if st == 429:
        ra = (hdrs or {}).get("Retry-After") or (hdrs or {}).get("retry-after")
        if ra:
            try:
                retry_after_s = float(ra)
            except ValueError:
                pass
    return None, err, None, 0, cls, reason, retry_after_s


# ---------- 候选排序（bandit + model bench + cooldown 过滤） ----------
def _build_candidates(gws, models, session=None, sticky_pref=None):
    """构建按 bandit 得分排序的候选列表，过滤 cooldown 和 model bench。"""
    now = _now()
    candidates = []
    intel_models = list(models) if models else []
    for m in models:
        for g in gws:
            # model-level bench
            benched, _ = is_model_benched(m)
            if benched:
                continue
            # per-key cooldown
            cooled, remaining, source = is_cooled(g["name"], m)
            if cooled:
                continue
            score = bandit_score(g["name"], m, models_for_intel=intel_models)
            candidates.append({
                "gw": g, "model": m, "score": score,
                "cooldown_source": source,
            })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


# ---------- hedge 并发发送 ----------
def _hedge_send(candidates, payload_base, timeout, max_fanout=HEDGE_FANOUT):
    """对前 N 个最高分候选并发发送，取第一个成功，其余丢弃。

    返回 (res, chosen_idx, tried_indices, results)：
      - res：首个成功的 7 元组，或失败候选里索引最小者的 7 元组
      - chosen_idx：-1 表示无人成功；否则为选中的候选下标
      - tried_indices：本次 fanout 实际发起过的候选下标集合（串行 fallback 跳过它们，避免重复打）
      - results：tried_indices → 对应 7 元组（调用方据此记录冷却 / 填错误报告）
    关键：绝不在 executor 退出时 wait 所有任务——一个抖动的网关不能拖死整次调用。
    """
    top = candidates[:max_fanout]
    if len(top) <= 1:
        res, idx = _chat_once_top(top[0] if top else None, payload_base, timeout)
        return res, idx, set(range(len(top))), {idx: res}

    # 单次 fanout 尝试超时：避免一个卡住的网关吃满整次 timeout（默认 120s）
    attempt_timeout = min(timeout, HEDGE_ATTEMPT_TIMEOUT)

    results = {}
    tried = set()
    executor = ThreadPoolExecutor(max_workers=len(top))
    try:
        fut_to_idx = {}
        for idx, c in enumerate(top):
            p = dict(payload_base)
            p["model"] = c["model"]
            fut = executor.submit(_chat_once, c["gw"], p, attempt_timeout)
            fut_to_idx[fut] = idx
            tried.add(idx)
        for fut in as_completed(fut_to_idx):
            idx = fut_to_idx[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = (None, str(e)[:200], None, 0, "transport", "timeout", None)
            results[idx] = res
            # 第一个成功立即返回，不等待其余
            if res[0] is not None:
                return res, idx, tried, results
    finally:
        # wait=False：不阻塞；cancel_futures：取消还没启动的任务（已运行的会被 socket 超时自然终结）
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    # 全部失败：返回索引最小的那个错误
    best_idx = min(results.keys())
    return results[best_idx], best_idx, tried, results


def _chat_once_top(c, payload_base, timeout):
    if not c:
        return (None, "no candidate", None, 0, "transport", "timeout", None), -1
    p = dict(payload_base)
    p["model"] = c["model"]
    res = _chat_once(c["gw"], p, timeout)
    return res, 0


# =====================================================================
# 能力实现
# =====================================================================

def _h_endpoints(cap, args, dev):
    gws = discover_gateways()
    out = []
    for g in gws:
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
        nmodels = 0
        if st == 200 and isinstance(body, dict):
            nmodels = len(body.get("data") or body.get("models") or [])
        out.append({"name": g["name"], "base": g["base"],
                    "anon": g.get("anon", False), "ok": st == 200,
                    "models": nmodels,
                    "err": None if st == 200 else ("HTTP %s" % st)})
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": out,
                    "count": len(out), "online": sum(1 for x in out if x["ok"])})


def _h_models(cap, args, dev):
    gws = discover_gateways()
    result = {}
    for g in gws:
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=10)
        ids = []
        if st == 200 and isinstance(body, dict):
            raw = body.get("data") or body.get("models") or []
            for m in raw:
                mid = m.get("id") or m.get("name") or (m if isinstance(m, str) else None)
                if mid:
                    ids.append({"id": mid, "family": _family(mid)})
        result[g["name"]] = {"count": len(ids), "models": ids[:300],
                             "ok": st == 200, "err": None if st == 200 else str(err)[:120]}
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": result})


def _h_chat(cap, args, dev):
    messages = args.get("messages")
    if not messages:
        return R.error(cap, "messages 必填", device=dev.name, via=dev.via)
    models_arg = args.get("models") or args.get("model")
    if isinstance(models_arg, str):
        models = [models_arg]
    elif isinstance(models_arg, list):
        models = list(models_arg)
    else:
        models = []
    session = args.get("session")
    endpoint_pref = args.get("endpoint")
    temperature = args.get("temperature")
    max_tokens = int(args.get("max_tokens") or 1024)
    timeout = int(args.get("timeout") or 120)

    gws = discover_gateways()
    if endpoint_pref:
        gws = [g for g in gws if g["name"] == endpoint_pref] or gws
    if not gws:
        return R.unavailable(cap, "无可用网关（OFM/zen-gate 未装且匿名端点不可达）",
                             device=dev.name, via=dev.via)

    # sticky：优先复用上次成功的 (gw, model)
    if session and session in _STICKY:
        st = _STICKY[session]
        if _now() - st["ts"] < _STICKY_TTL:
            pref = [g for g in gws if g["name"] == st["gw"]]
            if pref:
                gws = pref + [g for g in gws if g["name"] != st["gw"]]
                if not models:
                    models = [st["model"]]

    # 默认模型：从首个可用网关取；匿名端点拿不到 /v1/models 时用 default_model
    if not models:
        for g in gws:
            st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
            ids = [m.get("id") for m in (body or {}).get("data", []) if m.get("id")] if (st == 200 and isinstance(body, dict)) else []
            if ids:
                models = [ids[0]]
                break
            if g.get("default_model"):
                models = [g["default_model"]]
                break
        if not models:
            return R.error(cap, "未指定 model 且拿不到模型清单", device=dev.name, via=dev.via)

    payload_base = {"messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload_base["temperature"] = temperature

    # 候选排序：bandit 得分 + cooldown/model bench 过滤
    candidates = _build_candidates(gws, models)

    attempts = []
    if not candidates:
        return R.error(cap, "所有候选均在 cooldown 或 model bench 中",
                       device=dev.name, via=dev.via)

    # hedge 并发：对前 N 个候选并发发送（单次尝试有超时上限，不阻塞退出）
    res, chosen_idx, tried, results = _hedge_send(candidates, payload_base, timeout)

    if res[0] is not None:
        body, err, via, tokens, cls, reason, retry_after = res
        c = candidates[chosen_idx]
        g = c["gw"]
        m = c["model"]
        lat = round(_now() - c.get("_t0", _now()), 2) if c.get("_t0") else 0
        # 记录成功
        record_success(g["name"], m, tokens=tokens)
        if session:
            with _LOCK:
                _STICKY[session] = {"gw": g["name"], "model": m, "ts": _now()}
        routed = body.get("_routed_via") or via or g["name"]
        return R.success(cap, dev.name, via=dev.via, data={
            "result": body, "endpoint": g["name"], "base": g["base"],
            "model": m, "routed_via": routed, "latency_s": lat,
            "fallback_attempts": attempts, "session": session,
            "cooldown_source": c.get("cooldown_source"),
        })

    # hedge 也试过的候选若失败：记录冷却 + 进 attempts（避免重复打、让错误报告有内容）
    for idx in sorted(tried):
        rr = results.get(idx)
        if rr is None or rr[0] is not None:
            continue  # 未完成的或成功的跳过
        c = candidates[idx]
        g = c["gw"]
        m = c["model"]
        err = rr[1] or ""
        reason = rr[5] or "5xx"
        attempts.append({"gateway": g["name"], "model": m,
                         "err": (err or "")[:150], "latency_s": 0,
                         "cooldown_source": c.get("cooldown_source"), "via": "hedge"})
        record_failure(g["name"], m, err_text=err, reason=reason, retry_after_s=rr[6])

    # 全部失败：尝试串行 fallback 剩余候选（跳过 hedge 已试过的，避免重复打）
    serial_timeout = min(timeout, HEDGE_ATTEMPT_TIMEOUT)
    for idx in range(len(candidates)):
        if idx in tried:
            continue
        c = candidates[idx]
        g = c["gw"]
        m = c["model"]
        p = dict(payload_base)
        p["model"] = m
        t0 = _now()
        body, err, via, tokens, retry_after_s = _chat_once_simple(g, p, serial_timeout)
        lat = round(_now() - t0, 2)
        if body is not None:
            record_success(g["name"], m, tokens=tokens)
            if session:
                with _LOCK:
                    _STICKY[session] = {"gw": g["name"], "model": m, "ts": _now()}
            routed = body.get("_routed_via") or via or g["name"]
            return R.success(cap, dev.name, via=dev.via, data={
                "result": body, "endpoint": g["name"], "base": g["base"],
                "model": m, "routed_via": routed, "latency_s": lat,
                "fallback_attempts": attempts, "session": session,
                "cooldown_source": c.get("cooldown_source"),
            })
        cls = _classify_error(0, err)
        reason = {"timeout": "timeout", "transport": "timeout"}.get(cls, "5xx")
        record_failure(g["name"], m, err_text=err, reason=reason, retry_after_s=retry_after_s)
        attempts.append({"gateway": g["name"], "model": m,
                         "err": (err or "")[:150], "latency_s": lat,
                         "cooldown_source": c.get("cooldown_source")})

    return R.error(cap, "全部候选失败：%s" % attempts, device=dev.name, via=dev.via)


def _chat_once_simple(gw, payload, timeout):
    """简化版 _chat_once，返回 (body, err, via, tokens, retry_after_s)。

    用于串行 fallback；多返回一个 retry_after_s（429 时从 Retry-After 头取），
    透传给 record_failure → set_cooldown 标记 authoritative。
    """
    st, hdrs, body, err0 = _req_json(
        gw["base"], "/v1/chat/completions", gw["key"],
        timeout=timeout, method="POST", body=payload)
    if err0 and st == 0:
        return None, err0, None, 0, None
    if st == 200 and isinstance(body, dict) and "choices" in body:
        via = (hdrs or {}).get("X-Routed-Via") or gw["name"]
        body["_routed_via"] = via
        tokens = (body.get("usage") or {}).get("total_tokens", 0)
        return body, None, via, tokens, None
    err = _extract_err(body)
    retry_after_s = None
    if st == 429:
        ra = (hdrs or {}).get("Retry-After") or (hdrs or {}).get("retry-after")
        if ra:
            try:
                retry_after_s = float(ra)
            except ValueError:
                pass
    if not err:
        err = "HTTP %s" % st
    return None, err, None, 0, retry_after_s


# sticky 状态（内存，持久化可选）
_STICKY = {}
_STICKY_TTL = 30 * 60  # 30 分钟（FreeLLMAPI 同款）


def _h_embeddings(cap, args, dev):
    inputs = args.get("input") or args.get("inputs")
    if inputs is None:
        return R.error(cap, "input 必填", device=dev.name, via=dev.via)
    if isinstance(inputs, str):
        inputs = [inputs]
    model = args.get("model")
    endpoint_pref = args.get("endpoint")
    timeout = int(args.get("timeout") or 60)

    gws = discover_gateways()
    if endpoint_pref:
        gws = [g for g in gws if g["name"] == endpoint_pref] or gws

    if not model:
        for g in gws:
            st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
            ids = [m.get("id") for m in (body or {}).get("data", []) if m.get("id")] if (st == 200 and isinstance(body, dict)) else []
            emb = [i for i in ids if "embed" in i.lower() or "bge" in i.lower() or "e5" in i.lower()]
            if emb:
                model = emb[0]
                break
        if not model:
            return R.error(cap, "未指定 model 且拿不到 embeddings 模型", device=dev.name, via=dev.via)

    attempts = []
    for g in gws:
        cooled, _, _ = is_cooled(g["name"], model)
        if cooled:
            continue
        benched, _ = is_model_benched(model)
        if benched:
            continue
        payload = {"model": model, "input": inputs}
        st, hdrs, body, _ = _req_json(g["base"], "/v1/embeddings", g["key"],
                                    timeout=timeout, method="POST", body=payload)
        if st == 200 and isinstance(body, dict) and "data" in body:
            body["_routed_via"] = (hdrs or {}).get("X-Routed-Via") or g["name"]
            record_success(g["name"], model)
            return R.success(cap, dev.name, via=dev.via, data={
                "result": body, "endpoint": g["name"], "model": model,
                "routed_via": body["_routed_via"]})
        err = _extract_err(body)
        attempts.append({"gateway": g["name"], "err": (err or "HTTP %s" % st)[:150]})
        cls = _classify_error(st, err)
        reason = {"429": "429", "rate": "429", "server": "5xx"}.get(cls, "5xx")
        record_failure(g["name"], model, err_text=err, reason=reason)
        if st == 429:
            set_cooldown(g["name"], model, "429")
        elif st >= 500:
            set_cooldown(g["name"], model, "5xx")
    return R.error(cap, "全部候选失败：%s" % attempts, device=dev.name, via=dev.via)


def _h_probe(cap, args, dev):
    model = args.get("model")
    if not model:
        return R.error(cap, "model 必填", device=dev.name, via=dev.via)
    gws = discover_gateways()
    ep = args.get("endpoint")
    if ep:
        gws = [g for g in gws if g["name"] == ep] or gws
    out = []
    for g in gws:
        cooled, _, _ = is_cooled(g["name"], model)
        if cooled:
            out.append({"gateway": g["name"], "ok": False, "ttfb_ms": None,
                        "err": "cooldown"})
            continue
        benched, _ = is_model_benched(model)
        if benched:
            out.append({"gateway": g["name"], "ok": False, "ttfb_ms": None,
                        "err": "model-benched"})
            continue
        t0 = _now()
        st, hdrs, body, _ = _req_json(g["base"], "/v1/chat/completions", g["key"],
                                    timeout=int(args.get("timeout") or 30),
                                    method="POST",
                                    body={"model": model,
                                          "messages": [{"role": "user", "content": "hi"}],
                                          "max_tokens": 4})
        ttfb = round((_now() - t0) * 1000)
        ok = st == 200
        out.append({"gateway": g["name"], "ok": ok, "ttfb_ms": ttfb if ok else None,
                    "http": st, "err": None if ok else str(body)[:120]})
        if ok:
            tok_per_sec = 0
            usage = (body or {}).get("usage") or {}
            total_tok = usage.get("total_tokens", 0)
            if ttfb > 0:
                tok_per_sec = round(total_tok / (ttfb / 1000.0), 2)
            record_success(g["name"], model, tokens=total_tok,
                           tok_per_sec=tok_per_sec, ttfb_ms=ttfb)
        else:
            cls = _classify_error(st, str(body)[:120])
            reason = {"429": "429", "rate": "429", "server": "5xx"}.get(cls, "5xx")
            record_failure(g["name"], model, err_text=str(body)[:120], reason=reason)
            if st in (429, 500, 502, 503):
                set_cooldown(g["name"], model, "429" if st == 429 else "5xx")
    return R.success(cap, dev.name, via=dev.via, data={"model": model, "probes": out})


def _h_cooldowns(cap, args, dev):
    now = _now()
    items = []
    for (gw, model), entry in list(_COOLDOWN.items()):
        until = entry.get("until", 0)
        if until <= now:
            del _COOLDOWN[(gw, model)]
            continue
        items.append({"gateway": gw, "model": model if model != "*" else None,
                      "reason": entry.get("source", "heuristic"),
                      "remaining_s": round(until - now, 1),
                      "hits_24h": len(entry.get("hits", []))})
    # model-level bench
    for mk, until in list(_MODEL_COOLDOWN.items()):
        if until <= now:
            _MODEL_COOLDOWN.pop(mk, None)
            continue
        items.append({"gateway": "*", "model": mk if mk != "*" else None,
                      "reason": "model-benched",
                      "remaining_s": round(until - now, 1),
                      "hits_24h": 0})
    items.sort(key=lambda x: x["remaining_s"])
    return R.success(cap, dev.name, via=dev.via,
                     data={"cooldowns": items, "count": len(items)})


def _h_sticky(cap, args, dev):
    action = (args.get("action") or "list").lower()
    if action == "clear":
        sid = args.get("session")
        if sid and sid in _STICKY:
            del _STICKY[sid]
            _save_state()
            return R.success(cap, dev.name, via=dev.via, data={"cleared": sid})
        if not sid:
            _STICKY.clear()
            _save_state()
            return R.success(cap, dev.name, via=dev.via, data={"cleared": "all"})
        return R.error(cap, "session 不存在", device=dev.name, via=dev.via)
    now = _now()
    items = []
    for sid, st in list(_STICKY.items()):
        if now - st["ts"] > _STICKY_TTL:
            del _STICKY[sid]
            continue
        items.append({"session": sid, "gateway": st["gw"],
                      "model": st["model"], "age_s": round(now - st["ts"], 1)})
    return R.success(cap, dev.name, via=dev.via, data={"sessions": items, "count": len(items)})


# ---------- 模型家族 ----------
def _family(model):
    """模型家族：不同家族的向量不混（FreeLLMAPI embeddings 设计）。"""
    if not model:
        return None
    m = model.lower()
    for fam in ("qwen", "llama", "gpt", "gemini", "glm", "deepseek",
                "mistral", "codestral", "nemotron", "kimi"):
        if fam in m:
            return fam
    return m.split("-")[0].split(":")[0]


_HANDLERS = {
    "llmgw.endpoints": _h_endpoints,
    "llmgw.models": _h_models,
    "llmgw.chat": _h_chat,
    "llmgw.embeddings": _h_embeddings,
    "llmgw.probe": _h_probe,
    "llmgw.cooldowns": _h_cooldowns,
    "llmgw.sticky": _h_sticky,
}


class LlmGwDevice(BaseDevice):
    name = "llmgw"
    via = "local"

    def __init__(self, name="llmgw:bench", **kw):
        super().__init__(name, via="local", **kw)

    def probe(self):
        gws = discover_gateways()
        return bool(gws), "可用网关：%s" % ", ".join(g["name"] for g in gws) if gws else "无本地网关"

    def caps(self):
        return tuple(_HANDLERS.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        fn = _HANDLERS.get(cap)
        if not fn:
            return R.unsupported(cap, "llmgw 设备没有这个能力",
                                 device=self.name, via=self.via)
        return fn(cap, args, self)


# =====================================================================
# 离线自测（不碰真实网络：monkeypatch discover_gateways + _req_json）
# =====================================================================

def _selftest():
    import copy as _copy

    # 备份并清空模块级状态，避免与真实持久化互相污染
    saved = {
        "_COOLDOWN": _COOLDOWN.copy(), "_MODEL_COOLDOWN": _MODEL_COOLDOWN.copy(),
        "_MODEL_FAILURES": _MODEL_FAILURES.copy(), "_BANDIT": _BANDIT.copy(),
        "_LEARNED_LIMITS": _LEARNED_LIMITS.copy(), "_RATE": _RATE.copy(),
        "_STICKY": _STICKY.copy(),
    }
    for d in (_COOLDOWN, _MODEL_COOLDOWN, _MODEL_FAILURES, _BANDIT, _LEARNED_LIMITS, _RATE, _STICKY):
        d.clear()

    FAKE = [{"name": "fake", "base": "http://127.0.0.1:9", "key": "x", "anon": False}]
    g0 = globals()
    real_discover = g0["discover_gateways"]
    real_req = g0["_req_json"]
    calls = {"n": 0}

    def fake_discover():
        return _copy.deepcopy(FAKE)

    def fake_req(base, path, key, timeout=15, method="GET", body=None, extra_headers=None):
        if path.endswith("/models"):
            return 200, {}, {"data": [{"id": "tm"}, {"id": "emb-1"}]}, None
        if path.endswith("/chat/completions"):
            calls["n"] += 1
            if calls["n"] == 1:   # 第 1 次成功（验证 sticky）
                return 200, {"X-Routed-Via": "fake"}, {
                    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"total_tokens": 7}}, None
            if calls["n"] == 2:   # 第 2 次 429 + Retry-After → authoritative
                return 429, {"Retry-After": "120"}, {"error": {"message": "rate"}}, None
            # 第 3 次起（含 probe）：成功，便于验证 probe 计速与后续路径
            return 200, {"X-Routed-Via": "fake"}, {
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"total_tokens": 7}}, None
        if path.endswith("/embeddings"):
            return 200, {}, {"data": [{"embedding": [0.1, 0.2], "index": 0}]}, None
        return 404, {}, {"error": "x"}, None

    # 把 monkeypatch 注入到本模块命名空间（handler 通过模块级名调用）
    g = globals()
    g["discover_gateways"] = fake_discover
    g["_req_json"] = fake_req

    try:
        d = LlmGwDevice("selftest")
        # ① 成功路径
        r = d.invoke("llmgw.chat", {"messages": [{"role": "user", "content": "ping"}],
                                    "model": "tm", "session": "s1"})
        assert r.ok, "成功路径应 ok，得到 %s" % r.err
        assert r.as_dict()["data"]["endpoint"] == "fake"
        assert r.as_dict()["data"]["routed_via"] == "fake"
        assert r.as_dict()["data"]["result"]["choices"][0]["message"]["content"] == "pong"
        # ② sticky 列表
        rs = d.invoke("llmgw.sticky", {})
        assert rs.ok and rs.as_dict()["data"]["count"] == 1, "sticky 应记录 s1"
        # ③ 第 2 次 429 → authoritative 冷却
        r2 = d.invoke("llmgw.chat", {"messages": [{"role": "user", "content": "ping"}], "model": "tm"})
        assert not r2.ok
        rc = d.invoke("llmgw.cooldowns", {})
        cds = rc.as_dict()["data"]["cooldowns"]
        assert any(c["gateway"] == "fake" and c["model"] == "tm" and c["reason"] == "authoritative"
                   for c in cds), "429+Retry-After 应标 authoritative: %s" % cds
        # ④ authoritative 不可被 cooldown-probe 提前恢复
        assert clear_cooldown_early("fake", "tm") is False, "authoritative 不应被清"
        # ⑤ 记录失败计数进了 bandit
        assert _BANDIT.get(("fake", "tm"), {}).get("success") == 1
        assert _BANDIT.get(("fake", "tm"), {}).get("failure") == 1
        # ⑥ 冷却中的候选被跳过 → 直接报 所有候选均在 cooldown
        r3 = d.invoke("llmgw.chat", {"messages": [{"role": "user", "content": "ping"}], "model": "tm"})
        assert not r3.ok and "cooldown" in r3.err, "冷却候选应被跳过: %s" % r3.err
        # ⑦ embeddings 家族内成功
        re = d.invoke("llmgw.embeddings", {"input": "hi", "model": "emb-1"})
        assert re.ok, "embeddings 应成功: %s" % re.err
        assert re.as_dict()["data"]["result"]["data"][0]["embedding"] == [0.1, 0.2]
        # ⑧ endpoints / models 盘点
        re2 = d.invoke("llmgw.endpoints", {})
        assert re2.ok and re2.as_dict()["data"]["count"] == 1
        rm = d.invoke("llmgw.models", {})
        assert rm.ok and rm.as_dict()["data"]["endpoints"]["fake"]["count"] == 2
        # ⑨ probe 成功记 bandit 速度（先清掉前面 429 留下的冷却，否则会被 cooldown 挡住）
        _COOLDOWN.clear()
        rp = d.invoke("llmgw.probe", {"model": "tm"})
        assert rp.ok and any(p["ok"] for p in rp.as_dict()["data"]["probes"])
        # ⑩ sticky clear
        rcl = d.invoke("llmgw.sticky", {"action": "clear", "session": "s1"})
        assert rcl.ok
        print("llmgw_device 自测：10 组全过")
    finally:
        # 还原模块级状态与函数，避免影响后续 import
        g["discover_gateways"] = real_discover
        g["_req_json"] = real_req
        for name, val in saved.items():
            globals()[name].clear()
            globals()[name].update(val)


if __name__ == "__main__":
    _selftest()
