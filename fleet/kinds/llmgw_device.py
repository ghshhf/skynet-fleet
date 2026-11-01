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

能力（13 项）：
  llmgw.endpoints   — 列出所有可用网关（OFM/zen-gate/匿名/codex/带 key）+ 健康探测
  llmgw.models      — 按网关列出模型清单（含 family 分类）
  llmgw.chat        — OpenAI 兼容 chat，跨网关·跨模型 fallback + cooldown + bandit 路由 + hedge 并发
                      stream=true 走 SSE：首字节前失败换上游，首字节后断流保留已得内容
  llmgw.embeddings  — embeddings，家族内 failover（不同模型向量不混）
  llmgw.probe       — 单模型首字延迟（TTFB）
  llmgw.cooldowns   — 查看当前冷却中的端点/模型（带 source 标记）
  llmgw.sticky      — 查看/清除粘性会话
  llmgw.compress    — 网关层上下文压缩（省 token、少撞上下文上限）
  llmgw.health      — 可用性图谱（各端点模型实时健康状态，坏的降权/过滤 → 模型随时可用）
  llmgw.quota       — 额度总览（借用 OFM/zen-gate 自己探上游的结论 + 自建健康图谱，零配额成本）
  llmgw.discover    — new-api 系公益站公开元数据（模型/倍率/赠送额度/投放窗口，零登录零额度）
  llmgw.codex_login — ChatGPT 订阅号 device-code 授权（无浏览器）；连上后 codex 后端进路由池
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
from .. import codex_auth as _codex


def _codex_access(force_refresh=False):
    """取 Codex 订阅号的 access token + account_id（快过期会自动续）。"""
    try:
        return _codex.access_and_account(force_refresh=force_refresh)
    except Exception as e:
        return None, None, str(e)[:120]


def _codex_headers(account_id):
    try:
        return _codex.headers(account_id)
    except Exception:
        return {}


def _codex_gateway():
    """已连上 ChatGPT 订阅号时，把 codex 后端当一个真实上游加进池子。"""
    try:
        stt = _codex.status()
    except Exception:
        return None
    if not stt.get("connected"):
        return None
    return {"name": "codex", "base": _codex.CODEX_BASE, "key": "", "anon": False,
            "auth": "codex", "api_style": "responses",
            "responses_path": "/responses",
            "ua": _codex.USER_AGENT,
            # 订阅是包月固定成本 → 边际成本接近 0，但仍排在本机网关之后：
            # 号池有风控，能走本机网关就不去撞它。
            "cost": 0.05,
            "default_model": None,
            "note": "ChatGPT 订阅号直连（chatgpt.com/backend-api/codex，Responses 协议）。"
                    "凭据是自己账号的 device-code OAuth，走 llmgw.codex_login 授权。"}

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
# 这两家自己探上游后落下的结论（只读，不发请求）→ 见「外部额度快照」
OFM_AVAILABILITY = os.path.join(os.path.expanduser("~"), ".our-free-model", "availability.json")
ZENGATE_QUOTA = os.path.join(os.environ.get("APPDATA") or "", "zen-gate", "quota.json")

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

# 带 key 的额度型上游（new-api / one-api 系聚合站）。额度来自账号（注册赠送 + 每日签到 +
# 邀请返利），不是匿名免费。用完会返回 402 "Budget pool quota has been exhausted." ——
# 那是「这一窗的额度用完了」，**不是**「上游死了」，所以下面给它独立的 quota 语义：
# 冷却到下一个投放窗口，且不进 model bench（模型本身没问题，明天照用）。
KEYED_ENDPOINTS = [
    {"name": "agentrouter", "base": "https://agentrouter.org/v1",
     "env": "LLMW_AGENTROUTER_KEY", "secret": "agentrouter",
     "default_model": "gpt-5.6-sol", "cost": 1.0,
     "models": ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-astra", "claude-opus-5-5"],
     # 两道闸，实测出来的（11-01）：
     #  ① 客户端指纹白名单：curl / 浏览器 UA 一律 401 "unauthorized client detected"，
     #     换成 codex / claude-code / cline 的 UA 立刻 200 —— 它靠 UA 挡「公开代理」，
     #     但认不出来就是认不出来，这不是加密，只是约定。
     #  ② 协议只吃 Responses：打 /v1/chat/completions 报 "does not support, please use
     #     /v1/responses" —— 因为上游本来就是 Codex 订阅后端，原生说 Responses。
     "api_style": "responses",
     "ua": "codex_cli_rs/0.51.0 (Windows 11; x86_64) Windows",
     # 可替代的客户端 UA（同一把钥匙，任选其一即可过①）
     "ua_alts": ["claude-cli/2.1.7 (external, cli)", "Cline/3.9.0"],
     "note": "AgentRouter：new-api 部署的公益站（响应头 X-Oneapi-Request-Id 可证）。"
             "注册 $50、邀请再 $100、每日签到约 $20、官方明示无在线充值（enable_online_topup=false）。"
             "只有 4 个模型，全是 GPT 订阅号池 / Claude 号池转出来的，不是官方按量 API："
             "gpt-5.6-sol(倍率2) gpt-6.1-sol(2) gpt-6-astra(4) claude-opus-5-5(3, 仅 test/svip 分组)。"
             "注意 co.agentrouter.org 是文档写的接入域名，但与主站的令牌库**不是同一套**（实测 401），"
             "要用 https://agentrouter.org/v1。"},
]
# 密钥来源：环境变量 → ~/.fleet/upstreams.json（不进 git、不进日志、不进能力输出）
UPSTREAMS_SECRET = os.path.join(os.path.expanduser("~"), ".fleet", "upstreams.json")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _keyed_key(ep):
    """取带 key 上游的凭据：环境变量优先，其次 ~/.fleet/upstreams.json。
    没配置就返回空串 → 该上游不进候选（不占 hedge 名额，也不假装能用）。"""
    v = (os.environ.get(ep.get("env") or "", "") or "").strip()
    if v:
        return v
    f = _load_json(UPSTREAMS_SECRET)
    if isinstance(f, dict):
        return str(f.get(ep.get("secret") or ep.get("name")) or "").strip()
    return ""


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
                    "cost": float(a.get("cost") or 0.0),
                    "note": a.get("note", "")})
    # 带 key 的额度型上游：只有真拿到凭据才加入（没 key 就静默跳过，不假装可用）
    for e in KEYED_ENDPOINTS:
        k = _keyed_key(e)
        if not k:
            continue
        gws.append({"name": e["name"], "base": e["base"], "key": k, "anon": False,
                    "default_model": e.get("default_model"),
                    "models": list(e.get("models") or []),
                    "cost": float(e.get("cost") or 0.0),
                    "keyed": True,
                    "api_style": e.get("api_style") or "chat",
                    "ua": e.get("ua") or "",
                    "note": e.get("note", "")})
    # Codex 订阅号直连（只有真授权过才加，没连就静默跳过）
    cg = _codex_gateway()
    if cg:
        gws.append(cg)
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

# 成本权重：路由偏好「不烧额度 / 不烧钱的先上」。cost=0 的匿名端点因子恒为 1（老排序一字不变），
# 只有显式声明成本的上游（cost>0，例如 AgentRouter 这类额度型聚合站）才被折价。
COST_WEIGHT = 0.5

# cooldown-probe：只探测 heuristic cooldown，不碰 authoritative/quota/credit/tier
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
# 「额度窗用尽」标记：这类错误说明的是**这一窗的预算没了**，不是端点坏了。
#   AgentRouter(new-api) 402 → "Budget pool quota has been exhausted."
#   OFM 免费通道 → "Free usage limit reached. Free models work in OpenCode"
# 混成 rate 会走 2m→10m→1h→1d 的阶梯（越撞越久，但明天额度回来了还得等 1 天）；
# 混成 5xx 会进 model bench（把好模型当坏模型关 10 分钟）。两者都是误伤。
_QUOTA_MARKERS = (
    "quota has been exhausted", "insufficient_quota", "insufficient quota",
    "exceeded your current quota", "free usage limit reached", "budget pool",
    "quota exhausted", "no remaining quota", "out of credits", "daily limit reached",
    "额度已用完", "额度不足", "余额不足", "配额已",
)


def _looks_like_quota_window(low):
    return any(m in low for m in _QUOTA_MARKERS)


def _classify_error(status_code, err_text):
    """把 HTTP 状态 + 错误体分类为：
    auth / credit / quota / forbidden / rate / server / timeout / context / transport / other"""
    low = (err_text or "").lower()
    if status_code == 0:
        if "timeout" in low or "timed out" in low:
            return "timeout"
        return "transport"
    if status_code == 402:
        return "quota"
    if status_code in (401, 403):
        if "credit" in low or "balance" in low or "quota" in low or "payment" in low:
            return "credit"
        if status_code == 403:
            return "forbidden"
        return "auth"
    if status_code == 429 or "rate" in low or "quota" in low:
        if status_code == 429 and _looks_like_quota_window(low):
            return "quota"
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


def _reason_for(cls):
    """错误分类 → cooldown 原因。chat / embeddings / probe 三处共用一份映射，免得漂移。"""
    return {"auth": "auth", "credit": "credit", "quota": "quota",
            "forbidden": "forbidden", "tier": "tier", "rate": "429",
            "server": "5xx", "context": "context", "timeout": "timeout",
            "transport": "timeout", "client": "429", "other": "5xx"}.get(cls, "5xx")


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


# ---------- 外部额度快照（借用 OFM / zen-gate 自己探过的结论，零配额成本） ----------
# 这两家软件都在主动探上游、并把结论写在本地：
#   OFM      `availability.json` → {results: {model: {state, detail, latencyMs, at}}}
#   zen-gate `quota.json`        → {model: {throttledAt, cooldownUntil, lastOk, episodes}}
# 免费层每打一次就是实打实烧配额，既然它们已经探过「这个模型现在额度尽 / 国家封」，
# 我们就直接用，不必再撞一次必败的墙。只读文件、不发任何请求；缓存 20s。
_EXT_CACHE = {"at": {}, "ts": 0.0, "ttl": 20.0}
# 这些 state 表示「现在打必败」，可安全重度降权
_EXT_DEAD = {"throttled", "region-blocked", "unavailable", "deprecated"}
# 外部记录可能很久不更新（OFM 不是常探），超过这个时长就不拿它拦路
EXT_STALE_S = 1800


def _ext_snapshot(force=False):
    """返回 {模型身份: {state, src, until, at, detail}}，身份用 `_model_identity` 归一。

    归一是关键：OFM/zen-gate 记的是裸名（`glm-5.3-flash-free`），候选里可能带厂商前缀
    （`opencode/glm-5.3-flash-free`）——同一把尺归一后才能对上。
    """
    now = _now()
    c = _EXT_CACHE
    if not force and c["ts"] and (now - c["ts"]) < c["ttl"]:
        return c["at"]
    at = {}
    avail = _load_json(OFM_AVAILABILITY)
    if isinstance(avail, dict):
        for mid, v in (avail.get("results") or {}).items():
            if not isinstance(v, dict):
                continue
            ident = _model_identity(mid)
            if not ident:
                continue
            at[ident] = {"state": (v.get("state") or "unknown"),
                         "src": "ofm",
                         "at": (v.get("at") or 0) / 1000.0,
                         "detail": str(v.get("detail") or "")[:200]}
    q = _load_json(ZENGATE_QUOTA)
    if isinstance(q, dict):
        for mid, v in q.items():
            if not isinstance(v, dict):
                continue
            ident = _model_identity(mid)
            if not ident:
                continue
            until = (v.get("cooldownUntil") or 0) / 1000.0
            last_ok = (v.get("lastOk") or 0) / 1000.0
            prev = at.get(ident)
            if until > now:
                # zen-gate 的冷却时间是它自己算的，比 OFM 的 state 更有时效性 → 覆盖
                at[ident] = {"state": "throttled", "src": "zen-gate", "until": until,
                             "at": last_ok or now,
                             "detail": "zen-gate 记录：冷却中，还需 %.0f 分钟"
                                       % ((until - now) / 60.0)}
            elif prev is None:
                at[ident] = {"state": "ok" if last_ok else "unknown",
                             "src": "zen-gate", "at": last_ok,
                             "detail": "zen-gate 有成功记录" if last_ok else ""}
    c["at"] = at
    c["ts"] = now
    return at


def _ext_verdict(model):
    """外部快照对单个模型的判定 → (dead, weight, src, detail)。

    dead=True：按已有记录现在打它大概率白烧预算（额度尽/国家封/已下架），但**不完全丢弃**
    （weight 降到 0.02 仍留在候选尾部兜底，免得外部记录本身出错时无路可走）。
    """
    rec = _ext_snapshot().get(_model_identity(model))
    if not rec:
        return False, 1.0, None, ""
    st = rec.get("state")
    now = _now()
    born = rec.get("at") or 0
    stale = bool(born) and (now - born) > EXT_STALE_S
    if st in _EXT_DEAD:
        until = rec.get("until") or 0
        if until and until <= now:          # 冷却已过 → 让它自己自证，不再拦
            return False, 1.0, rec.get("src"), ""
        if stale:                            # 记录太老 → 只轻微倾向，别拿旧结论压坏活路
            return False, 0.9, rec.get("src"), rec.get("detail") or st
        return True, 0.02, rec.get("src"), rec.get("detail") or st
    if st == "unknown":
        return False, 0.5, rec.get("src"), rec.get("detail") or "unknown"
    return False, 1.0, rec.get("src"), ""


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
# 额度投放窗口：new-api 系公益站的额度是**按窗重置**的。AgentRouter 官方公告明示
# 每天北京时间 9:00 / 10:00 / 19:00 分批投放、用完即止，另有 0 点自然日重置。
# 所以撞到「额度尽」时不该按 429 阶梯（2m→10m→1h→1d）熬，而是「等到下一个窗口」。
QUOTA_WINDOWS_HOURS = (0, 9, 10, 19)


def _next_quota_window(now=None):
    """下一个额度投放窗口的时间戳（本地时区；本机 = 北京时间）。"""
    t = now if now is not None else _now()
    lt = time.localtime(t)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    for h in QUOTA_WINDOWS_HOURS:
        cand = base + h * 3600
        if cand > t + 60:
            return cand
    return base + 24 * 3600


def _quota_window_seconds():
    """到下一个投放窗口还有多久（夹在 5 分钟 ~ 12 小时之间）。"""
    now = _now()
    return int(max(300, min(_next_quota_window(now) - now, 12 * 3600)))


def _cooldown_decision(gw, model, reason, retry_after_s=None):
    """按原因分派 cooldown 时长 + source 标记。
    source: heuristic（可被 cooldown-probe 早期恢复）/ authoritative（供应商 Retry-After，不碰）
            / credit（402 欠费，不碰）/ quota（额度窗用尽，等到下个投放窗口，不碰）
            / tier（403 模型不在该层，不碰）
    """
    now = _now()
    k = _ckey(gw, model)

    # 本地端点豁免（loopback / LAN）：无供应商配额，短 bench
    if gw in ("ofm", "zen-gate") and "127.0.0.1" in _gw_base(gw):
        return {"seconds": 5, "source": "heuristic"}

    if reason == "auth":
        return {"seconds": 300, "source": "authoritative"}
    if reason == "quota":
        # 额度窗用尽：等到下一个投放窗口，不升级、不 bench（不是模型坏，是预算空）
        return {"seconds": _quota_window_seconds(), "source": "quota"}
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


# ---------- OpenAI Responses API 适配层（Codex 系订阅后端原生协议）----------
# 为什么要这层：AgentRouter 这类站的上游是 Codex 订阅后端，它原生只说 Responses。
# 打 /v1/chat/completions 直接被拒（"does not support ... please use /v1/responses"）。
# 天网内部一律用 chat 语义，所以在这里做双向翻译，上层调用方完全无感。
def _to_resp_input(messages):
    """chat messages → (instructions, input[])。

    Responses 把 system 放在顶层 instructions，input 只放对话轮次；
    且 content 是带类型的对象数组：user → input_text，assistant → output_text。
    类型写错会被上游 400，不是格式美化问题。
    """
    sys_parts, turns = [], []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        c = m.get("content")
        if isinstance(c, list):  # 多模态：只取文本段，图片段原样保留
            texts, others = [], []
            for seg in c:
                if isinstance(seg, dict) and seg.get("type") in ("text", "input_text"):
                    texts.append(str(seg.get("text") or ""))
                elif isinstance(seg, dict) and seg.get("type") == "image_url":
                    others.append({"type": "input_image",
                                   "image_url": (seg.get("image_url") or {}).get("url")
                                   if isinstance(seg.get("image_url"), dict) else seg.get("image_url")})
            c = "\n".join(texts)
            extra = others
        else:
            c = "" if c is None else str(c)
            extra = []
        if role == "system":
            sys_parts.append(c)
            continue
        typ = "output_text" if role == "assistant" else "input_text"
        content = ([{"type": typ, "text": c}] if c else []) + extra
        if not content:
            continue
        turns.append({"role": role, "content": content})
    return "\n\n".join(p for p in sys_parts if p), (turns or [{
        "role": "user", "content": [{"type": "input_text", "text": ""}]}])


def _from_resp_body(d):
    """Responses 返回 → chat 风格 body（让上层完全无感）。"""
    texts, reasoning = [], []
    for it in (d.get("output") or []):
        if not isinstance(it, dict):
            continue
        if it.get("type") == "reasoning":
            for sm in (it.get("summary") or []):
                if isinstance(sm, dict) and sm.get("text"):
                    reasoning.append(sm["text"])
            continue
        if it.get("type") != "message":
            continue
        for seg in (it.get("content") or []):
            if isinstance(seg, dict) and seg.get("type") in ("output_text", "text"):
                texts.append(str(seg.get("text") or ""))
    text = "".join(texts)
    if not text and isinstance(d.get("output_text"), str):
        text = d["output_text"]
    u = d.get("usage") or {}
    pt = int(u.get("input_tokens") or 0)
    ct = int(u.get("output_tokens") or 0)
    out = {
        "id": d.get("id"), "object": "chat.completion", "model": d.get("model"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop" if d.get("status") in (None, "completed") else str(d.get("status")),
        }],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": int(u.get("total_tokens") or (pt + ct))},
        "_style": "responses",
    }
    if reasoning:
        out["_reasoning"] = "\n".join(reasoning)[:2000]
    rt = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
    if rt:
        out["usage"]["reasoning_tokens"] = int(rt)
    return out


def _responses_once(gw, payload, timeout):
    """用 Responses 协议打一次，返回与 _chat_once 相同的 7 元组。"""
    instructions, turns = _to_resp_input(payload.get("messages") or [])
    codex = gw.get("auth") == "codex"
    req_body = {
        "model": payload.get("model"),
        "input": turns,
    }
    # Codex 后端不收 max_output_tokens / metadata（FCC 里叫 omitted_request_fields）
    if not codex:
        # reasoning 型上游会先烧 reasoning_tokens：预算给小了正文直接是空字符串
        # （实测 agentrouter gpt-5.6-sol max_tokens=60 → reasoning 吃满，content 为 ""）。
        # 所以这里给个下限，而不是把调用方的 max_tokens 原样透传。
        req_body["max_output_tokens"] = max(512, int(payload.get("max_tokens") or 1024))
    if instructions:
        req_body["instructions"] = instructions
    if payload.get("temperature") is not None:
        req_body["temperature"] = payload["temperature"]
    if payload.get("stream"):
        req_body["stream"] = True
    hdrs = {"Accept": "application/json"}
    key = gw.get("key")
    if codex:
        tok, acc, err_c = _codex_access()
        if not tok:
            return None, err_c or "codex 未连接", None, 0, "auth", "auth", None
        key = tok
        hdrs.update(_codex_headers(acc))
    elif gw.get("ua"):
        hdrs["User-Agent"] = gw["ua"]
    path = gw.get("responses_path") or "/v1/responses"
    st, hd, body, err0 = _req_json(gw["base"], path, key,
                                   timeout=timeout, method="POST", body=req_body,
                                   extra_headers=hdrs)
    if err0 and st == 0:
        return None, err0, None, 0, "transport", "timeout", None
    if st == 200 and isinstance(body, dict) and (body.get("output") is not None
                                                 or body.get("output_text")):
        chat = _from_resp_body(body)
        chat["_routed_via"] = (hd or {}).get("X-Routed-Via") or gw["name"]
        tokens = (chat.get("usage") or {}).get("total_tokens", 0)
        return chat, None, chat["_routed_via"], tokens, "ok", "ok", None
    err = _extract_err(body)
    if not err:
        err = "HTTP %s" % st
    cls = _classify_error(st, err)
    reason = _reason_for(cls)
    retry_after_s = None
    if st == 429:
        ra = (hd or {}).get("Retry-After") or (hd or {}).get("retry-after")
        if ra:
            try:
                retry_after_s = float(ra)
            except ValueError:
                pass
    return None, err, None, 0, cls, reason, retry_after_s


# ---------- SSE 流式（首字节前可换上游，首字节后不重试，保住已得内容）----------
# 语义要点：mid-stream 故障转移不能靠「重发」——重发会让调用方看到重复内容。
# 所以分界线是**首字节**：
#   首字节前出错 → 上游根本没开始输出，等价于普通失败，可以换下一个候选；
#   首字节后出错 → 已经吐了的字收不回来，把 partial 连同错误一起交上去，
#                  由调用方决定是续写还是丢弃（天网默认保留，partial=True 标记）。
def _sse_once(gw, payload, timeout, on_delta=None):
    """流式打一次。返回 8 元组，末位是 meta(ttft_s/chunks/partial)。

    同时认两种事件格式：
      chat 风格      data: {"choices":[{"delta":{"content":"x"}}]}
      responses 风格 event: response.output_text.delta / data: {"delta":"x"}
    """
    style = gw.get("api_style") or "chat"
    if style == "responses":
        instructions, turns = _to_resp_input(payload.get("messages") or [])
        req_body = {"model": payload.get("model"), "input": turns, "stream": True}
        # 同 _responses_once：reasoning 型上游预算给小了正文会空，给下限
        if gw.get("auth") != "codex":
            req_body["max_output_tokens"] = max(512, int(payload.get("max_tokens") or 1024))
        if instructions:
            req_body["instructions"] = instructions
        if payload.get("temperature") is not None:
            req_body["temperature"] = payload["temperature"]
        path = "/v1/responses"
    else:
        req_body = dict(payload)
        req_body["stream"] = True
        path = "/v1/chat/completions"

    url = _join_url(gw["base"], path)
    host = (_urlparse.urlparse(gw["base"]).hostname or "").lower()
    local = host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
    handlers = []
    if not local:
        px = _egress_proxy_addr()
        if px:
            handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
        elif not _dns_ok(host):
            return (None, "域名 %s 解析/不可达（DNS 预筛）" % host, None, 0,
                    "transport", "timeout", None,
                    {"ttft_s": None, "chunks": 0, "partial": False})
    handlers += [urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()]
    opener = urllib.request.build_opener(*handlers)
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if gw.get("key"):
        headers["Authorization"] = "Bearer " + gw["key"]
    if gw.get("ua"):
        headers["User-Agent"] = gw["ua"]
    req = urllib.request.Request(url, data=json.dumps(req_body).encode("utf-8"),
                                 headers=headers, method="POST")

    parts, reasoning, chunks = [], [], 0
    ttft = None
    t0 = _now()
    usage = {}
    try:
        with opener.open(req, timeout=timeout) as r:
            via = r.headers.get("X-Routed-Via") or gw["name"]
            first = True
            while True:
                line = r.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if not s or s.startswith(":"):
                    continue
                if not s.startswith("data:"):
                    continue
                seg = s[5:].strip()
                if seg == "[DONE]":
                    break
                try:
                    ev = json.loads(seg)
                except Exception:
                    continue
                if isinstance(ev.get("usage"), dict) and ev["usage"]:
                    usage = ev["usage"]
                # chat 风格
                for ch in (ev.get("choices") or []):
                    d = (ch or {}).get("delta") or {}
                    piece = d.get("content")
                    if piece:
                        if first:
                            ttft = round(_now() - t0, 3)
                            first = False
                        parts.append(piece)
                        chunks += 1
                        if on_delta:
                            try:
                                on_delta(piece)
                            except Exception:
                                pass
                    if d.get("reasoning_content"):
                        reasoning.append(d["reasoning_content"])
                # responses 风格
                typ = ev.get("type") or ""
                if typ.endswith("output_text.delta"):
                    piece = ev.get("delta") or ""
                    if piece:
                        if first:
                            ttft = round(_now() - t0, 3)
                            first = False
                        parts.append(piece)
                        chunks += 1
                        if on_delta:
                            try:
                                on_delta(piece)
                            except Exception:
                                pass
                elif typ.endswith("reasoning_summary_text.delta"):
                    reasoning.append(ev.get("delta") or "")
                elif typ == "response.completed":
                    usage = (ev.get("response") or {}).get("usage") or usage
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        err = raw or ("HTTP %s" % e.code)
        try:
            j = json.loads(raw)
            err = _extract_err(j) or err
        except Exception:
            pass
        cls = _classify_error(e.code, err)
        return (None, err, None, 0, cls, _reason_for(cls),
                _retry_after_of(e.headers), {"ttft_s": ttft, "chunks": chunks,
                                             "partial": bool(parts)})
    except Exception as e:
        # 首字节前挂 = 普通失败（可换上游）；首字节后挂 = 保住 partial，不重试
        if not parts:
            return (None, str(e)[:200], None, 0, "transport", "timeout", None,
                    {"ttft_s": ttft, "chunks": 0, "partial": False})
        return (_sse_body(parts, reasoning, usage, gw, payload), None, gw["name"], 0,
                "ok", "ok", None,
                {"ttft_s": ttft, "chunks": chunks, "partial": True,
                 "err": str(e)[:200]})

    if not parts:
        return (None, "流结束但没有内容（上游返回空流）", None, 0, "server", "5xx", None,
                {"ttft_s": ttft, "chunks": 0, "partial": False})
    body = _sse_body(parts, reasoning, usage, gw, payload)
    tk = int((usage or {}).get("total_tokens") or 0)
    return (body, None, gw["name"], tk, "ok", "ok", None,
            {"ttft_s": ttft, "chunks": chunks, "partial": False})


def _retry_after_of(headers):
    try:
        ra = headers.get("Retry-After") or headers.get("retry-after")
        return float(ra) if ra else None
    except Exception:
        return None


def _sse_body(parts, reasoning, usage, gw, payload):
    """把流式碎片拼成 chat 风格 body（与 _chat_once 成功返回同构，上层无感）。"""
    u = usage or {}
    pt = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
    ct = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
    out = {
        "id": "stream-" + str(int(_now())), "object": "chat.completion",
        "model": payload.get("model"), "_style": gw.get("api_style") or "chat",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "".join(parts)},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": int(u.get("total_tokens") or (pt + ct))},
    }
    if reasoning:
        out["_reasoning"] = "".join(reasoning)[:2000]
    return out


# ---------- chat 单次尝试 ----------
def _chat_once(gw, payload, timeout):
    """统一返回 7 元组：(body|None, err, via, tokens, cls, reason, retry_after_s)。

    成功：body 非 None，err=None，cls/reason="ok"；
    传输错（st==0）：body=None，cls="transport"，reason="timeout"；
    HTTP 错：body=None，cls/reason 由 _classify_error 决定。
    """
    if gw.get("api_style") == "responses":
        return _responses_once(gw, payload, timeout)
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
    reason = _reason_for(cls)
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
_IDENTITY_TAIL = re.compile(r"((?::|/|-)(?:free|latest))+$", re.I)


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
            # 外部额度快照：OFM/zen-gate 已经探过「额度尽/国家封」，直接用它的结论降权，
            # 免得我们再打一次必败的请求把免费配额烧掉。
            ext_dead, ext_w, ext_src, ext_detail = _ext_verdict(m)
            if ext_w != 1.0:
                score *= ext_w
            # 成本折价：免费匿名端点 cost=0 → 不折；额度/付费型上游按 cost 打折，
            # 同分情况下免费的先上（额度留给真正没别的路的时刻）
            cost = float(g.get("cost") or 0.0)
            if cost > 0:
                score *= 1.0 / (1.0 + COST_WEIGHT * cost)
            candidates.append({
                "gw": g, "model": m, "score": score, "origin": origin,
                "cooldown_source": source, "healthy": healthy,
                "ext_dead": ext_dead, "ext_src": ext_src,
                "ext_detail": ext_detail, "cost": cost,
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
        via = "models-endpoint" if raw_ids else "none"
        if not ids and g.get("models"):
            # 声明式模型表（带 key 的额度型上游）：/v1/models 若不可读，按声明的清单报，
            # 但 via 标 declared —— 清单不等于可用性承诺，可用性由 chat/健康图谱负责
            ids = list(g["models"])
            via = "declared"
        if not ids and g.get("default_model"):
            # /v1/models 未实现或返回空（如 pollinations 在 openai 基址下返回 200 空列表）
            # → 用已知默认模型兜底，仍可用
            ids = [g["default_model"]]
            via = "default_model"
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
            # 关键：免费端点（cost=0）永远排在额度型前面 —— 否则一旦额度型上游刚成功过，
            # bandit 分会把它顶到默认位，之后每次默认调用都在薅同一只羊。
            picks.sort(key=lambda t: (float(t[1].get("cost") or 0) > 0,
                                      -bandit_score(t[1]["name"], t[0])))
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

    # 免费优先，额度型只做兜底 —— 不逮着一只羊薅。
    # 第一轮（hedge/流式）只发 cost==0 的免费端点；它们全挂了，串行 fallback
    # 才会轮到带 key 的额度型上游。显式 endpoint= 指定时不做这层过滤（你点名要谁就是谁）。
    free_c = [c for c in candidates if not (c["gw"].get("cost") or 0) > 0]
    allow_paid = bool(endpoint_pref) or not free_c or args.get("allow_paid") is True
    first_round = candidates if allow_paid else free_c

    # 流式：不并发（两路流没法合流），按分串行；首字节前失败换下一个候选。
    sv = args.get("stream")
    if sv is True or (isinstance(sv, str) and sv.lower() in ("1", "true", "on", "yes")):
        return _chat_stream_serial(cap, dev, first_round, payload_base, timeout,
                                   session, comp_saved,
                                   deeper=candidates if not allow_paid else None)

    # hedge 并发：对前 N 个候选并发发送（单次尝试有超时上限，不阻塞退出）
    res, chosen_idx, tried, results = _hedge_send(first_round, payload_base, timeout)

    if res[0] is not None:
        body, err, via, tokens, cls, reason, retry_after = res
        c = first_round[chosen_idx]
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
        c = first_round[idx]
        g = c["gw"]
        m = c["model"]
        err = rr[1] or ""
        reason = rr[5] or "5xx"
        attempts.append({"gateway": g["name"], "model": m,
                         "err": (err or "")[:150], "latency_s": 0,
                         "cooldown_source": c.get("cooldown_source"), "via": "hedge"})
        record_failure(g["name"], m, err_text=err, reason=reason, retry_after_s=rr[6])
        _update_health(g["name"], m, False)

    # 全部失败：尝试串行 fallback 剩余候选（跳过 hedge 已试过的，避免重复打）。
    # 这里用全量 candidates —— 免费端点全挂时，额度型上游在这一步才被轮到（兜底不抢跑）。
    serial_timeout = min(timeout, HEDGE_ATTEMPT_TIMEOUT)
    tried_ids = {id(first_round[i]) for i in tried}
    for c in candidates:
        if id(c) in tried_ids:
            continue
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


def _chat_stream_serial(cap, dev, candidates, payload_base, timeout, session, comp_saved,
                        deeper=None):
    """流式路径：按 bandit 分串行试候选。

    分界线是首字节 —— 首字节前失败换下一个（等价普通 fallback）；
    首字节后失败不再重试，把已得内容作为 partial 交回（重试会让用户看到重复输出）。
    deeper=全量候选表：免费端点全在首字节前挂了，才轮到额度型上游兜底。
    """
    attempts = []
    queue = list(candidates)
    seen = {id(c) for c in queue}
    i = 0
    while i < len(queue):
        c = queue[i]
        i += 1
        if i == len(queue) and deeper:
            # 免费那批耗尽：追加额度型继续（同一轮里不重复打）
            for d2 in deeper:
                if id(d2) not in seen:
                    seen.add(id(d2))
                    queue.append(d2)
        g, m = c["gw"], c["model"]
        p = dict(payload_base)
        p["model"] = m
        t0 = _now()
        body, err, via, tk, cls, reason, ra, meta = _sse_once(
            g, p, min(timeout, HEDGE_ATTEMPT_TIMEOUT))
        lat = round(_now() - t0, 2)
        if body is not None:
            ttft = meta.get("ttft_s")
            record_success(g["name"], m, tokens=tk,
                           ttfb_ms=(ttft * 1000) if ttft else None)
            _update_health(g["name"], m, True)
            if session:
                with _LOCK:
                    _STICKY[session] = {"gw": g["name"], "model": m, "ts": _now()}
            data = {
                "result": body, "endpoint": g["name"], "base": g["base"],
                "model": m, "origin": c.get("origin"),
                "routed_via": body.get("_routed_via") or via or g["name"],
                "latency_s": lat, "fallback_attempts": attempts, "session": session,
                "cooldown_source": c.get("cooldown_source"),
                "compress_saved_chars": comp_saved,
                "stream": True, "ttft_s": ttft, "chunks": meta.get("chunks", 0),
                "partial": bool(meta.get("partial")),
            }
            if meta.get("partial"):
                # 中途断流：内容保留，错误如实上报，让调用方自己决定要不要续写
                data["stream_err"] = meta.get("err") or "mid-stream interrupted"
            return R.success(cap, dev.name, via=dev.via, data=data)

        record_failure(g["name"], m, err_text=err, reason=reason, retry_after_s=ra)
        _update_health(g["name"], m, False)
        attempts.append({"gateway": g["name"], "model": m, "err": (err or "")[:150],
                         "latency_s": lat, "cooldown_source": c.get("cooldown_source"),
                         "phase": "pre-first-byte"})

    return R.error(cap, "流式：全部候选在首字节前失败：%s" % attempts,
                   device=dev.name, via=dev.via)


def _chat_once_simple(gw, payload, timeout):
    """简化版 _chat_once，返回 (body, err, via, tokens, retry_after_s)。

    用于串行 fallback；多返回一个 retry_after_s（429 时从 Retry-After 头取），
    透传给 record_failure → set_cooldown 标记 authoritative。
    """
    if gw.get("api_style") == "responses":
        return _responses_once(gw, payload, timeout)
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
        reason = _reason_for(cls)
        record_failure(g["name"], model, err_text=err, reason=reason)
        # 二次冷却只为把 429/5xx 标成阶梯；quota 已经由 record_failure 设成窗口冷却，别再覆盖
        if cls != "quota":
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
            reason = _reason_for(cls)
            record_failure(g["name"], model, err_text=str(body)[:120], reason=reason)
            if cls != "quota" and st in (429, 500, 502, 503):
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


def _h_quota(cap, args, dev):
    """额度总览：外部快照（OFM / zen-gate 自己探过的结论）+ 自建健康图谱，合并成一张表。

    `refresh=True` 时强制重读外部文件（但**依然不发网络请求**）。
    """
    if args.get("refresh"):
        _ext_snapshot(force=True)
    ext = _ext_snapshot()
    now = _now()
    items = []
    dead_n = stale_n = ok_n = 0
    for ident, rec in sorted(ext.items()):
        st = rec.get("state")
        until = rec.get("until") or 0
        born = rec.get("at") or 0
        stale = bool(born) and (now - born) > EXT_STALE_S
        cd_s = max(0, int(round(until - now))) if until > now else 0
        is_dead = (st in _EXT_DEAD) and (cd_s > 0 or not until) and not stale
        if is_dead:
            dead_n += 1
        elif stale:
            stale_n += 1
        elif st == "ok":
            ok_n += 1
        items.append({
            "model": ident, "state": st, "source": rec.get("src"),
            "dead": bool(is_dead), "cooldown_s": cd_s, "stale": bool(stale),
            "detail": rec.get("detail") or "",
        })
    with _LOCK:
        health = [{
            "gateway": g, "model": (None if m == "*" else m),
            "healthy": bool(st.get("healthy", True))
                       or (st.get("ok") and (now - st["ok"]) < HEALTH_RECOVER_S),
            "consecutive_fail": st.get("consecutive", 0),
        } for (g, m), st in list(_HEALTH.items())]
    return R.success(cap, dev.name, via=dev.via, data={
        "external": {
            "items": items, "count": len(items),
            "dead": dead_n, "ok": ok_n, "stale": stale_n,
            "sources": {
                "ofm": {"path": OFM_AVAILABILITY, "readable": os.path.isfile(OFM_AVAILABILITY)},
                "zen_gate": {"path": ZENGATE_QUOTA, "readable": os.path.isfile(ZENGATE_QUOTA)},
            },
            "stale_s": EXT_STALE_S,
        },
        "local_health": {"items": health[:500], "count": len(health),
                         "healthy": sum(1 for x in health if x["healthy"])},
        # 额度型上游：报「配没配 key」「声明的模型」「成本系数」，绝不回显 key 本身
        "keyed_upstreams": [{
            "name": e["name"], "base": e["base"], "configured": bool(_keyed_key(e)),
            "models": list(e.get("models") or []), "cost": float(e.get("cost") or 0.0),
            "env": e.get("env"), "secret_file": UPSTREAMS_SECRET,
        } for e in KEYED_ENDPOINTS],
        # 额度投放窗口：撞到 402 的冷却时长就是「到下一个窗口」
        "quota_window": {
            "hours_local": list(QUOTA_WINDOWS_HOURS),
            "next_reset": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(_next_quota_window())),
            "reset_in_s": _quota_window_seconds(),
        },
        "note": "external = OFM/zen-gate 自己探上游后落下的结论（只读，零配额成本）；"
                "dead 的模型在候选里被降到 0.02 权重而非丢弃（外部记录可能过期）。"
                "quota 类错误（402 / 额度窗用尽）不是死亡：冷却到下一个投放窗口，不进 model bench。",
    })


# ---------- new-api 系公益站：公开元数据抓取（零登录、零额度） ----------
# new-api / one-api 有两张**无需鉴权**的接口，站点自己挂在公网上：
#   /api/status  → 站点名、版本、quota_per_unit、注册赠送(quota_for_invitee)、
#                  邀请返利(quota_for_inviter)、有没有在线充值、可用 OAuth、公告全文
#   /api/pricing → 每个模型的名字 + 倍率(model_ratio) + 补全倍率 + 可见分组
# 公告里还常写死投放窗口（AgentRouter 明示北京时间 9:00 / 10:00 / 19:00 分批投放），
# 正则扒出来就能喂给 quota 冷却，不用自己猜。
# 拿这些不花一分额度、不碰任何账号 —— 比自己注册一个个试更省，也比抄别人的清单更及时。
DEFAULT_NEWAPI_SITES = ["agentrouter.org"]
# 公告里两种写法都出现过：「北京时间 9:00」和「（对应 UTC 1:00）」，还有「北京时间10:00和19:00」。
# 所以按「同一段里以北京时间为准」来切：先把 UTC 那半截切掉再抓所有时刻；
# 只有通篇没提北京时间时才按 UTC+8 折算。实测乱抓会把 [9,10,19] 读成 [1,9,10]。
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")


def _extract_windows(text):
    t = text or ""
    if "北京时间" in t:
        head = t.split("UTC")[0]
        return {int(h) for h, _m in _TIME_RE.findall(head) if 0 <= int(h) <= 23}
    if "UTC" in t:
        tail = t.split("UTC", 1)[1]
        return {(int(h) + 8) % 24 for h, _m in _TIME_RE.findall(tail) if 0 <= int(h) <= 23}
    return set()


def _newapi_site(host, timeout=10):
    base = host if host.startswith("http") else "https://" + host
    base = base.rstrip("/")
    info, models, errs = {}, [], []
    st, _, body, err = _req_json(base, "/api/status", None, timeout=timeout)
    d = (body or {}).get("data") if isinstance(body, dict) else None
    if st == 200 and isinstance(d, dict):
        try:
            qpu = float(d.get("quota_per_unit") or 500000)
        except Exception:
            qpu = 500000.0

        def _usd(q):
            try:
                return round(float(q or 0) / qpu, 2)
            except Exception:
                return None

        anns = [str(a.get("content") or "") for a in (d.get("announcements") or [])
                if isinstance(a, dict)]
        windows = set()
        for t in anns:
            windows |= _extract_windows(t)
        windows = sorted(windows)
        info = {
            "system_name": d.get("system_name"),
            "version": d.get("version"),
            "quota_per_unit": qpu,
            "price_cny_per_usd": d.get("price"),
            "signup_usd": _usd(d.get("quota_for_invitee")),
            "inviter_usd": _usd(d.get("quota_for_inviter")),
            "topup_enabled": bool(d.get("enable_online_topup") or d.get("enable_stripe_topup")),
            "oauth": [k[:-len("_oauth")] if k.endswith("_oauth") else k
                      for k in ("github_oauth", "linuxdo_oauth", "wechat_login",
                                "oidc_enabled", "telegram_oauth") if d.get(k)],
            "quota_drop_windows_h": windows,
            "announcements": [a[:240] for a in anns[:3]],
        }
    else:
        errs.append("status: %s" % (err or ("HTTP %s" % st)))
    st2, _, body2, err2 = _req_json(base, "/api/pricing", None, timeout=timeout)
    rows = (body2 or {}).get("data") if isinstance(body2, dict) else None
    if st2 == 200 and isinstance(rows, list):
        for m in rows:
            if not isinstance(m, dict):
                continue
            models.append({"model": m.get("model_name"),
                           "ratio": m.get("model_ratio"),
                           "completion_ratio": m.get("completion_ratio"),
                           "groups": m.get("enable_groups") or []})
    else:
        errs.append("pricing: %s" % (err2 or ("HTTP %s" % st2)))
    return {"site": base, "ok": bool(info or models), "info": info,
            "models": models, "model_count": len(models), "errors": errs}


def _h_discover(cap, args, dev):
    """扫 new-api 系站点的公开元数据：模型清单、倍率、赠送额度、有无充值、投放窗口。

    只读公开接口，不需要账号也需要 key —— 用来「先看清楚再决定要不要注册」。
    """
    sites = args.get("sites") or DEFAULT_NEWAPI_SITES
    if isinstance(sites, str):
        sites = [sites]
    tmo = int(args.get("timeout") or 10)
    out = [_newapi_site(s, timeout=tmo) for s in sites]
    return R.success(cap, dev.name, via=dev.via, data={
        "sites": out, "count": len(out), "ok": sum(1 for x in out if x["ok"]),
        "note": "new-api 的 /api/status 与 /api/pricing 无需鉴权 → 零登录、零额度即可拿到"
                "模型/倍率/赠送额度/有无充值/投放窗口。这里只读公开元数据，不碰账号与 key。",
    })


def _h_codex_login(cap, args, dev):
    """ChatGPT 订阅号的 device-code 授权：无浏览器、一条能力走完全程。

    action=start  → 拿 user_code，你去 verification_url 输码
    action=poll   → 轮询换 token（403/404 = 还没输完，不算错）
    action=status → 看连没连、多久过期
    action=logout → 撤销并删除本地凭据
    """
    action = (args.get("action") or "status").lower()
    if action in ("start", "login", "begin"):
        r = _codex.login_start()
    elif action in ("poll", "wait", "finish"):
        r = _codex.login_poll(wait_s=int(args.get("wait") or 0))
    elif action in ("status", "state"):
        r = _codex.status()
    elif action in ("logout", "revoke", "disconnect"):
        r = _codex.logout()
    else:
        return R.error(cap, "action 只能是 start/poll/status/logout", device=dev.name,
                       via=dev.via)
    if action in ("status", "state"):
        g = _codex_gateway()
        r["gateway_active"] = bool(g)
    note = ("凭据只存在本机 ~/.fleet/codex_auth.json，能力输出永不回显 token。"
            "Codex 后端要求 originator + ChatGPT-Account-ID 两个头，"
            "并且不接受 max_output_tokens / metadata —— 这些已在适配层处理。")
    return R.success(cap, dev.name, via=dev.via, data={"action": action, **r, "note": note})


_HANDLERS = {
    "llmgw.endpoints": _h_endpoints,    "llmgw.models": _h_models,
    "llmgw.chat": _h_chat,
    "llmgw.embeddings": _h_embeddings,
    "llmgw.probe": _h_probe,
    "llmgw.cooldowns": _h_cooldowns,
    "llmgw.sticky": _h_sticky,
    "llmgw.compress": _h_compress,
    "llmgw.egress": _h_egress,
    "llmgw.health": _h_health,
    "llmgw.quota": _h_quota,
    "llmgw.discover": _h_discover,
    "llmgw.codex_login": _h_codex_login,
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

        # 同源归并：同一模型的两种写法（带厂商前缀 / 带 :free 后缀）必须归一成同一 origin；
        # 但不能把「不同上游的 free 池」也归并成一个 —— `kilo-auto/free` 与
        # `openrouter/free` 砍掉尾缀后剩 `kilo-auto` / `openrouter`，不许都变成 `free`。
        assert g0["_model_identity"]("kilo-auto/free") == "kilo-auto", g0["_model_identity"]("kilo-auto/free")
        assert g0["_model_identity"]("openrouter/free") == "openrouter"
        assert g0["_model_identity"]("kilo-auto/free") != g0["_model_identity"]("openrouter/free"), \
            "不同 free 池不许撞成同一身份"
        # ⑮ 外部额度快照（借 OFM/zen-gate 的结论，零配额成本）
        real_load = g0["_load_json"]
        now_ms = int(g0["_now"]() * 1000)
        fake_files = {}
        def _fake_load(path):
            return fake_files.get(os.path.basename(str(path)))
        g0["_load_json"] = _fake_load
        try:
            fake_files["availability.json"] = {"results": {
                # 裸名 throttled → 应能匹配带前缀的候选写法（归一后才对得上）
                "glm-5.3-flash-free": {"state": "throttled", "at": now_ms,
                                       "detail": "Free usage limit reached."},
                "beam-free": {"state": "region-blocked", "at": now_ms,
                              "detail": "not available in your country"},
                "mimo-v2.5-free": {"state": "unknown", "at": now_ms,
                                   "detail": "deprecated"},
                # 很久以前探的（超过 EXT_STALE_S）→ 不该拦路
                "old-model-free": {"state": "throttled",
                                   "at": now_ms - 3 * 3600 * 1000, "detail": "old"},
            }}
            fake_files["quota.json"] = {
                "cooldown-model": {"cooldownUntil": now_ms + 600 * 1000, "lastOk": now_ms},
                "expired-model": {"cooldownUntil": now_ms - 60 * 1000, "lastOk": now_ms},
            }
            g0["_EXT_CACHE"].update(at={}, ts=0.0)
            dead, w, src, _ = g0["_ext_verdict"]("opencode/glm-5.3-flash-free")
            assert dead and w < 0.05 and src == "ofm", (dead, w, src)
            assert g0["_ext_verdict"]("nvidia/glm-5.3-flash:free")[0], "不同写法也要命中"
            assert g0["_ext_verdict"]("beam-free")[0], "region-blocked 也算死"
            assert not g0["_ext_verdict"]("mimo-v2.5-free")[0], "unknown 只降权不判死"
            assert g0["_ext_verdict"]("mimo-v2.5-free")[1] < 1.0
            assert not g0["_ext_verdict"]("old-model-free")[0], "陈旧记录不该拦路"
            assert g0["_ext_verdict"]("cooldown-model")[0], "zen-gate 未过冷却要拦"
            assert not g0["_ext_verdict"]("expired-model")[0], "冷却已过不该拦"
            assert g0["_ext_verdict"]("never-seen-model") == (False, 1.0, None, "")
            # 能力：llmgw.quota 汇总（外部 + 自建），数量与死/活计数要自洽
            rq = d.invoke("llmgw.quota", {"refresh": True})
            qd = rq.as_dict()["data"]
            assert rq.ok and qd["external"]["count"] == 6, qd["external"]["count"]
            assert qd["external"]["dead"] == 3, qd["external"]["dead"]
            assert qd["external"]["stale"] == 1, qd["external"]["stale"]
            assert qd["external"]["sources"]["ofm"]["readable"] in (True, False)
        finally:
            g0["_load_json"] = real_load
            g0["_EXT_CACHE"].update(at={}, ts=0.0)

        # ⑯ 额度窗用尽（402 / "budget pool quota has been exhausted"）必须和「模型坏了」分开：
        # 分类独立、冷却到下一个投放窗口、绝不进 model bench、不可被早期恢复。
        clf = g0["_classify_error"]
        assert clf(402, "Budget pool quota has been exhausted.") == "quota"
        assert clf(429, "You exceeded your current quota, please check your plan") == "quota"
        assert clf(429, "Rate limit reached for requests") == "rate", "普通限流还是 rate"
        assert clf(401, "invalid api key") == "auth"
        assert clf(403, "model not allowed in this tier") == "forbidden"
        assert clf(0, "timed out") == "timeout"
        assert g0["_reason_for"]("quota") == "quota"
        assert g0["_reason_for"]("rate") == "429"
        dec = g0["_cooldown_decision"]("agentrouter", "gpt-6.1-sol", "quota")
        assert dec["source"] == "quota" and 300 <= dec["seconds"] <= 12 * 3600, dec
        nw = g0["_next_quota_window"]()
        assert 0 < nw - g0["_now"]() <= 24 * 3600, nw
        # 窗口必须落在 0/9/10/19 点之一
        assert time.localtime(nw).tm_hour in g0["QUOTA_WINDOWS_HOURS"], time.localtime(nw).tm_hour
        bench_before = list(g0["_MODEL_FAILURES"].get("gpt-6.1-sol", []))
        g0["record_failure"]("agentrouter", "gpt-6.1-sol", err_text="budget pool", reason="quota")
        assert g0["_MODEL_FAILURES"].get("gpt-6.1-sol", []) == bench_before, "quota 不该 bench 模型"
        cooled, rem, src = g0["is_cooled"]("agentrouter", "gpt-6.1-sol")
        assert cooled and src == "quota" and rem > 60, (cooled, rem, src)
        assert not g0["clear_cooldown_early"]("agentrouter", "gpt-6.1-sol"), "quota 不可早期恢复"
        g0["_COOLDOWN"].pop(("agentrouter", "gpt-6.1-sol"), None)

        # ⑰ 带 key 的额度型上游：有凭据才进候选（没 key 不假装可用），凭据不许出现在能力输出里
        real_secret = g0["UPSTREAMS_SECRET"]
        tmpf = os.path.join(os.environ.get("TEMP") or "/tmp", "llmgw_selftest_upstreams.json")
        try:
            if os.path.isfile(tmpf):
                os.remove(tmpf)
            g0["UPSTREAMS_SECRET"] = tmpf
            assert "agentrouter" not in [x["name"] for x in real_discover()], \
                "没 key 不该出现在端点清单"
            with open(tmpf, "w", encoding="utf-8") as f:
                json.dump({"agentrouter": "sk-selftest"}, f)
            assert g0["_keyed_key"](g0["KEYED_ENDPOINTS"][0]) == "sk-selftest"
            gws = real_discover()
            ag = [x for x in gws if x["name"] == "agentrouter"]
            assert ag and ag[0]["key"] == "sk-selftest", ag
            assert ag[0]["cost"] == 1.0 and ag[0].get("keyed") is True
            assert ag[0]["models"] == g0["KEYED_ENDPOINTS"][0]["models"]
            # 环境变量优先于密钥文件
            os.environ["LLMW_AGENTROUTER_KEY"] = "sk-env"
            assert g0["_keyed_key"](g0["KEYED_ENDPOINTS"][0]) == "sk-env"
            # 能力输出里只有「配没配」，没有凭据本身
            qd2 = d.invoke("llmgw.quota", {}).as_dict()["data"]
            ku = {x["name"]: x for x in qd2["keyed_upstreams"]}
            assert ku["agentrouter"]["configured"] is True
            assert "sk-env" not in json.dumps(qd2) and "sk-selftest" not in json.dumps(qd2)
            assert qd2["quota_window"]["next_reset"], qd2["quota_window"]
        finally:
            os.environ.pop("LLMW_AGENTROUTER_KEY", None)
            g0["UPSTREAMS_SECRET"] = real_secret
            if os.path.isfile(tmpf):
                os.remove(tmpf)

        # ⑱ 成本折价只压「真花额度」的上游：免费端点因子恒为 1（老排序一字不变），
        # 同分时免费的先上，额度型上游留到没有免费路可走的时刻。
        real_disc2 = g0["discover_gateways"]
        g0["discover_gateways"] = lambda: [
            {"name": "freeep", "base": "http://127.0.0.1:1/v1", "key": "k", "cost": 0.0},
            {"name": "paidep", "base": "http://127.0.0.1:2/v1", "key": "k", "cost": 1.0},
        ]
        try:
            cs = g0["_build_candidates"](g0["discover_gateways"](), ["m-x"])
            by = {c["gw"]["name"]: c for c in cs}
            assert by["freeep"]["cost"] == 0.0 and by["paidep"]["cost"] == 1.0, by
            expect = by["paidep"]["score"] * (1.0 + g0["COST_WEIGHT"] * 1.0)
            assert abs(by["freeep"]["score"] - expect) < 1e-9, (by["freeep"]["score"], expect)
            assert cs[0]["gw"]["name"] == "freeep", "同分时免费端点应排前"
        finally:
            g0["discover_gateways"] = real_disc2

        # ⑲ new-api 公开元数据（零登录、零额度）：/api/status + /api/pricing 两张公开接口
        #    要能解析出模型倍率、赠送额度换算、有无充值、以及公告里的投放窗口；站点挂了要如实报错。
        def fake_req2(base, path, key, timeout=15, method="GET", body=None, extra_headers=None):
            if "nope.invalid" in base:
                return 404, {}, {"error": "x"}, None
            if path == "/api/status":
                return 200, {}, {"data": {
                    "system_name": "Agent Router", "version": "init-test",
                    "quota_per_unit": 500000, "quota_for_invitee": 25000000,
                    "quota_for_inviter": 50000000,
                    "enable_online_topup": False, "enable_stripe_topup": False,
                    "github_oauth": True, "price": 7.3,
                    "announcements": [
                        {"content": "每日于北京时间 9:00（对应 UTC 1:00）投放额度，用完即止"},
                        {"content": "投放到北京时间10:00和19:00（对应UTC时间02:00和11:00）"}]}}, None
            if path == "/api/pricing":
                return 200, {}, {"data": [{"model_name": "gpt-6.1-sol", "model_ratio": 2,
                                           "completion_ratio": 5,
                                           "enable_groups": ["default"]}]}, None
            return 404, {}, {"error": "x"}, None
        g0["_req_json"] = fake_req2
        try:
            rd = d.invoke("llmgw.discover", {"sites": ["agentrouter.org"]}).as_dict()["data"]
            assert rd["ok"] == 1 and rd["count"] == 1, rd
            s0 = rd["sites"][0]
            assert s0["info"]["signup_usd"] == 50.0, s0["info"]
            assert s0["info"]["inviter_usd"] == 100.0
            assert s0["info"]["topup_enabled"] is False
            assert s0["info"]["quota_drop_windows_h"] == [9, 10, 19], \
                s0["info"]["quota_drop_windows_h"]
            assert s0["model_count"] == 1 and s0["models"][0]["ratio"] == 2, s0["models"]
            assert "github" in s0["info"]["oauth"], s0["info"]["oauth"]
            rbad = d.invoke("llmgw.discover", {"sites": ["nope.invalid"]}).as_dict()["data"]
            assert rbad["ok"] == 0 and len(rbad["sites"][0]["errors"]) == 2, rbad
        finally:
            g0["_req_json"] = fake_req

        # ⑳ Responses 协议双向翻译：system→instructions、assistant→output_text、
        #    返回的 reasoning 段不进正文但保留在 _reasoning，usage 映射成 chat 口径。
        ins, turns = g0["_to_resp_input"]([
            {"role": "system", "content": "你是蓝鲸"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在"},
            {"role": "user", "content": "继续"},
        ])
        assert ins == "你是蓝鲸", ins
        assert [t["role"] for t in turns] == ["user", "assistant", "user"], turns
        assert turns[0]["content"][0]["type"] == "input_text"
        assert turns[1]["content"][0]["type"] == "output_text", turns[1]
        chat = g0["_from_resp_body"]({
            "id": "resp_x", "status": "completed", "model": "gpt-5.6-sol",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "想一下"}]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "收到。"}]},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 7,
                      "total_tokens": 17,
                      "output_tokens_details": {"reasoning_tokens": 3}},
        })
        assert chat["choices"][0]["message"]["content"] == "收到。", chat
        assert chat["usage"]["total_tokens"] == 17 and chat["usage"]["reasoning_tokens"] == 3
        assert chat["_reasoning"] == "想一下" and chat["_style"] == "responses"

        # ㉑ Responses 型网关：请求必须打到 /v1/responses 且带上客户端 UA
        #    （AgentRouter 两道闸：UA 白名单 + 只认 Responses 协议）。
        seen = {}

        def fake_resp(base, path, key, timeout=15, method="GET", body=None,
                      extra_headers=None):
            seen["path"] = path
            seen["ua"] = (extra_headers or {}).get("User-Agent")
            seen["body"] = body
            return 200, {}, {"id": "r1", "status": "completed",
                             "output": [{"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": "ok"}]}],
                             "usage": {"input_tokens": 3, "output_tokens": 1,
                                       "total_tokens": 4}}, None

        g0["_req_json"] = fake_resp
        try:
            gw_r = {"name": "agentrouter", "base": "https://agentrouter.org/v1",
                    "key": "sk-test", "anon": False, "api_style": "responses",
                    "ua": "codex_cli_rs/0.51.0 (Windows 11; x86_64) Windows"}
            b, e, _via, tk, _cls, _rs, _ra = g0["_chat_once"](
                gw_r, {"model": "gpt-5.6-sol", "max_tokens": 64,
                       "messages": [{"role": "system", "content": "S"},
                                    {"role": "user", "content": "U"}]}, 20)
            assert e is None and b["choices"][0]["message"]["content"] == "ok", (e, b)
            assert tk == 4, tk
            assert seen["path"] == "/v1/responses", seen
            assert seen["ua"] and "codex" in seen["ua"].lower(), seen
            assert seen["body"]["instructions"] == "S", seen["body"]
            # reasoning 型上游给 64 会被 reasoning 吃光导致正文为空 → 必须有下限
            assert seen["body"]["max_output_tokens"] >= 512, seen["body"]
            assert seen["body"]["input"][0]["content"][0]["type"] == "input_text"
        finally:
            g0["_req_json"] = fake_req

        # ㉒ 流式：首字节前失败换上游；免费那批全挂后，额度型才在 deeper 里被兜底轮到。
        calls = []
        real_sse = g0["_sse_once"]

        def fake_sse(gw, payload, timeout, on_delta=None):
            calls.append(gw["name"])
            if gw["name"] == "free-bad":
                return (None, "boom", None, 0, "server", "5xx", None,
                        {"ttft_s": None, "chunks": 0, "partial": False})
            if gw["name"] == "free-cut":
                # 首字节后断流：内容保留，标 partial，不重试
                return ({"choices": [{"message": {"content": "半句"},
                                      "finish_reason": "stop"}],
                         "usage": {"total_tokens": 3}}, None, gw["name"], 3, "ok", "ok",
                        None, {"ttft_s": 0.2, "chunks": 2, "partial": True,
                               "err": "connection reset"})
            return ({"choices": [{"message": {"content": "ok"},
                                  "finish_reason": "stop"}],
                     "usage": {"total_tokens": 5}}, None, gw["name"], 5, "ok", "ok",
                    None, {"ttft_s": 0.3, "chunks": 4, "partial": False})

        g0["_sse_once"] = fake_sse
        try:
            c_free_bad = {"gw": {"name": "free-bad", "base": "x"}, "model": "m1", "score": 1}
            c_paid = {"gw": {"name": "paid-ok", "base": "x", "cost": 2.0}, "model": "m2",
                      "score": 0.5}
            rr = g0["_chat_stream_serial"]("llmgw.chat", d, [c_free_bad], {}, 5, None, 0,
                                           deeper=[c_free_bad, c_paid])
            assert rr.ok, rr.err
            dd = rr.as_dict()["data"]
            assert dd["endpoint"] == "paid-ok", dd
            assert calls == ["free-bad", "paid-ok"], calls
            assert dd["partial"] is False and dd["ttft_s"] == 0.3
            # 首字节后断流：不换上游，内容保留 + stream_err 如实上报
            calls.clear()
            rr2 = g0["_chat_stream_serial"]("llmgw.chat", d,
                                           [{"gw": {"name": "free-cut", "base": "x"},
                                             "model": "m3", "score": 1}], {}, 5, None, 0,
                                           deeper=[c_paid])
            dd2 = rr2.as_dict()["data"]
            assert calls == ["free-cut"], calls
            assert dd2["partial"] is True and "connection reset" in dd2["stream_err"], dd2
            assert dd2["result"]["choices"][0]["message"]["content"] == "半句"
        finally:
            g0["_sse_once"] = real_sse

        # ㉓ 免费优先：默认模型挑选必须把 cost=0 的端点排在额度型前面，
        #    否则额度型刚成功一次就会被 bandit 顶到默认位，之后天天薅同一只羊。
        #    注意 picks 的元素是 (default_model, 网关)，与 _h_chat 里一致。
        picks = [("paid-model", {"name": "paid", "cost": 3.0}),
                 ("free-model", {"name": "free", "cost": 0.0})]
        picks.sort(key=lambda t: (float(t[1].get("cost") or 0) > 0,
                                  -g0["bandit_score"](t[1]["name"], t[0])))
        assert picks[0][0] == "free-model", picks

        # ㉔ Codex 订阅号网关：没凭据就静默跳过（不假装可用）；有凭据必须是
        #    Responses 协议 + 专门的 responses_path（Codex 的路径不带 /v1）。
        import base64 as _b64
        import shutil as _sh
        import tempfile as _tf
        ca = g0["_codex"]
        old_auth, old_pending, old_req = ca.AUTH_PATH, ca.PENDING_PATH, ca._req
        _td = _tf.mkdtemp()
        try:
            ca.AUTH_PATH = os.path.join(_td, "codex_auth.json")
            ca.PENDING_PATH = os.path.join(_td, "codex_pending.json")
            assert g0["_codex_gateway"]() is None, "没凭据不该挂 codex 网关"

            def _jwt(payload):
                seg = _b64.urlsafe_b64encode(
                    json.dumps(payload).encode()).decode().rstrip("=")
                return "eyJhbGciOiJub25lIn0." + seg + ".sig"

            acc = "acc-selftest"
            at = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": acc},
                       "exp": int(time.time()) + 3600})
            it = _jwt({"email": "t@example.com"})
            with open(ca.AUTH_PATH, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "access_token": at, "refresh_token": "rt",
                           "id_token": it, "account_id": acc,
                           "email": "t@example.com",
                           "expires_at": int(time.time()) + 3600}, f)
            gw_c = g0["_codex_gateway"]()
            assert gw_c and gw_c["name"] == "codex", gw_c
            assert gw_c["api_style"] == "responses", gw_c
            assert gw_c["responses_path"] == "/responses", gw_c
            assert gw_c["auth"] == "codex" and gw_c["cost"] >= 0, gw_c

            # ㉕ codex_login：status 认得连上了，logout 清干净，
            #    并且任何返回里都不许出现 token 原文（凭据不外泄是硬要求）。
            ca._req = lambda *a, **k: (200, {}, None)
            r1 = d.invoke("llmgw.codex_login", {"action": "status"})
            dd1 = r1.as_dict()["data"]
            assert dd1["connected"] is True and dd1["gateway_active"] is True, dd1
            assert dd1.get("email") == "t@example.com", dd1
            blob = json.dumps(r1.as_dict(), ensure_ascii=False)
            assert at not in blob and "rt" != blob, "能力输出回显了 token"
            r2 = d.invoke("llmgw.codex_login", {"action": "logout"})
            assert r2.ok and not os.path.isfile(ca.AUTH_PATH), r2.as_dict()
            assert g0["_codex_gateway"]() is None
            r3 = d.invoke("llmgw.codex_login", {"action": "nonsense"})
            assert not r3.ok, "非法 action 该报错"
        finally:
            ca.AUTH_PATH, ca.PENDING_PATH, ca._req = old_auth, old_pending, old_req
            _sh.rmtree(_td, ignore_errors=True)

        print("llmgw_device 自测：25 组全过")
    finally:
        # 还原模块级状态与函数，避免影响后续 import
        g["discover_gateways"] = real_discover
        g["_req_json"] = real_req
        for name, val in saved.items():
            globals()[name].clear()
            globals()[name].update(val)


if __name__ == "__main__":
    _selftest()
