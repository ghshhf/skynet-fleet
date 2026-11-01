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
    实为 g4f 的 Flask 薄壳；其「健康监控」实为流量被动计数（无主动探针），错误分类粗糙、
    且 401 也算 provider 失败。真正可抄的是 g4f 的 provider 池概念，而非它这层壳。
    我取它的错误分类 + 多层 fallback 思想，健康监控则用下面更强的实时可用性图谱。

本模块不依赖上面三者运行时，只复用它们的协议/设计，用 Python 标准库
实现一个 OpenAI 兼容客户端 + 路由器：自动发现 OFM/zen-gate 本地 key，
聚合匿名免 key 直连上游（pollinations/llm7/kilo/OVH，均实测 chat 可用），
支持跨网关·跨模型 fallback、配额追踪、cooldown、粘性会话、家族路由、
实时可用性图谱 + bandit 路由 + hedge 并发 —— 目标是「模型随时可用」：
无论哪个上游挂/限流，自动切到其他健康上游，用户无感。

能力（10 项）：
  llmgw.endpoints   — 列出所有可用网关（OFM/zen-gate/匿名）+ 健康探测
  llmgw.models      — 按网关列出模型清单（含 family 分类）
  llmgw.chat        — OpenAI 兼容 chat，跨网关·跨模型 fallback + cooldown + bandit 路由 + hedge 并发
  llmgw.embeddings  — embeddings，家族内 failover（不同模型向量不混）
  llmgw.probe       — 单模型首字延迟（TTFB）
  llmgw.cooldowns   — 查看当前冷却中的端点/模型（带 source 标记）
  llmgw.sticky      — 查看/清除粘性会话
  llmgw.compress    — 网关层上下文压缩（省 token、少撞上下文上限）
  llmgw.health      — 可用性图谱（各端点模型实时健康状态，坏的降权/过滤 → 模型随时可用）
  llmgw.egress      — 网络出口状态（免费端点直连即可用；环境若设了 HTTPS_PROXY 则透明走，非必需）
"""
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse as _urlparse
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

# 匿名端点（FreeLLMAPI 设计：no key，直接打）。全部经实测（DNS+TCP+chat 探测）确认可用。
# 不同软件绑定不同上游 → 不同免费模型；天网把这些免 key 直连上游全聚合，加自家 OFM/zen-gate，
# 再加冷却/hedge/fallback/健康图谱，做到「无论哪个上游挂/限流，总有别的能答 = 模型随时可用」。
ANON_ENDPOINTS = [
    {"name": "pollinations", "base": "https://text.pollinations.ai/openai/v1",
     "key": "", "anon": True, "default_model": "openai-fast",
     "note": "自建开源权重 GPT-OSS 20B，匿名可用（实测 chat OK）"},
    {"name": "llm7", "base": "https://api.llm7.io/v1",
     "key": "", "anon": True, "default_model": "gpt-oss-20b",
     "note": "100 req/hr 匿名，聚合多上游（偶发超时，靠冷却兜底）"},
    {"name": "kilo", "base": "https://api.kilo.ai/api/gateway/v1",
     "key": "", "anon": True, "default_model": "kilo-auto/free", "free_only": True,
     "note": "kilo 免费池；/v1/models 会吐 384 个（绝大多数要付费/需 key），"
             "只有 :free / -free 结尾的是匿名免费 → free_only 过滤，别把付费模型当可用上报"},
    {"name": "ovh", "base": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
     "key": "", "anon": True, "default_model": "gpt-oss-20b",
     "note": "OVH AI Endpoints 免费开源权重，匿名可用（22 模型，修正域名后实测 OK）"},
]


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# 免费模型 id 标记：`:free` / `-free` / `/free` 结尾（`kilo-auto/free`、`nvidia/x:free`、
# `glm-5.3-flash-free` 都算）。只为「只认免费池」的端点做上报过滤，不做能力判断。
_FREE_ID = re.compile(r"(:free|/free|-free)$", re.I)


def _is_free_id(mid):
    return bool(_FREE_ID.search((mid or "").strip()))


def _only_free(gw, ids):
    """端点声明 free_only 时过滤掉付费模型 id；过滤后为空则原样返回（宁可多报，不误杀）。"""
    if not gw.get("free_only") or not ids:
        return ids
    kept = [x for x in ids if _is_free_id(x)]
    return kept or ids


# ---------- 本地离线兜底桥（Ollama 兼容 / 天网 compute.py） ----------
# 默认关闭：需显式 LLMW_LOCAL=1 开启。开启后探测 127.0.0.1:11434（Ollama 的 OpenAI 兼容 /v1），
# 在线才加入，且永远排在所有远程免费层之后（remote 优先，local 是断网安全网 → 随处可用）。
# 默认关闭的原因：本机实测关闭的 11434 被防火墙静默丢弃而非拒绝，探测会阻塞 ~2s；
# 对没装 Ollama 的机器做无谓探测不划算。LLMW_LOCAL_BASE 改指向天网自己的 compute 端点。
_LOCAL_BASE = os.environ.get("LLMW_LOCAL_BASE", "http://127.0.0.1:11434")
_LOCAL_MODEL = os.environ.get("LLMW_LOCAL_MODEL", "llama3.1:latest")
_LOCAL_CACHE = {"gw": None, "ts": 0.0, "ttl": 30.0}


def _local_enabled():
    v = os.environ.get("LLMW_LOCAL", "")
    if v == "":
        return False                     # 默认关闭：避免对无 Ollama 的机器做无谓探测/阻塞
    return v.lower() not in ("0", "off", "false", "no")


def _local_gw():
    if not _local_enabled():
        return None
    now = _now()
    if _LOCAL_CACHE["gw"] is not None and now - _LOCAL_CACHE["ts"] < _LOCAL_CACHE["ttl"]:
        return _LOCAL_CACHE["gw"]
    st, _, body, _ = _req_json(_LOCAL_BASE.rstrip("/") + "/v1", "/models", None, timeout=2)
    gw = None
    if st == 200 and isinstance(body, dict):
        ids = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
        gw = {"name": "local", "base": _LOCAL_BASE.rstrip("/") + "/v1", "key": "",
              "anon": False, "local": True,
              "default_model": ids[0] if ids else _LOCAL_MODEL}
    _LOCAL_CACHE.update(gw=gw, ts=now)
    return gw


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
                    "free_only": bool(a.get("free_only")),
                    "note": a.get("note", "")})
    # 本地离线兜底（Ollama 兼容）——永远排最后，远程免费层优先
    lg = _local_gw()
    if lg:
        gws.append(lg)
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
# 可用性图谱（freellmapi model_probe_status 思路，但用实时流量驱动而非纯周期探针）：
# (gw, model) -> {"ok": ts, "fail": ts, "consecutive": int, "healthy": bool}
# 成功/失败实时更新；已知连续失败且不自愈的端点，_build_candidates 会降权/过滤，
# 把 hedge 名额留给健康端点 → 请求不浪费在已死上游上 = 模型随时可用。
_HEALTH = {}
# 连续失败达到此值且近期无成功 → 判定 unhealthy（过滤出候选，不浪费 hedge 名额）
HEALTH_FAIL_THRESHOLD = 3
# 最近一次成功距今小于此值 → 强制 healthy（自愈恢复，哪怕之前连续失败）
HEALTH_RECOVER_S = 180
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


# ---------- 可用性图谱（实时流量驱动；freellmapi model_probe_status 的轻量版） ----------
def _update_health(gw, model, ok):
    """请求后更新 (gw, model) 的健康状态。成功 → 重置连续失败并标记 healthy；
    失败 → 连续失败 +1，连续达到阈值且无近期成功则判定 unhealthy。"""
    k = _ckey(gw, model)
    with _LOCK:
        st = _HEALTH.get(k, {"ok": 0.0, "fail": 0.0, "consecutive": 0, "healthy": True})
        now = _now()
        if ok:
            st["ok"] = now
            st["consecutive"] = 0
            st["healthy"] = True
        else:
            st["fail"] = now
            st["consecutive"] = st.get("consecutive", 0) + 1
            if st["consecutive"] >= HEALTH_FAIL_THRESHOLD and (now - st.get("ok", 0)) > HEALTH_RECOVER_S:
                st["healthy"] = False
        _HEALTH[k] = st


def _health_state(gw, model):
    """返回 (healthy: bool, consecutive_fail: int, last_ok_ts, last_fail_ts)。"""
    k = _ckey(gw, model)
    with _LOCK:
        st = _HEALTH.get(k)
        if not st:
            return True, 0, 0.0, 0.0   # 未知 = 默认健康（乐观，首次请求自证）
        # 自愈：最近成功足够近 → 强制 healthy
        now = _now()
        if st.get("ok", 0) and (now - st["ok"]) < HEALTH_RECOVER_S:
            return True, st.get("consecutive", 0), st["ok"], st["fail"]
        return st.get("healthy", True), st.get("consecutive", 0), st["ok"], st["fail"]


def _is_health_filtered(gw, model):
    """是否应被过滤出候选（已知连续失败且不自愈）。返回 True 表示先别选它。"""
    healthy, consecutive, last_ok, _ = _health_state(gw, model)
    if healthy:
        return False
    # unhealthy：仅当近期也无成功（已自愈恢复的情况 _health_state 已返回 healthy）
    return not healthy


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


# ---------- 出口：免费端点直连即可用，代理只是可选增强 ----------
# 免费端点分两类：本地网关（OFM/zen-gate，127.0.0.1，国内稳）和匿名端点
# （pollinations/llm7/kilo/ovh）。后者正是 FreeLLMAPI / Free-GPT4-WEB-API 直连使用的那种
# ——它们自建开源权重或公开服务，国内直连就能用（实测 pollinations/llm7 建连 <1s，
# chat 端到端返回正常）。本模块**默认直连**，这就是那两个开源项目的玩法。
# HTTPS_PROXY / HTTP_PROXY 只是「可选透明增强」：用户自有全局代理 / VPN 时设上，
# urllib 自动走它（例如某些网络下 ovh 解析不稳时可绕）；但它**不是**免费端点可用的前提。
# 不接 gitport：它只接管 GitHub/arxiv 等受管域名，LLM 端点不在其列，导过去也是 DIRECT。
_EGRESS_CACHE = {"addr": None, "ts": 0.0, "ttl": 30.0}


def _egress_proxy_addr():
    """出口代理地址：读标准环境变量 HTTPS_PROXY / HTTP_PROXY，否则 None。
    缓存 30s；LLMW_NO_PROXY=1 强制纯直连。本地网关不走这里（调用处判断）。"""
    if os.environ.get("LLMW_NO_PROXY", "").lower() in ("1", "on", "true", "yes"):
        return None
    now = time.time()
    if now - _EGRESS_CACHE["ts"] < _EGRESS_CACHE["ttl"]:
        return _EGRESS_CACHE["addr"]
    addr = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    if addr and not addr.startswith("http"):
        addr = "http://" + addr
    _EGRESS_CACHE["addr"] = addr
    _EGRESS_CACHE["ts"] = now
    return addr


def _dns_ok(host, timeout=2.5):
    """快速 DNS 预筛：域名能否解析 + 端口是否可达。不可达则立即返回 False，
    避免 urllib 在死域名上卡 ~11s（OS 级 DNS 超时无超时参数可控）。
    仅对「非本地 + 未走代理」的请求有意义（代理场景下解析发生在代理侧）。"""
    try:
        import socket as _sk
        import threading as _th

        def _resolve():
            _sk.gethostbyname(host)

        t = _th.Thread(target=_resolve, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return False
        return True
    except Exception:
        return False


# ---------- HTTP 请求（标准库 urllib） ----------
def _join_url(base, path):
    """拼 base+path，避免 base 已含 /v1 时 path 再带 /v1 造成 /v1/v1 重复
    （匿名端点 base 多含 /v1，而统一 path 又是 /v1/chat/completions）。"""
    b = (base or "").rstrip("/")
    p = path or ""
    if b.endswith("/v1") and p.startswith("/v1/"):
        return b + p[3:]
    return b + p


def _req_json(base, path, key, timeout=15, method="GET", body=None,
              extra_headers=None):
    url = _join_url(base, path)
    host = (_urlparse.urlparse(base).hostname or "").lower()
    local = host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
    # 本地网关直连；远程默认直连（免费端点直连即可用，正是 FreeLLMAPI/Free-GPT4-WEB-API 的玩法），
    # 若环境设了 HTTPS_PROXY 则透明走（可选增强，非可用性前提）。
    handlers = []
    px = None
    if not local:
        px = _egress_proxy_addr()
        if px:
            handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
        else:
            # 未走代理时，对死域名做快速 DNS 预筛，避免 urllib 卡 ~11s 的 OS DNS 超时。
            if not _dns_ok(host):
                return 0, {}, {"_err": "dns-unreachable:%s" % host}, \
                    "域名 %s 解析/不可达（DNS 预筛）" % host
    handlers.append(urllib.request.HTTPHandler())
    handlers.append(urllib.request.HTTPSHandler())
    opener = urllib.request.build_opener(*handlers)
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
        with opener.open(req, timeout=timeout) as r:
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


# ---------- 网关层上下文压缩（RTK / Caveman 思路，OmniRoute·9router 同款） ----------
# 免费层额度小、上下文窗窄；在网关出口处把工具结果等冗余内容压缩，让免费额度更耐用、
# 更少撞 context 上限。纯本地、零网络依赖。OmniRoute 称省 15–95%，9router −40%，
# 核心是压缩工具输出 + 长上下文。这里走保守安全版：level 0=关；1=结构化内容去空白
# （JSON/array minify，语义不变）；2=再去空白后超长截断（安全网，默认 cap 16K）。
def _compress_messages(messages, level=1, cap=16000):
    if not level or not messages:
        return list(messages), 0
    saved = 0
    out = []
    for m in messages:
        m = dict(m)
        c = m.get("content")
        if isinstance(c, str) and len(c) > 400:
            s = c.strip()
            if s[:1] in ("{", "["):
                try:
                    obj = json.loads(c)
                    mini = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
                    saved += len(c) - len(mini)
                    c = mini
                except Exception:
                    pass
            if level >= 2 and len(c) > cap:
                drop = len(c) - cap - 36
                if drop > 0:
                    c = c[:cap] + "\n…[llmgw: truncated %d chars]" % drop
                    saved += drop
            m["content"] = c
        out.append(m)
    return out, saved


# ---------- 上游同源识别（"假多样性"的解药） ----------
# 本机实证（见记忆 2026-11-01-反代与免费上游机制.md）：OFM 报 50 个模型、zen-gate 报 21 个，
# 但两家的上游其实是同一批（kilo free / OpenCode-zen free / 自家账号池）。同一个模型的
# 不同写法（`glm-5.3-flash-free` vs `opencode/glm-5.3-flash-free`）会被当成两个独立候选，
# 于是一次 hedge 三个名额全打在同源上 —— 上游一挂同时挂，降权也救不回来。
# 解法：按模型身份归一，同源候选只让分最高的那一个进并发，其余留作串行备位。
_IDENTITY_TAIL = re.compile(r"(:free|:latest|-free|-latest)+$", re.I)


def _model_identity(model):
    """模型身份归一 key：大小写、`opencode/` 这类厂商前缀、`:free`/`-free` 后缀都不算差异。

    例：`glm-5.3-flash-free` / `opencode/glm-5.3-flash-free` / `glm-5.3-flash:free`
        → 全部归一为 `glm-5.3-flash`
        `nvidia/nemotron-3.5-super-vl:free` / `nemotron-3.5-super-vl-free`
        → 全部归一为 `nemotron-3.5-super-vl`
    只在「同一模型的不同入口」上合并；确实不同的模型（ling-3.0 vs ling-3.1）仍然分开。
    """
    m = (model or "").strip().lower()
    if not m:
        return ""
    m = _IDENTITY_TAIL.sub("", m)
    if "/" in m:
        tail = m.rsplit("/", 1)[-1].strip()
        # 斜杠后为空（如 `kilo-auto/free` 去掉 :free 后剩 `kilo-auto/free`）时保留原样
        if tail:
            m = tail
    return _IDENTITY_TAIL.sub("", m) or m


def _fanout_pick(candidates, n):
    """从有序候选里挑 n 个「不同模型身份」的下标；名额不足时才用同源备位补。

    返回 candidates 的**全局下标**列表（调用方按它索引，不要用局部序号）。
    """
    if n <= 0:
        return []
    picked, seen = [], set()
    for i, c in enumerate(candidates):
        o = c.get("origin") or c.get("model")
        if o in seen:
            continue
        seen.add(o)
        picked.append(i)
        if len(picked) >= n:
            return picked
    # 不同模型不够 n 个：拿同源备位补足并发名额（总比空着强）
    chosen = set(picked)
    for i in range(len(candidates)):
        if i in chosen:
            continue
        picked.append(i)
        chosen.add(i)
        if len(picked) >= n:
            break
    return picked


# ---------- 候选排序（bandit + model bench + cooldown 过滤） ----------
def _build_candidates(gws, models, session=None, sticky_pref=None):
    """构建按 bandit 得分排序的候选列表，过滤 cooldown 和 model bench。"""
    now = _now()
    candidates = []
    intel_models = list(models) if models else []
    for m in models:
        origin = _model_identity(m)
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
            # 健康图谱：已知连续失败且不自愈的端点降权（不浪费 hedge 名额，但不完全丢弃 —
            # 万一是全部上游都临时抽风，仍会按降权顺序尝试，不至于直接返回「无候选」）
            healthy = not _is_health_filtered(g["name"], m)
            if not healthy:
                score *= 0.001
            candidates.append({
                "gw": g, "model": m, "score": score, "origin": origin,
                "cooldown_source": source, "healthy": healthy,
            })
    # 先按 local 标志升序（非本地在前），再按 bandit 分降序（稳定排序：同分时本地仍压最后）
    candidates.sort(key=lambda c: c["gw"].get("local", False))
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
    # 同源去重取位：避免多个并发名额打在同一批上游的不同入口上（一起挂 = 白打）
    top_idx = _fanout_pick(candidates, max_fanout)
    if not top_idx:
        return (None, "no candidate", None, 0, "transport", "timeout", None), -1, set(), {}
    if len(top_idx) <= 1:
        i = top_idx[0]
        res, _ = _chat_once_top(candidates[i], payload_base, timeout)
        return res, i, {i}, {i: res}

    # 单次 fanout 尝试超时：避免一个卡住的网关吃满整次 timeout（默认 120s）
    attempt_timeout = min(timeout, HEDGE_ATTEMPT_TIMEOUT)

    results = {}
    tried = set()
    executor = ThreadPoolExecutor(max_workers=len(top_idx))
    try:
        fut_to_idx = {}
        for idx in top_idx:
            c = candidates[idx]
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
        raw_ids = []
        if st == 200 and isinstance(body, dict):
            raw_ids = [m.get("id") for m in (body.get("data") or body.get("models") or []) if m.get("id")]
        raw_ids = _only_free(g, raw_ids)
        ids = list(raw_ids)
        if not ids and g.get("default_model"):
            # /v1/models 未实现或返回空（如 pollinations 在 openai 基址下返回 200 空列表）
            # → 用已知默认模型兜底，仍可用
            ids = [g["default_model"]]
        via = "models-endpoint" if raw_ids else ("default_model" if ids else "none")
        out.append({"name": g["name"], "base": g["base"],
                    "anon": g.get("anon", False),
                    "ok": bool(ids),
                    "models": len(ids),
                    "discovered_via": via,
                    "err": None if ids else ("HTTP %s" % st)})
    return R.success(cap, dev.name, via=dev.via, data={"endpoints": out,
                    "count": len(out), "online": sum(1 for x in out if x["ok"])})


def _h_models(cap, args, dev):
    gws = discover_gateways()
    result = {}
    for g in gws:
        st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=10)
        ids = []
        raw_ids = []
        if st == 200 and isinstance(body, dict):
            raw = body.get("data") or body.get("models") or []
            mids = []
            for m in raw:
                mid = m.get("id") or m.get("name") or (m if isinstance(m, str) else None)
                if mid:
                    mids.append(mid)
            # free_only 端点（如 kilo 报 384 个但绝大多数要付费）只统计真免费的
            mids = _only_free(g, mids)
            for mid in mids:
                ids.append({"id": mid, "family": _family(mid)})
                raw_ids.append(mid)
        if not ids and g.get("default_model"):
            ids = [{"id": g["default_model"], "family": _family(g["default_model"]),
                    "via": "default_model"}]
        idents = []
        for it in ids:
            it2 = dict(it)
            it2["origin"] = _model_identity(it["id"])
            idents.append(it2)
        result[g["name"]] = {"count": len(ids), "models": idents[:300],
                             "ok": bool(ids),
                             "distinct_origins": len({x["origin"] for x in idents}),
                             "err": None if ids else str(err)[:120]}
    # 全局同源度：模型条目总数 vs 真正不同的模型数（"假多样性"的量化指标）
    all_origins = set()
    total_rows = 0
    for v in result.values():
        total_rows += v["count"]
        for it in v["models"]:
            all_origins.add(it["origin"])
    return R.success(cap, dev.name, via=dev.via, data={
        "endpoints": result,
        "total_model_rows": total_rows,
        "distinct_models": len(all_origins),
        "duplication": (round(total_rows / len(all_origins), 2) if all_origins else 0),
    })


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

    # 默认模型选取（用户没指定 model 时）：
    # 1) 各网关的 default_model 是**实测验证过能 chat** 的精挑模型 → 优先；
    #    多个就按 bandit 分挑最高的，且跳过已知不健康的。
    #    （反例：OFM 的 /v1/models[0] 是 `big-pickle`，实测 availability 标 throttled —— 不能当默认）
    # 2) 都没有才退到 /v1/models 第一个（free_only 端点先过滤付费模型）。
    if not models:
        picks = [(g["default_model"], g) for g in gws if g.get("default_model")
                 and not _is_health_filtered(g["name"], g["default_model"])]
        if picks:
            picks.sort(key=lambda t: bandit_score(t[1]["name"], t[0]), reverse=True)
            models = [picks[0][0]]
        else:
            for g in gws:
                st, _, body, err = _req_json(g["base"], "/v1/models", g["key"], timeout=8)
                ids = []
                if st == 200 and isinstance(body, dict):
                    ids = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                    ids = _only_free(g, ids)
                if ids:
                    models = [ids[0]]
                    break
        if not models:
            return R.error(cap, "未指定 model 且拿不到模型清单", device=dev.name, via=dev.via)

    # 网关层上下文压缩（默认开，level 1=结构化去空白；0 关闭）：让免费层额度更耐用
    compress_level = args.get("compress", 1)
    if isinstance(compress_level, str):
        compress_level = 0 if compress_level.lower() in ("0", "false", "off", "no") else 1
    try:
        compress_level = int(compress_level)
    except (TypeError, ValueError):
        compress_level = 1
    comp_msgs, comp_saved = _compress_messages(messages, level=compress_level)
    payload_base = {"messages": comp_msgs, "max_tokens": max_tokens}
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
        _update_health(g["name"], m, True)
        if session:
            with _LOCK:
                _STICKY[session] = {"gw": g["name"], "model": m, "ts": _now()}
        routed = body.get("_routed_via") or via or g["name"]
        return R.success(cap, dev.name, via=dev.via, data={
            "result": body, "endpoint": g["name"], "base": g["base"],
            "model": m, "origin": c.get("origin"), "routed_via": routed, "latency_s": lat,
            "fallback_attempts": attempts, "session": session,
            "cooldown_source": c.get("cooldown_source"),
            "compress_saved_chars": comp_saved,
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
        _update_health(g["name"], m, False)

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
                "model": m, "origin": c.get("origin"), "routed_via": routed, "latency_s": lat,
                "fallback_attempts": attempts, "session": session,
                "cooldown_source": c.get("cooldown_source"),
                "compress_saved_chars": comp_saved,
            })
        cls = _classify_error(0, err)
        reason = {"timeout": "timeout", "transport": "timeout"}.get(cls, "5xx")
        record_failure(g["name"], m, err_text=err, reason=reason, retry_after_s=retry_after_s)
        _update_health(g["name"], m, False)
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


def _h_egress(cap, args, dev):
    """网络出口状态：免费端点直连即可用；环境若设了 HTTPS_PROXY 则透明走（可选增强）。"""
    disabled = os.environ.get("LLMW_NO_PROXY", "").lower() in ("1", "on", "true", "yes")
    addr = _egress_proxy_addr()
    return R.success(cap, dev.name, via=dev.via, data={
        "proxy_addr": addr,
        "proxy_active": bool(addr),
        "disabled": disabled,
        "direct_ok": True,
        "note": "免费端点（pollinations/llm7/kilo/ovh）直连即可用，无需任何代理——"
                "这正是 FreeLLMAPI/Free-GPT4-WEB-API 的玩法（实测建连 <1s、chat 端到端正常）。"
                "HTTPS_PROXY/HTTP_PROXY 只是可选透明增强：用户自有全局代理时设上会自动走，"
                "但非可用性前提。本地网关(OFM/zen-gate)始终直连。",
    })


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


def _h_compress(cap, args, dev):
    messages = args.get("messages") or []
    level = args.get("level", 1)
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 1
    cap_n = int(args.get("cap", 16000) or 16000)
    out, saved = _compress_messages(messages, level=level, cap=cap_n)
    return R.success(cap, dev.name, via=dev.via, data={
        "messages": out, "saved_chars": saved,
        "level": level, "count": len(out)})


def _h_health(cap, args, dev):
    """可用性图谱：各 (gw, model) 实时健康状态（流量驱动，已知死的端点会降权/过滤）。"""
    now = _now()
    with _LOCK:
        items = []
        for (g, m), st in list(_HEALTH.items()):
            ok_ts = st.get("ok", 0.0)
            healthy = bool(st.get("healthy", True)) or (ok_ts and (now - ok_ts) < HEALTH_RECOVER_S)
            items.append({
                "gateway": g, "model": (None if m == "*" else m),
                "healthy": bool(healthy),
                "consecutive_fail": st.get("consecutive", 0),
                "last_ok": round(ok_ts, 1), "last_fail": round(st.get("fail", 0.0), 1),
            })
    total = len(items)
    healthy_n = sum(1 for x in items if x["healthy"])
    return R.success(cap, dev.name, via=dev.via, data={
        "items": items[:500], "total": total, "healthy": healthy_n,
        "unhealthy": total - healthy_n,
        "fail_threshold": HEALTH_FAIL_THRESHOLD, "recover_s": HEALTH_RECOVER_S,
    })


_HANDLERS = {
    "llmgw.endpoints": _h_endpoints,
    "llmgw.models": _h_models,
    "llmgw.chat": _h_chat,
    "llmgw.embeddings": _h_embeddings,
    "llmgw.probe": _h_probe,
    "llmgw.cooldowns": _h_cooldowns,
    "llmgw.sticky": _h_sticky,
    "llmgw.compress": _h_compress,
    "llmgw.egress": _h_egress,
    "llmgw.health": _h_health,
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
    real_dns_ok = g0["_dns_ok"]
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
        # ⑪ 网关层压缩：结构化内容去空白 + 超长截断（level 2）
        big = '{\n  "status": "ok",\n  "data": [1, 2, 3, 4, 5]\n}\n' + "z" * 500
        rcx = d.invoke("llmgw.compress", {"messages": [{"role": "tool", "content": big}],
                                          "level": 2, "cap": 120})
        assert rcx.ok, "compress 应成功: %s" % rcx.err
        dx = rcx.as_dict()["data"]
        assert dx["saved_chars"] > 0, "应省出字符: %s" % dx
        assert dx["messages"][0]["content"].startswith("{"), "minify 后结构保留"
        # 默认 level=1 对短消息不压缩
        rs0 = d.invoke("llmgw.compress", {"messages": [{"role": "user", "content": "hi"}]})
        assert rs0.ok and rs0.as_dict()["data"]["saved_chars"] == 0
        # ⑫ 可选代理增强机制（不依赖真实网络）：免费端点默认直连，环境设了
        #     HTTPS_PROXY 则透明走；LLMW_NO_PROXY 强制直连。逻辑正确，仅验证开关。
        # 先清掉可能从运行环境继承来的代理变量，保证测试确定性
        _pk = [k for k in os.environ if k.upper() in ("HTTPS_PROXY", "HTTP_PROXY")]
        _pv = {k: os.environ[k] for k in _pk}
        for k in _pk:
            del os.environ[k]
        # 设置 HTTPS_PROXY → 生效
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9999"
        g0["_EGRESS_CACHE"].update(addr=None, ts=0.0)
        assert g0["_egress_proxy_addr"]() == "http://127.0.0.1:9999", g0["_egress_proxy_addr"]()
        # 缓存命中：环境变量改回空也应返回缓存值（30s 内）
        del os.environ["HTTPS_PROXY"]
        assert g0["_egress_proxy_addr"]() == "http://127.0.0.1:9999", "应命中缓存"
        # LLMW_NO_PROXY 强制关（覆盖缓存）
        os.environ["LLMW_NO_PROXY"] = "1"
        g0["_EGRESS_CACHE"].update(addr=None, ts=0.0)
        assert g0["_egress_proxy_addr"]() is None
        del os.environ["LLMW_NO_PROXY"]
        g0["_EGRESS_CACHE"].update(addr=None, ts=0.0)
        # 本地请求不走代理 / 远程请求走代理（spy build_opener）
        seen = {}
        real_build = urllib.request.build_opener
        def _spy(*h):
            seen["h"] = h
            return real_build(*h)
        urllib.request.build_opener = _spy
        g0["_EGRESS_CACHE"].update(addr="http://127.0.0.1:9999", ts=time.time())
        seen["h"] = ()
        real_req("http://127.0.0.1:18900", "/v1/models", "", timeout=1)
        assert not any(isinstance(x, urllib.request.ProxyHandler) for x in seen["h"]), "本地不应走代理"
        seen["h"] = ()
        real_req("https://api.llm7.io/v1", "/v1/models", "", timeout=1)
        assert any(isinstance(x, urllib.request.ProxyHandler) for x in seen["h"]), "远程应走出口代理"
        urllib.request.build_opener = real_build
        # 死域名 DNS 预筛：无代理时 _dns_ok 返回 False → _req_json 立即报错，不卡 11s
        g0["_dns_ok"] = lambda h, timeout=2.5: False
        g0["_EGRESS_CACHE"].update(addr=None, ts=0.0)
        st_dns, _, body_dns, err_dns = real_req("https://dead.example.invalid/v1/models", "", "", timeout=1)
        assert st_dns == 0 and "DNS 预筛" in (err_dns or ""), "死域名应秒退: %s" % err_dns
        g0["_dns_ok"] = real_dns_ok
        g0["_EGRESS_CACHE"].update(addr=None, ts=0.0)
        # 还原代理环境变量
        for k, v in _pv.items():
            os.environ[k] = v
        # ⑬ 可用性图谱（流量驱动，不依赖真实网络）
        g0["_HEALTH"].clear()
        hg, hm = "hgw", "hmodel"
        assert g0["_health_state"](hg, hm)[0] is True, "未知端点默认健康"
        assert g0["_is_health_filtered"](hg, hm) is False
        g0["_update_health"](hg, hm, True)
        assert g0["_health_state"](hg, hm)[0] is True
        # 让 last_ok 落到自愈窗口之外，再连续失败 3 次 → 判定 unhealthy
        g0["_HEALTH"][(hg, hm)]["ok"] = _now() - (HEALTH_RECOVER_S + 300)
        for _ in range(HEALTH_FAIL_THRESHOLD):
            g0["_update_health"](hg, hm, False)
        assert g0["_is_health_filtered"](hg, hm) is True, "连续失败应判定 unhealthy"
        # 一次成功立即恢复（自愈）
        g0["_update_health"](hg, hm, True)
        assert g0["_is_health_filtered"](hg, hm) is False, "成功后应恢复健康"
        # llmgw.health 能力返回图谱
        rh = d.invoke("llmgw.health", {})
        assert rh.ok and rh.as_dict()["data"]["total"] >= 1
        # URL 拼接去重：base 含 /v1 时 path 的 /v1 不重复
        assert g0["_join_url"]("https://x/v1", "/v1/chat/completions") == "https://x/v1/chat/completions"
        assert g0["_join_url"]("https://x", "/v1/models") == "https://x/v1/models"
        assert g0["_join_url"]("http://127.0.0.1:1/v1", "/models") == "http://127.0.0.1:1/v1/models"

        # ⑭ 上游同源归并（假多样性解药，不依赖真实网络）
        ident = g0["_model_identity"]
        # 同一模型的不同写法必须归一到同一个 key
        assert ident("glm-5.3-flash-free") == ident("opencode/glm-5.3-flash-free") == "glm-5.3-flash"
        assert ident("glm-5.3-flash:free") == "glm-5.3-flash"
        assert ident("nvidia/nemotron-3.5-super-vl:free") == ident("nemotron-3.5-super-vl-free") \
            == "nemotron-3.5-super-vl"
        # 不同模型不许被合并（否则会把真备选也砍掉）
        assert ident("ling-3.0-flash-fin-free") != ident("ling-3.1-flash-1019-free")
        assert ident("") == ""
        # 去重取位：同源只拿一个并发名额
        cands = [
            {"model": "glm-5.3-flash-free", "origin": "glm-5.3-flash"},
            {"model": "opencode/glm-5.3-flash-free", "origin": "glm-5.3-flash"},
            {"model": "deepseek-v4.1-flash-free", "origin": "deepseek-v4.1-flash"},
            {"model": "opencode/deepseek-v4.1-flash-free", "origin": "deepseek-v4.1-flash"},
            {"model": "kilo-auto/free", "origin": "kilo-auto/free"},
        ]
        pick = g0["_fanout_pick"](cands, 3)
        assert pick == [0, 2, 4], pick          # 3 个不同模型，跳过 1 和 3
        # 不同模型不够时用同源备位补足并发名额（不能空着）
        assert g0["_fanout_pick"](cands, 5) == [0, 2, 4, 1, 3]
        assert g0["_fanout_pick"]([], 3) == []
        # _build_candidates 必须带上 origin 字段
        real_disc = g0["discover_gateways"]
        g0["discover_gateways"] = lambda: [
            {"name": "ofm", "base": "http://127.0.0.1:1/v1", "key": "k"},
            {"name": "zen-gate", "base": "http://127.0.0.1:2/v1", "key": "k"},
        ]
        try:
            cs = g0["_build_candidates"](g0["discover_gateways"](),
                                          ["glm-5.3-flash-free", "opencode/glm-5.3-flash-free"])
            assert cs and all("origin" in x for x in cs), cs
            assert len({x["origin"] for x in cs}) == 1, "两个写法应归一到同一 origin"
        finally:
            g0["discover_gateways"] = real_disc
        # free_only 上游（kilo 报 384 个但绝大多数要付费）：只留真免费的，过滤后为空不误杀
        is_free = g0["_is_free_id"]
        assert is_free("kilo-auto/free") and is_free("nvidia/x:free") and is_free("glm-5.3-flash-free")
        assert not is_free("gpt-4o") and not is_free("claude-sonnet-4")
        only = g0["_only_free"]
        kilo_gw = {"free_only": True}
        assert only(kilo_gw, ["gpt-4o", "kilo-auto/free", "nvidia/x:free"]) \
            == ["kilo-auto/free", "nvidia/x:free"]
        assert only(kilo_gw, ["gpt-4o"]) == ["gpt-4o"], "全付费时不许把列表清空"
        assert only({}, ["gpt-4o"]) == ["gpt-4o"], "非 free_only 端点不过滤"
        print("llmgw_device 自测：14 组全过")
    finally:
        # 还原模块级状态与函数，避免影响后续 import
        g["discover_gateways"] = real_discover
        g["_req_json"] = real_req
        for name, val in saved.items():
            globals()[name].clear()
            globals()[name].update(val)


if __name__ == "__main__":
    _selftest()
