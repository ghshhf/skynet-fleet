"""网络层 · 免费模型网关的注册表与探测（P0）。

一句话：**把"哪个云端能用"变成一条命令能查清的事。**

为什么要这一层（而不是在手机端分别对接三次）
--------------------------------------------
freellmapi、zen-gate、dsh-our-free-model 三个仓库长得完全不一样，做的事却是同一件：
把上游的**免费额度**翻译成一个 OpenAI 兼容端点。差异只在「上游从哪来」「怎么轮换」
「有没有局域网口」。

所以对天网来说它们不是三个系统，是**同一种资源的三个实例**。分别对接三次的话：
  · 加第四家要改四处（路由、UI、记账、设置）；
  · 三家共享同一批上游账号，各自记账必然互相抢额度；
  · 密钥散落在手机端，手机丢了等于全泄。
⇒ 抽一层 Provider 注册表，手机只认"云引擎"，不认具体是哪家。

设计上刻意不做的两件事
----------------------
1. **不 fork 那三个仓库**。它们是外部依赖，跟着上游走；一 fork 就要同步三家，纯负债。
2. **不把端口写死成唯一真相**。三家端口都能变（dsh 明确写了"占用则顺延，
   实际端口落盘"）。所以是「候选端口扫描 + 读它们自己的配置文件 + 手填」三路合流，
   任何一路拿到都算数。
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict

try:
    import requests
except Exception:                      # 没装 requests 时退化成"只探端口"，不整体炸
    requests = None


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    """一个可用的上游端点。

    `source` 只用于**展示与人眼判断**（"这是 dsh 还是 zen-gate"），
    **路由逻辑不许依赖它** —— 路由只看 kind / 能力 / 额度。
    一旦让路由认识具体品牌，加第四家就得改路由，这层就白抽了。
    """
    id: str
    label: str
    kind: str                       # gateway | local | paid
    base: str                       # 形如 http://127.0.0.1:3001/v1
    source: str = ""                # freellmapi | zen-gate | dsh | llama.cpp | self
    key_env: str = ""               # 密钥从哪个环境变量取（不落盘）
    key: str = ""                   # 显式给的密钥（只在内存，不写文件）
    key_hint: str = ""              # 从网关自己的配置文件里读到的密钥（内存；不打印、不落盘）
    models: list = field(default_factory=list)
    alive: bool = False
    latency_ms: int = 0
    needs_key: bool = False
    note: str = ""                  # 死了要说清为什么，不许只写"不可用"
    checked_at: float = 0.0

    @property
    def real_key(self):
        """优先级：显式给 > 环境变量 > 网关配置里自动读到的。

        第三级是 2026-10-25 补的：端口都从人家的配置文件里读了，Key 就在同一个文件，
        没道理再让人去配一遍环境变量——那样 `discover()` 会显示"活着但要 Key"，
        模型数永远是 0，等于探测白做一半。
        """
        if self.key:
            return self.key
        if self.key_env:
            got = os.environ.get(self.key_env, "")
            if got:
                return got
        return self.key_hint


# 内置候选：每个都带「为什么是这个端口」的出处，省得下次再翻 README。
BUILTIN = [
    Provider(
        id="freellmapi", label="FreeLLMAPI", kind="gateway",
        base="http://127.0.0.1:3001/v1", source="freellmapi",
        key_env="FREELMAPI_KEY",
        note="34 家 / 635 端点；鉴权用仪表盘「密钥」页顶部的统一 Key",
    ),
    Provider(
        id="dsh-local", label="dsh 本机网关", kind="gateway",
        base="http://127.0.0.1:8326/v1", source="dsh",
        key_env="DSH_KEY",
        note="Chat Completions + Responses；免登录免 Key，凭据在宿主凭据库",
    ),
    Provider(
        id="dsh-relay", label="dsh 局域网中继", kind="gateway",
        base="http://127.0.0.1:18899/v1", source="dsh",
        key_env="DSH_LAN_KEY",
        note="独立 LAN Key（与本机 Key 不通用）；默认关，开了才活",
    ),
    Provider(
        id="zen-gate", label="Zen Gate", kind="gateway",
        base="http://127.0.0.1:18765/v1", source="zen-gate",
        key_env="ZENGATE_KEY",
        note="端口在面板里配；这里只是候选，discover() 会读它自己的配置改正",
    ),
    Provider(
        id="llama-8080", label="本机 llama.cpp · 8080", kind="local",
        base="http://127.0.0.1:8080/v1", source="llama.cpp",
        note="常驻 Qwen3.5-4B（Vulkan，32K ctx）",
    ),
    Provider(
        id="llama-8090", label="本机 llama.cpp · 8090", kind="local",
        base="http://127.0.0.1:8090/v1", source="llama.cpp",
        note="Meissa Qwen2.5-7B（q8_0）",
    ),
]

# 降级链：按「失效概率从低到高」排。
# 本地排第一不是偏好，是它唯一不依赖任何上游政策；
# 自付排最后因为它唯一会真花钱。
FALLBACK_ORDER = ["llama-8080", "llama-8090", "dsh-local", "dsh-relay",
                  "zen-gate", "freellmapi", "paid"]


# ---------------------------------------------------------------------------
# 配置发现：三家各自的落盘位置不一样，读它们自己的话，别猜
# ---------------------------------------------------------------------------

def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _iter_json(root, depth=2):
    """扫一个目录下的 json（限制层数，别把整个盘翻一遍）。"""
    if not os.path.isdir(root):
        return
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root):
        if cur[len(root):].count(os.sep) >= depth:
            dirs[:] = []
        for fn in files:
            if fn.endswith(".json"):
                yield os.path.join(cur, fn)


def _find_key_in(obj, keys=("mainkey", "lankey", "apikey", "api_key", "authkey",
                            "access_key", "token", "secret", "key")):
    """在网关自己的配置里找密钥。

    和 `_find_port_in` 一个路子：都在任意嵌套 JSON 里找，找到第一个像的就返回。
    只取看起来真像凭据的值（长度够、不是明显的占位符），免得把 `"key": ""`
    或某个字段名本身当成密钥。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, str):
                s = v.strip()
                # 太短的多半是开关/枚举，不是凭据
                if len(s) >= 8 and not s.lower() in ("true", "false", "none", "null"):
                    return s
            got = _find_key_in(v, keys)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key_in(v, keys)
            if got:
                return got
    return ""


def _find_port_in(obj, keys=("port", "listen_port", "gateway_port", "http_port")):
    """在任意嵌套 JSON 里找端口。找到第一个像端口的就返回。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, (int, str)):
                try:
                    n = int(str(v).strip())
                    if 1 <= n <= 65535:
                        return n
                except Exception:
                    pass
            got = _find_port_in(v, keys)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_port_in(v, keys)
            if got:
                return got
    return None


def _zen_gate_ports():
    """zen-gate 的端口在面板里配，落盘在 %APPDATA%\\zen-gate\\ 下的 json。

    顺手把 mainKey 一起读出来：端口都从这儿取了，Key 就在同一个文件里。
    返回 (port, 文件, key)；key 只进内存，不打印、不落盘。
    """
    roots = [
        os.path.join(os.environ.get("APPDATA", ""), "zen-gate"),
        os.path.expanduser("~/.config/zen-gate"),
        os.path.expanduser("~/Library/Application Support/zen-gate"),
    ]
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for f in _iter_json(r):
            d = _read_json(f)
            p = _find_port_in(d)
            if p:
                return p, f, _find_key_in(d)
    return None, "", ""


def _dsh_ports():
    """dsh 独立服务默认 ~/.our-free-model，落盘的实际端口会覆盖默认值。

    同样顺手读 key（dsh 免登录免 Key，但独立服务/中继可能配了 LAN Key）。
    """
    root = os.path.expanduser("~/.our-free-model")
    for f in _iter_json(root):
        d = _read_json(f)
        p = _find_port_in(d)
        if p:
            return p, f, _find_key_in(d)
    return None, "", ""


def discover():
    """返回 (providers, notes)。notes 记「谁从哪读到的端口」，出问题能回溯。"""
    provs = [Provider(**{**asdict(p), "models": [], "checked_at": 0.0}) for p in BUILTIN]
    notes = []
    by_id = {p.id: p for p in provs}

    p, f, k = _zen_gate_ports()
    if p and by_id.get("zen-gate"):
        by_id["zen-gate"].base = "http://127.0.0.1:%d/v1" % p
        if k:
            by_id["zen-gate"].key_hint = k
        notes.append("zen-gate 端口 %d 读自 %s" % (p, f))

    p, f, k = _dsh_ports()
    if p:
        for pid in ("dsh-local", "dsh-relay"):
            if by_id.get(pid) and pid == "dsh-local":
                by_id[pid].base = "http://127.0.0.1:%d/v1" % p
                if k:
                    by_id[pid].key_hint = k
                notes.append("dsh 端口 %d 读自 %s" % (p, f))

    # 用户手填的覆盖（"咱们的"配置，不进仓库）
    cfg = os.path.expanduser("~/.fleet/providers.json")
    d = _read_json(cfg)
    if isinstance(d, dict):
        for k, v in (d.get("override") or {}).items():
            if k in by_id and isinstance(v, str) and v:
                by_id[k].base = v
                notes.append("%s 被 %s 覆盖为 %s" % (k, cfg, v))
    return provs, notes


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def _tcp_ok(host, port, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _host_port(base):
    m = re.match(r"https?://([^/:]+)(?::(\d+))?", base or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2) or (443 if base.startswith("https") else 80))


def probe(p, timeout=1.2):
    """探一个 provider。

    🔴 判据：**先探端口再谈 HTTP**。
    端口都不通时直接给"端口没监听"，不要发请求等超时——
    这一条是 `ctrl_client.available()` 那次的教训：失败路径最慢，而它恰恰最常被调。
    """
    host, port = _host_port(p.base)
    if not host:
        p.alive, p.note = False, "base 写错了：%r" % p.base
        return p
    t0 = time.time()
    if not _tcp_ok(host, port):
        p.alive, p.latency_ms = False, 0
        p.note = "端口没监听（%s:%s）—— 网关没起，或端口顺延了" % (host, port)
        p.checked_at = time.time()
        return p

    if requests is None:
        p.alive = True
        p.latency_ms = int((time.time() - t0) * 1000)
        p.note = "端口通（没装 requests，无法列模型）"
        p.checked_at = time.time()
        return p

    url = p.base.rstrip("/") + "/models"
    headers = {}
    k = p.real_key
    if k:
        headers["Authorization"] = "Bearer " + k
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        p.alive, p.latency_ms = False, 0
        p.note = "端口通但 /models 失败：%s" % type(e).__name__
        p.checked_at = time.time()
        return p

    p.latency_ms = int((time.time() - t0) * 1000)
    p.checked_at = time.time()
    if r.status_code == 401 or r.status_code == 403:
        p.alive, p.needs_key = True, True
        p.note = "活着，但要 Key（HTTP %d）—— 配 %s" % (r.status_code, p.key_env or "密钥")
        return p
    if r.status_code >= 400:
        p.alive = False
        p.note = "HTTP %d：%s" % (r.status_code, (r.text or "")[:80].replace("\n", " "))
        return p
    try:
        d = r.json()
        items = d.get("data") if isinstance(d, dict) else None
        if items is None and isinstance(d, dict):
            items = d.get("models")
        p.models = [x.get("id") for x in (items or []) if isinstance(x, dict) and x.get("id")]
    except Exception:
        p.models = []
    p.alive = True
    p.needs_key = False
    p.note = "活" if p.models else "活（/models 没返回列表）"
    return p


def probe_all(provs=None, timeout=1.2, workers=8):
    """并发探测。串行的话 6 个 provider × 1.2s 上限 = 7 秒，终端会明显卡。"""
    provs = provs if provs is not None else discover()[0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda x: probe(x, timeout=timeout), provs))
    return provs


def fallback_chain(provs):
    """按 FALLBACK_ORDER 排，没探活的排后面（但**不丢**）。

    为什么不丢：网关可能下一秒就起来，把它从链里删掉就再也回不来。
    """
    idx = {pid: i for i, pid in enumerate(FALLBACK_ORDER)}
    return sorted(provs, key=lambda p: (not p.alive, idx.get(p.id, 99)))


# ---------------------------------------------------------------------------
# P2：路由（选谁做）+ 额度记账（按上游账号，不按网关）
# ---------------------------------------------------------------------------

# 模型 id → 上游厂商。记账的键必须是「厂商 + 账号」，不是网关名：
# 同一个智谱账号可能同时被 dsh 和 zen-gate 消耗，按网关记账就会出现
# 「以为还有额度、实际早打满」。这是 freellmapi 按 Key 跟踪用量那条教训的推广。
VENDOR_HINTS = [
    (r"^(glm|chatglm)", "zhipu"),
    (r"^deepseek", "deepseek"),
    (r"^(qwen|qwq)", "alibaba"),
    (r"^(gpt|o[1-9]|text-davinci|chatgpt)", "openai"),
    (r"^claude", "anthropic"),
    (r"^(gemini|gemma)", "google"),
    (r"^(kimi|moonshot)", "moonshot"),
    (r"^(minimax|abab)", "minimax"),
    (r"^step", "stepfun"),
    (r"^nemotron", "nvidia"),
    (r"^mimo", "xiaomi"),
    (r"^llama", "meta"),
    (r"^(mistral|mixtral)", "mistral"),
    (r"^ernie", "baidu"),
    (r"^hunyuan", "tencent"),
    (r"^(doubao|seed)", "bytedance"),
    (r"^(yi-|01-ai)", "01ai"),
    (r"^(glm-4|internlm)", "internlm"),
]


def vendor_of(model_id):
    """从模型 id 推上游厂商。推不出来就返回 unknown——**不许瞎猜成某一家**。

    猜错比不猜更糟：把 A 厂的用量记到 B 厂头上，等于额度账本身就是错的。
    """
    m = (model_id or "").strip().lower()
    if not m:
        return "unknown"
    # 带网关前缀的（如 "zen-gate/deepseek-v4"）先剥掉
    if "/" in m:
        m = m.split("/", 1)[1]
    for pat, name in VENDOR_HINTS:
        if re.match(pat, m):
            return name
    return "unknown"


# 模态需求：只看 provider 自己报上来的模型表，不猜、不假设它"应该支持"。
_NEED_PAT = {
    "vision": re.compile(r"(vision|[-_]vl\b|4v|omni|image)", re.I),
    "audio":  re.compile(r"(audio|voice|whisper|tts|speech|asr)", re.I),
    "long":   re.compile(r"(128k|200k|256k|512k|1m|long)", re.I),
    "embed":  re.compile(r"(embed|bge|gte)", re.I),
}


def supports(p, need):
    """provider 能不能满足这个需求。

    没探活或模型表为空的，一律 False——**模型表都拿不到就别谈支持什么**。
    """
    if not p.alive or not p.models:
        return False
    if not need:
        return True
    pat = _NEED_PAT.get(need)
    if not pat:
        return True
    return any(pat.search(m) for m in p.models)


def route(provs=None, need=None, exclude=(), model=None):
    """按降级链 + 需求选一个 provider。返回 (provider, reason)。

    选不出来时 provider 是 None，但 **reason 必须说清为什么**——
    静默返回 None 会让调用方以为"没有可用后端"，实际可能只是需求太窄。
    """
    provs = provs if provs is not None else probe_all()
    skip = set(exclude)
    seen = []
    for p in fallback_chain(provs):
        if p.id in skip:
            continue
        if model and model not in (p.models or []):
            seen.append("%s(无此模型)" % p.id)
            continue
        if not supports(p, need):
            seen.append("%s(不满足 %s)" % (p.id, need or "-"))
            continue
        return p, "选 %s：链位 %d，%dms，%d 模型" % (
            p.id, FALLBACK_ORDER.index(p.id) if p.id in FALLBACK_ORDER else 99,
            p.latency_ms, len(p.models or []))
    return None, "没有可用 provider（链上 %d 个；淘汰原因：%s）" % (
        len(provs), "、".join(seen) or "全被排除")


def pick_model(p, need=None, skip=()):
    """从一个 provider 的模型表里挑一个能用的。挑不出来返回 None（不许瞎编模型名）。

    `skip` 是这家已经试过且失败的模型——2026-10-25 实测出来的：
    一家网关的 23 个模型走的是**不同上游车道**，其中一条报
    `FreeUsageLimitError`（429）不代表整家废了，换同一个网关的下一个模型就能成。
    只会换 provider 不会换模型，等于把 23 条车道当 1 条用。
    """
    if not p.models:
        return None
    skip = set(skip)
    pat = _NEED_PAT.get(need) if need else None
    pool = [m for m in p.models if m not in skip]
    if pat:
        hit = [m for m in pool if pat.search(m)]
        return hit[0] if hit else None
    return pool[0] if pool else None


class Quota:
    """额度记账：键是「上游厂商 + 账号」，按天聚合，落 sqlite。

    为什么不按 provider 记：三家网关共享同一批上游账号，
    按网关记就会各算各的，合起来早超了却看不出来。
    """

    def __init__(self, path=None):
        self.path = path or os.path.expanduser("~/.fleet/quota.sqlite")
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        import sqlite3
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS usage ("
            " day TEXT NOT NULL, vendor TEXT NOT NULL, account TEXT NOT NULL,"
            " calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0,"
            " PRIMARY KEY (day, vendor, account))")
        self._db.commit()

    def add(self, vendor, account="", calls=1, tokens=0, day=None):
        import datetime
        day = day or datetime.date.today().isoformat()
        vendor = vendor or "unknown"
        self._db.execute(
            "INSERT INTO usage (day, vendor, account, calls, tokens) VALUES (?,?,?,?,?) "
            "ON CONFLICT (day, vendor, account) DO UPDATE SET "
            "calls = calls + excluded.calls, tokens = tokens + excluded.tokens",
            (day, vendor, account, calls, tokens))
        self._db.commit()

    def used(self, vendor, account="", day=None):
        import datetime
        day = day or datetime.date.today().isoformat()
        row = self._db.execute(
            "SELECT calls, tokens FROM usage WHERE day=? AND vendor=? AND account=?",
            (day, vendor, account or "")).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def report(self, day=None):
        """按天出一张表：每行一个「厂商/账号」。"""
        if day:
            rows = self._db.execute(
                "SELECT day, vendor, account, calls, tokens FROM usage WHERE day=? "
                "ORDER BY calls DESC", (day,)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT day, vendor, account, calls, tokens FROM usage "
                "ORDER BY day DESC, calls DESC").fetchall()
        return rows

    def close(self):
        try:
            self._db.close()
        except Exception:
            pass


def chat(messages, provs=None, need=None, model=None, timeout=90,
         max_try=8, quota=None, account=""):
    """真发一轮，失败自动换下一个。

    返回 dict，成功时带：
      `ok` / `used`（实际用了谁的 id）/ `used_model` / `content` / `tried`（试过几个）
    失败时带 `ok=False` + `err`，并且 **err 里列出每一步为什么淘汰**，
    不许只回一句"全部失败"。

    降级是**两级**的，这是 2026-10-25 实测出来的，不是设计癖：
      一级 · 同一家内换模型（429 = 那条上游车道额度打满，别家车道可能还能用）
      二级 · 换下一家 provider
    另外 403 与 429 要分开对待（计划书 R3）：403 是风控，**整家停用不再碰**；
    429 只是这条车道没钱了，只跳过这个模型。
    """
    import requests as _rq
    provs = provs if provs is not None else probe_all()
    tried, why = [], []
    tried_models = {}                 # provider_id -> 已失败的模型
    quota = quota if quota is not None else Quota()

    for _ in range(max_try):
        p, reason = route(provs, need=need, exclude=tried, model=model)
        if not p:
            return {"ok": False, "err": reason, "tried": tried, "why": why}
        m = model or pick_model(p, need, skip=tried_models.get(p.id, ()))
        if not m:
            why.append("%s：模型都试过了（%d 个）" % (p.id, len(p.models or [])))
            tried.append(p.id)
            continue

        url = p.base.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        k = p.real_key
        if k:
            headers["Authorization"] = "Bearer " + k
        try:
            r = _rq.post(url, headers=headers,
                         json={"model": m, "messages": messages, "stream": False},
                         timeout=timeout)
        except Exception as e:
            why.append("%s/%s：%s" % (p.id, m, type(e).__name__))
            tried_models.setdefault(p.id, []).append(m)
            continue
        if r.status_code == 403:
            why.append("%s：HTTP 403 风控，整家停用" % p.id)
            tried.append(p.id)
            continue
        if r.status_code >= 400:
            why.append("%s/%s：HTTP %d %s" % (p.id, m, r.status_code,
                                              (r.text or "")[:60].replace("\n", " ")))
            tried_models.setdefault(p.id, []).append(m)
            continue

        try:
            data = r.json()
            content = ((data.get("choices") or [{}])[0]
                       .get("message", {}).get("content", ""))
            usage = data.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
        except Exception as e:
            why.append("%s/%s：响应解析失败 %s" % (p.id, m, type(e).__name__))
            tried_models.setdefault(p.id, []).append(m)
            continue

        quota.add(vendor_of(m), account, calls=1, tokens=tokens)
        return {
            "ok": True,
            "used": p.id,           # 计划书 P2：响应里标注实际用了谁
            "used_model": m,
            "used_vendor": vendor_of(m),
            "content": content,
            "tokens": tokens,
            "tried": tried + [p.id],
            "why": why,
        }
    return {"ok": False, "err": "试了 %d 次都失败" % len(tried),
            "tried": tried, "why": why}


# ---------------------------------------------------------------------------
# 落盘 / 输出
# ---------------------------------------------------------------------------

def snapshot(path=None):
    """把探测结果落一份，给 re_llm 之类的老读者用（形状与 ~/.workbuddy/models.json 对齐）。"""
    provs = probe_all()
    data = {
        "at": time.time(),
        "providers": [
            {k: v for k, v in asdict(p).items() if k != "key"} for p in provs
        ],
        "models": [
            {
                "id": "%s/%s" % (p.id, m),
                "provider": p.id,
                "base": p.base,
                "alive": p.alive,
                "latency_ms": p.latency_ms,
            }
            for p in provs if p.alive for m in (p.models or [])
        ],
    }
    path = path or os.path.expanduser("~/.fleet/providers_snapshot.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return path, data


def render(provs, notes=None):
    out = []
    if notes:
        for n in notes:
            out.append("· " + n)
        out.append("")
    out.append("%-14s %-4s %-26s %6s %6s  %s" %
               ("ID", "状态", "端点", "延迟", "模型", "说明"))
    out.append("-" * 96)
    for p in fallback_chain(provs):
        out.append("%-14s %-4s %-26s %5dms %6s  %s" % (
            p.id,
            "活" if p.alive else "死",
            p.base.replace("http://", "").replace("https://", ""),
            p.latency_ms,
            len(p.models) or "-",
            (p.note or "")[:40],
        ))
    alive = [p for p in provs if p.alive]
    out.append("-" * 96)
    out.append("共 %d 个端点 / %d 个活着 / 覆盖模型 %d 个"
               % (len(provs), len(alive), sum(len(p.models) for p in alive)))
    return "\n".join(out)


def _selftest():
    """自测：不依赖任何真网关在跑，用假 HTTP 覆盖判据的四个分支。"""
    fails = []

    def ck(cond, why):
        if not cond:
            fails.append(why)

    # 1) base 写错 → 不许抛，要给出原因
    bad = Provider(id="x", label="x", kind="gateway", base="not-a-url")
    probe(bad)
    ck(not bad.alive and "base" in bad.note, "坏 base 应给出原因，实得 %r" % bad.note)

    # 2) 端口不通 → 走端口判据，不该发 HTTP（耗时必须远小于 timeout）
    dead = Provider(id="y", label="y", kind="gateway", base="http://127.0.0.1:1/v1")
    t = time.time()
    probe(dead, timeout=2.0)
    dt = time.time() - t
    ck(not dead.alive, "端口 1 不该判活")
    ck("端口没监听" in dead.note, "应报端口没监听，实得 %r" % dead.note)
    ck(dt < 1.0, "端口死探不能等满 timeout，实耗 %.2fs" % dt)

    # 3) 降级链：死的排后面但一个都不许丢
    ps = [Provider(id="freellmapi", label="a", kind="gateway", base="u", alive=False),
          Provider(id="llama-8080", label="b", kind="local", base="u", alive=True)]
    ch = fallback_chain(ps)
    ck(len(ch) == 2, "降级链不许丢项")
    ck(ch[0].id == "llama-8080", "活的应排前，实得 %s" % ch[0].id)

    # 4) 任意嵌套 json 里找端口
    ck(_find_port_in({"a": {"b": {"port": 8326}}}) == 8326, "_find_port_in 嵌套失败")
    ck(_find_port_in({"port": "abc"}) is None, "非数字端口应返回 None")
    ck(_find_port_in({"n": 999999}) is None, "越界端口应返回 None")

    # 5) discover 不许因为文件不存在就炸
    provs, _notes = discover()
    ck(len(provs) >= 5, "内置 provider 少于 5 个")

    # 6) 密钥三级优先：显式 > 环境变量 > 配置文件里读到的
    #    这条守的是 2026-10-25 那个 bug——端口从人家配置里读了，Key 却没读，
    #    结果探测显示"活着但要 Key"，模型数永远是 0，等于白探一半。
    pf = Provider(id="k", label="k", kind="gateway", base="u",
                  key_env="FLEET_TEST_KEY_X", key_hint="from-file")
    ck(pf.real_key == "from-file", "无显式无 env 时应回落配置文件，实得 %r" % pf.real_key)
    os.environ["FLEET_TEST_KEY_X"] = "from-env"
    try:
        ck(pf.real_key == "from-env", "env 应压过配置文件，实得 %r" % pf.real_key)
        pf.key = "explicit"
        ck(pf.real_key == "explicit", "显式应压过 env，实得 %r" % pf.real_key)
    finally:
        os.environ.pop("FLEET_TEST_KEY_X", None)

    # 7) _find_key_in：能挖出嵌套 mainKey，但绝不把开关/空值当密钥
    ck(_find_key_in({"a": {"b": {"mainKey": "sk-abcdefghij"}}}) == "sk-abcdefghij",
       "_find_key_in 嵌套失败")
    ck(_find_key_in({"key": ""}) == "", "空串不算密钥")
    ck(_find_key_in({"key": "true"}) == "", "开关值 true 不算密钥")
    ck(_find_key_in({"key": "abc"}) == "", "太短的不算密钥")

    # 8) key_hint 不许出现在渲染输出里（发现归发现，不许打印出去）
    leak = Provider(id="z", label="z", kind="gateway", base="http://127.0.0.1:1/v1",
                    key_hint="SUPER-SECRET-DO-NOT-PRINT")
    probe(leak, timeout=1.0)
    out = render([leak])
    ck("SUPER-SECRET" not in out, "渲染输出泄露了密钥")

    # ---- P2：路由 ----
    # 9) vendor_of：认得出的要说对，认不出的必须是 unknown（不许瞎猜成某一家）
    ck(vendor_of("deepseek-v4.1-flash-free") == "deepseek", "deepseek 判错")
    ck(vendor_of("glm-5.3-flash-free") == "zhipu", "glm 应判 zhipu")
    ck(vendor_of("zen-gate/whatever-xyz-123") == "unknown", "认不出应 unknown")

    # 10) 模型表为空就别谈支持什么
    empty = Provider(id="e", label="e", kind="gateway", base="u", alive=True, models=[])
    ck(not supports(empty, "vision"), "空模型表不能报支持")
    vis = Provider(id="v", label="v", kind="gateway", base="u", alive=True,
                   models=["qwen2-vl-7b", "deepseek-v3"])
    ck(supports(vis, "vision"), "有 vl 模型应判支持")
    ck(not supports(vis, "audio"), "没音频模型不该判支持")

    # 11) route 选不出来必须说清原因，不许静默 None
    p2, why2 = route([], need="vision")
    ck(p2 is None and why2, "选不出也要给原因")
    ck("淘汰原因" in why2 or "排除" in why2, "原因里要能看到淘汰过程：%r" % why2)

    # 12) 判据「手动关掉降级链第一环，同一轮不中断」：exclude 掉活的那个，
    #     必须自动落到下一个活的，而不是直接放弃
    a = Provider(id="llama-8080", label="a", kind="local", base="u", alive=True,
                 models=["qwen2-7b"])
    b = Provider(id="zen-gate", label="b", kind="gateway", base="u", alive=True,
                 models=["deepseek-v3"])
    ch = [a, b]
    pa, _ = route(ch)
    ck(pa.id == "llama-8080", "默认应走链首，实得 %s" % pa.id)
    pb, _ = route(ch, exclude=["llama-8080"])
    ck(pb.id == "zen-gate", "关掉第一环应自动换下一个，实得 %s" % pb.id)

    # ---- P2：额度记账 ----
    # 13) 按「厂商+账号」记：同厂商不同账号必须分开，不许合成一笔
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        q = Quota(os.path.join(td, "q.sqlite"))
        q.add("zhipu", "acct-A", tokens=100)
        q.add("zhipu", "acct-A", tokens=50)
        q.add("zhipu", "acct-B", tokens=7)
        ck(q.used("zhipu", "acct-A") == (2, 150),
           "同账号应累加，实得 %r" % (q.used("zhipu", "acct-A"),))
        ck(q.used("zhipu", "acct-B") == (1, 7),
           "不同账号必须分开，实得 %r" % (q.used("zhipu", "acct-B"),))
        ck(q.used("deepseek") == (0, 0), "没记过的不该有数")
        ck(len(q.report()) == 2, "报告应有 2 行，实得 %d" % len(q.report()))
        q.close()

    # 14) 判据「同一家内换模型」：这条是 2026-10-25 真跑出来的——
    #     zen-gate 23 个模型走不同上游车道，前两个报 429（FreeUsageLimitError），
    #     第三个 nemotron 直接成功。只会换 provider 的版本那一轮必失败。
    multi = Provider(id="m", label="m", kind="gateway", base="u", alive=True,
                     models=["lane-a", "lane-b", "lane-c"])
    ck(pick_model(multi) == "lane-a", "默认取第一个")
    ck(pick_model(multi, skip=["lane-a"]) == "lane-b", "跳过失败车道应取下一个")
    ck(pick_model(multi, skip=["lane-a", "lane-b"]) == "lane-c", "应继续往后找")
    ck(pick_model(multi, skip=["lane-a", "lane-b", "lane-c"]) is None,
       "全试过必须返回 None（好让上层换 provider），不许回头重复试")

    if fails:
        print("providers selftest: FAIL")
        for f in fails:
            print("  x " + f)
        return 1
    print("providers selftest: OK（%d 个内置端点）" % len(provs))
    return 0


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    if "--json" in argv:
        _p, data = snapshot()
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if "--snapshot" in argv:
        path, data = snapshot()
        print("已落盘：%s（%d 个模型）" % (path, len(data["models"])))
        return 0
    provs, notes = discover()
    probe_all(provs)
    print(render(provs, notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
