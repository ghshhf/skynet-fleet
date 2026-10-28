"""算力层：把「谁来算这件事」变成可配置、可验证的一件事。

为什么单独长出一层（2026-10-29 主人定调）
---------------------------------------
在这之前，算力是**写死的实现细节**：

  · 决策   → 环境变量 `AT_DECIDER_URL`，默认 `127.0.0.1:18080`
  · 对话   → `ai_backends` 那张表（免 key 的 pollinations / OVH）
  · 看图   → `providers(need="vision")` 从网关里挑
  · 语音   → 另有一处

四处各写各的，用户在桌面上一个都看不见、改不了。于是「本地小模型 58 选 1
要 52.9 秒」这种**物理限制**被当成**能力上限**，一路去抠模型、抠分域、抠提示词。

同一句问句、同样 58 个工具的实测对照（2026-10-29）：

    本地 Qwen3.5-2B · 核显 CPU     2874 tokens / 52.9 秒 / 超时，永远失败
    外接 pollinations（免 key）    3538 tokens /  8.6 秒 / 一次就对

⇒ 不是「模型不行」，是「算力选错了」。所以算力必须是**用户能改的东西**，
而不是散落在四个 env 里的实现细节。

三件事，别混
------------
1. **注册表**（本模块）：有哪些算力、怎么连、健康不健康。
2. **路由** `route(purpose)`：某个用途（decide / chat / vision / voice）走哪个。
3. **凭据**：单独文件、单独说明，**绝不进日志、快照、审计、前端回包**。

凭据的实话（不学 `snapshot()` 那种「注释写不落盘、实际落盘」）
------------------------------------------------------------
用户的 key **必须落盘**，否则每次开桌面端都要重填。所以：

  · 存 `~/.fleet/compute_keys.json`，与配置分开（配置可以给人看，key 不行）
  · 读出来只在内存用，`to_public()` 一律 `***` + 末四位
  · 写文件时尽力收紧权限（POSIX 0o600；Windows 上 setx 不了 ACL 就明说）

**不假装它不落盘**。落盘就要有落盘的处置：文件位置写在这、门禁盯着它，
而不是在注释里立个规矩然后没人执行。

探测为什么不能只看「有回答」（铁律：HTTP 200 ≠ 发生了一件事）
-----------------------------------------------------------
llama-server 模板编译失败时**不返回 500**，而是 HTTP 200 + 把 jinja 错误当回答。
所以 probe 要问的是**副作用**，不是格式：

  · 有没有 `usage`（真的编了 token 才有数字）
  · `prompt_tokens` 是否随输入变长而变长（恒等 = prompt 没进去）
  · 回答里有没有 `Invalid template variable` 这类组件错误原文

三条只要缺一，就判「通了但没真算」，而不是「通」。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import ai_backends as AB          # 复用失败分类（auth/rate/transient 已经有判据）

# ---------------------------------------------------------------------------
# 存哪
# ---------------------------------------------------------------------------

_DIR = os.path.join(os.path.expanduser("~"), ".fleet")
CONF_PATH = os.path.join(_DIR, "compute.json")
KEYS_PATH = os.path.join(_DIR, "compute_keys.json")

#: 用途 → 中文名。UI 与错误信息都用这张表，别在别处另抄一份。
PURPOSES = {
    "decide": "决策（她说的话该调哪个工具）",
    "chat": "对话（聊天、问答）",
    "vision": "看图（屏幕理解）",
    "voice": "语音（听写、朗读）",
}


# ---------------------------------------------------------------------------
# 内置推荐清单
# ---------------------------------------------------------------------------

def _builtin():
    """内置端点。`builtin=True` 的条目**不可删**，只能停用（stop 字段）。

    地址一律写到 `/v1` 为止（OpenAI 兼容的通行惯例）：
    拼 chat 是 base + `/chat/completions`。少写或多写一层 `/v1`，
    表现是 404 —— 而 404 会被误读成「这个厂商不支持」，所以统一在这一处收口。
    """
    return [
        # ---- 免 key：零配置就能用，作为开箱默认 ----
        Endpoint(id="pollinations", title="Pollinations（免 key）",
                 base_url="https://text.pollinations.ai/v1", model="openai",
                 builtin=True,
                 note="零配置可用。实测 58 个工具 8.6 秒一次选对。"),
        Endpoint(id="ovh", title="OVHcloud（免 key · 低频兜底）",
                 base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
                 model="gpt-oss-20b", builtin=True,
                 note="免 key，限频较紧，只当兜底。"),

        # ---- 付费：用户自己的 key。写清楚去哪拿，别让人搜 ----
        Endpoint(id="deepseek", title="DeepSeek（自己的 key）",
                 base_url="https://api.deepseek.com/v1", model="deepseek-chat",
                 need_key=True, builtin=True,
                 note="国内直连，便宜。key 在 platform.deepseek.com 拿。"),
        Endpoint(id="dashscope", title="阿里百炼 · 通义（自己的 key）",
                 base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                 model="qwen-plus", need_key=True, builtin=True,
                 note="Qwen 官方 OpenAI 兼容口。key 在 bailian.console.aliyun.com 拿。"),
        Endpoint(id="moonshot", title="Kimi · 月之暗面（自己的 key）",
                 base_url="https://api.moonshot.cn/v1", model="moonshot-v1-8k",
                 need_key=True, builtin=True,
                 note="长上下文友好。key 在 platform.moonshot.cn 拿。"),
        Endpoint(id="zhipu", title="智谱 GLM（自己的 key）",
                 base_url="https://open.bigmodel.cn/api/paas/v4",
                 model="glm-4-flash", need_key=True, builtin=True,
                 note="GLM-4-Flash 有免费额度。key 在 bigmodel.cn 拿。"),
        Endpoint(id="siliconflow", title="硅基流动（自己的 key）",
                 base_url="https://api.siliconflow.cn/v1",
                 model="Qwen/Qwen3-8B", need_key=True, builtin=True,
                 note="一个 key 调多家开源模型。key 在 cloud.siliconflow.cn 拿。"),
        Endpoint(id="openrouter", title="OpenRouter（自己的 key）",
                 base_url="https://openrouter.ai/api/v1",
                 model="qwen/qwen3-8b", need_key=True, builtin=True,
                 note="海外聚合，模型最全。需要能连出去。"),

        # ---- 本地：断网时唯一能用的一条 ----
        # 这两条**默认关着**：它们是「装了才有」的，服务大概率没起。
        # 开着的话路由会把它们排进候选，表现为每次决策先连一个死端口。
        # 要用就去算力页把开关打开 —— 主动打开这个动作本身就是「我知道它在跑」。
        Endpoint(id="llama-decide", title="本机 llama.cpp · 决策口",
                 base_url="http://127.0.0.1:18080/v1", model="", builtin=True,
                 enabled=False,
                 note="本地小模型。58 个工具扛不住（52.9s），只适合域内选择。"
                      "要用请先打开开关。"),
        Endpoint(id="ollama", title="本机 Ollama",
                 base_url="http://127.0.0.1:11434/v1", model="", builtin=True,
                 enabled=False,
                 note="装了 Ollama 才有。模型名留空则自动取第一个。"
                      "要用请先打开开关。"),
    ]


class Endpoint:
    """一个可用的算力端点。

    `base_url` 到 `/v1` 为止。运行时状态（ok/ms/err）不落盘 —— 它是「这一刻
    的事实」，写进文件第二天就成了假话。
    """

    __slots__ = ("id", "title", "base_url", "model", "need_key", "builtin",
                 "note", "key", "enabled",
                 "ok", "ms", "err", "seen_model", "checked_at")

    def __init__(self, id, title, base_url, model="", need_key=False,
                 builtin=False, note="", key="", enabled=True):
        self.id = id
        self.title = title
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.need_key = bool(need_key)
        self.builtin = bool(builtin)
        self.note = note or ""
        self.key = key or ""
        self.enabled = bool(enabled)
        # 运行时
        self.ok = None            # None=没测过
        self.ms = 0
        self.err = ""
        self.seen_model = ""      # 端点自己报的模型名（可能跟 model 配置不一样）
        self.checked_at = 0.0

    # ---- 拼地址：只有这一处知道怎么拼 ----
    def url(self, path):
        """/v1 之后的路径。`path` 写 `chat/completions` 这种，不带前导斜杠。"""
        return self.base_url + "/" + path.lstrip("/")

    def has_key(self):
        return bool(self.key)

    def ready(self):
        """能不能拿去用：配了 key 的必须有 key。"""
        return self.enabled and (self.has_key() if self.need_key else True)

    # ---- 出去给人看：key 一律遮 ----
    def to_public(self):
        return {
            "id": self.id,
            "title": self.title,
            "base_url": self.base_url,
            "model": self.model,
            "need_key": self.need_key,
            "builtin": self.builtin,
            "note": self.note,
            "enabled": self.enabled,
            "has_key": self.has_key(),
            # 遮法：只留末四位。空 key 明确给空串，不要给 `***` 让人以为配过了。
            "key_hint": ("***" + self.key[-4:]) if self.key else "",
            "ready": self.ready(),
            "ok": self.ok,
            "ms": self.ms,
            "err": self.err,
            "seen_model": self.seen_model,
            "checked_at": self.checked_at,
        }

    def to_conf(self):
        """落盘形态。**不含 key**（key 由 _save_keys 单独写）。"""
        return {"id": self.id, "title": self.title, "base_url": self.base_url,
                "model": self.model, "need_key": self.need_key,
                "builtin": self.builtin, "note": self.note,
                "enabled": self.enabled}


# ---------------------------------------------------------------------------
# 落盘 / 读盘
# ---------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件坏了不该让整个桌面端起不来：按「没配过」处理，但清掉坏文件
        # 之前先留个 .bad 备份 —— 用户可能手改过，直接删等于吞他的东西。
        try:
            os.replace(path, path + ".bad")
        except Exception:
            pass
        return {}


def _write_json(path, obj, secret=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if secret:
        # POSIX 上真收紧；Windows 上 chmod 基本无效，所以不假装它是道防线。
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def _load():
    """内置清单 + 用户配置合并。用户配置按 id 覆盖内置项（标题/模型/key 都可改）。"""
    conf = _read_json(CONF_PATH)
    keys = _read_json(KEYS_PATH)

    saved = {}
    for row in conf.get("endpoints") or []:
        if isinstance(row, dict) and row.get("id"):
            saved[str(row["id"])] = row

    out = []
    seen = set()
    for ep in _builtin():
        row = saved.get(ep.id) or {}
        ep.title = row.get("title") or ep.title
        ep.base_url = (row.get("base_url") or ep.base_url).rstrip("/")
        ep.model = row.get("model", ep.model) or ""
        if "enabled" in row:
            ep.enabled = bool(row["enabled"])
        if "need_key" in row:
            ep.need_key = bool(row["need_key"])
        ep.key = keys.get(ep.id, "") or ""
        out.append(ep)
        seen.add(ep.id)

    # 用户自己加的（builtin 为假的）
    for eid, row in saved.items():
        if eid in seen:
            continue
        out.append(Endpoint(
            id=eid,
            title=row.get("title") or eid,
            base_url=row.get("base_url") or "",
            model=row.get("model") or "",
            need_key=bool(row.get("need_key")),
            builtin=False,
            note=row.get("note") or "",
            key=keys.get(eid, "") or "",
            enabled=bool(row.get("enabled", True)),
        ))
    return out, conf


def _save(endpoints, conf):
    _write_json(CONF_PATH, {
        "endpoints": [e.to_conf() for e in endpoints if not e.builtin or _dirty(e)],
        "route": conf.get("route") or {},
    })
    _write_json(KEYS_PATH,
                {e.id: e.key for e in endpoints if e.key},
                secret=True)


def _dirty(ep):
    """内置项只有在被改过时才落盘（否则配置文件里全是原样抄的噪音）。"""
    for b in _builtin():
        if b.id == ep.id:
            return any((
                ep.title != b.title, ep.base_url != b.base_url,
                ep.model != b.model, ep.enabled != b.enabled,
                ep.need_key != b.need_key,
            ))
    return True


# ---------------------------------------------------------------------------
# 对外：列表 / 取 / 存 / 删 / 探测
# ---------------------------------------------------------------------------

def list_all():
    """全部端点（含内置）。键已遮。"""
    eps, _conf = _load()
    return [e.to_public() for e in eps]


def get(eid):
    eps, _ = _load()
    for e in eps:
        if e.id == eid:
            return e
    return None


def upsert(row):
    """新增或修改一个端点。返回 (public, err)。

    改内置项只覆盖允许改的字段（title/base_url/model/enabled），
    `builtin` 标记不接受外部输入 —— 否则前端一句 `"builtin": false`
    就能把内置项变成可删的。
    """
    eid = str(row.get("id") or "").strip()
    if not eid:
        return None, "缺少 id"
    base = str(row.get("base_url") or "").strip().rstrip("/")
    if not base:
        return None, "缺少 base_url"
    if not (base.startswith("http://") or base.startswith("https://")):
        return None, "base_url 必须以 http:// 或 https:// 开头"

    eps, conf = _load()
    target = None
    for e in eps:
        if e.id == eid:
            target = e
            break

    if target is None:
        target = Endpoint(id=eid, title=str(row.get("title") or eid),
                          base_url=base, model=str(row.get("model") or ""),
                          need_key=bool(row.get("need_key")), builtin=False)
        eps.append(target)
    target.base_url = base
    if row.get("title"):
        target.title = str(row["title"]).strip()
    if "model" in row:
        target.model = str(row.get("model") or "").strip()
    if "need_key" in row:
        target.need_key = bool(row["need_key"])
    if "enabled" in row:
        target.enabled = bool(row["enabled"])
    if row.get("note"):
        target.note = str(row["note"]).strip()

    # key：显式传空串=清空；不传=保持原样。**这两种必须分清**，
    # 否则用户改个模型名就把 key 洗掉了，而且看不出来。
    if "key" in row:
        target.key = str(row.get("key") or "").strip()

    _save(eps, conf)
    return target.to_public(), ""


def remove(eid):
    """删自定义端点。内置项不许删（可以 enabled=false 停用）。"""
    eps, conf = _load()
    target = None
    for e in eps:
        if e.id == eid:
            target = e
            break
    if target is None:
        return False, "没有这个端点：%s" % eid
    if target.builtin:
        return False, "%s 是内置端点，不能删；不想要就关掉（enabled=false）" % eid
    eps = [e for e in eps if e.id != eid]
    route = conf.get("route") or {}
    for k, v in list(route.items()):
        if v == eid:
            route.pop(k)          # 路由指着它，删了就得一起清，别留悬空引用
    conf["route"] = route
    _save(eps, conf)
    return True, ""


def probe(eid, timeout=25):
    """真打一次，判断「这个端点现在能不能算」。

    返回 dict：{ok, ms, model, prompt_tokens, err, level}
      level = "ok"      真算出来了
            = "shallow" 通了但拿不到 usage（无法确认真的算了）
            = "fail"    不通
    """
    ep = get(eid)
    if ep is None:
        return {"ok": False, "level": "fail", "ms": 0,
                "err": "没有这个端点：%s" % eid}
    if ep.need_key and not ep.has_key():
        return {"ok": False, "level": "fail", "ms": 0,
                "err": "还没填 key（这个端点需要鉴权）"}

    # 用一句必须靠「读输入」才能答对的话：数字藏在 system 里。
    # 这样模型若没收到 system 就会答错 —— 跟 t46 同一条判据思路。
    secret = str(int(time.time()) % 1000)
    sysmsg = ("下面这段话里有一个三位数，只把它原样回给我，不要解释："
              "编号 %s。另外用一句话说明你收到了这条指令。" % secret)
    payload = {
        "messages": [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": "回给我那个三位数"}],
        "temperature": 0.0,
        "max_tokens": 48,
        "stream": False,
    }
    if ep.model:
        payload["model"] = ep.model

    t0 = time.time()
    try:
        req = urllib.request.Request(
            ep.url("chat/completions"),
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers=_headers(ep))
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        ms = int((time.time() - t0) * 1000)
        kind = _classify(e, body)
        _remember(ep, False, ms, "%s" % kind)
        return {"ok": False, "level": "fail", "ms": ms, "err": kind, "detail": body}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        msg = _classify(e, "")
        _remember(ep, False, ms, msg)
        return {"ok": False, "level": "fail", "ms": ms, "err": msg}

    ms = int((time.time() - t0) * 1000)
    try:
        d = json.loads(raw)
    except Exception:
        _remember(ep, False, ms, "返回的不是 JSON（可能被网关拦截）")
        return {"ok": False, "level": "fail", "ms": ms,
                "err": "返回的不是 JSON：%r" % raw[:120]}

    try:
        msg = d["choices"][0]["message"].get("content") or ""
    except Exception:
        msg = ""
    usage = d.get("usage") or {}
    ptok = int(usage.get("prompt_tokens") or 0)
    ctok = int(usage.get("completion_tokens") or 0)
    model_seen = str(d.get("model") or "")

    # ① 组件错误当回答吐回来（llama-server 模板编译失败就是这种）
    for bad in ("Invalid template variable", "jinja", "Error:", "error:"):
        if bad in msg[:120]:
            _remember(ep, False, ms, "回答里是组件报错，不是模型输出")
            return {"ok": False, "level": "fail", "ms": ms,
                    "err": "回答里是组件报错：%r" % msg[:80]}

    # ② 没有 usage = 无法确认真的编了 token
    if not ptok:
        _remember(ep, True, ms, "", level="shallow")
        return {"ok": True, "level": "shallow", "ms": ms, "model": model_seen,
                "prompt_tokens": 0,
                "err": "通了，但端点没报 usage —— 无法确认真的算了（可能是缓存或代理）"}

    # ③ 关键：那个三位数有没有出现在回答里。没出现说明 system 没进去。
    got_secret = secret in msg
    _remember(ep, True, ms, "", level="ok")
    ep.seen_model = model_seen
    out = {"ok": True, "level": "ok" if got_secret else "shallow",
           "ms": ms, "model": model_seen,
           "prompt_tokens": ptok, "completion_tokens": ctok,
           "err": ""}
    if not got_secret:
        out["ok"] = True
        out["err"] = ("通了、也算过（%d tokens），但它没回出那个三位数 —— "
                      "可能把 system 丢了，或模型太弱。看一下回答：%r" % (ptok, msg[:80]))
    return out


def _headers(ep):
    h = {"Content-Type": "application/json"}
    if ep.key:
        h["Authorization"] = "Bearer " + ep.key
    return h


def _classify(exc, body):
    """把异常说成人能照着办的话。别只说「连接失败」。

    实测教训（2026-10-29）：地址写错一个字母时，urllib 抛的不是
    `Name or service not known`，而是包在 URLError 里的
    `[SSL: UNEXPECTED_EOF_WHILE_READING]`——因为 DNS 被解析到了某个
    会接 TCP 但不说 TLS 的东西上。原来那条判据没接住，直接把这串
    C 层错误码甩给用户。所以这里先剥 URLError 的外壳，再分情况说。
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", 0)
        if code == 401:
            return "401 鉴权失败：key 不对或没填"
        if code == 403:
            return "403 被拒：key 没权限，或这个地区/模型不开放"
        if code == 404:
            return "404 找不到：base_url 少了或多了 /v1，或模型名不对"
        if code == 429:
            return "429 限流：这个端点这会儿不给算，换一个或等会儿"
        if 500 <= code < 600:
            return "%d 端点自己出错（不是你的问题，等它恢复）" % code
        return "HTTP %d：%s" % (code, (body or "")[:120])
    if isinstance(exc, (TimeoutError,)):
        return "超时：端点没在设定时间内回话"
    # 连接被拒在 Windows 中文系统上是 ConnectionRefusedError("[WinError 10061]
    # 由于目标计算机积极拒绝，无法连接。")，英文关键词一个都不含 ——
    # 只按字符串判会掉到最后的兜底分支，把 WinError 原文甩给用户。
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒：端口没开（本地服务起了吗）"
    # 剥掉 URLError 外壳：里面的 reason 才是真原因
    inner = exc
    if isinstance(exc, urllib.error.URLError):
        inner = getattr(exc, "reason", exc)
    s = str(inner)
    low = s.lower()
    if "timed out" in low or "timeout" in low:
        return "超时：端点没在设定时间内回话（可能排队，也可能网络到不了）"
    if "unexpected_eof" in low or "ssl" in low or "certificate" in low:
        return ("TLS 握手失败：地址大概写错了（域名解析到了别的东西上），"
                "或这台机器到不了它。原始：%s" % s[:80])
    if "name or service not known" in low or "getaddrinfo" in low or "nodename" in low:
        return "域名解析不了：地址写错了，或这台机器连不出去"
    if "connection refused" in low or "积极拒绝" in low or "10061" in low:
        return "连接被拒：端口没开（本地服务起了吗）"
    if "connection reset" in low or "aborted" in low or "10054" in low:
        return "连接被对方断掉：可能被中间设备拦了（公司网/运营商），换个端点试试"
    if "no route to host" in low or "unreachable" in low:
        return "网络到不了这个地址"
    return "%s: %s" % (type(inner).__name__, s[:120])


def _remember(ep, ok, ms, err, level=""):
    ep.ok = bool(ok)
    ep.ms = int(ms)
    ep.err = err
    ep.checked_at = time.time()
    # 只记内存：探测通过的那条要影响 make 路由排序（见 _rank），
    # 但不落盘 —— 昨天的「通」不代表今天通。延迟也带进去，
    # 同样通的两条里先用快的。
    _LAST_PROBE[ep.id] = (bool(ok), int(ms))


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

#: 最近一次探测结果，只在进程内有效。
#: **刻意不落盘**：探测结果是「这一刻的事实」，写进文件第二天就成了假话。
#: 但它得影响路由 —— 用户点了「测一下」，通的那个就该被优先用上，
#: 否则「我明明测通了」和「实际却走了别人」会同时对，人没法理解。
_LAST_PROBE = {}


def _is_loopback(base):
    return ("127.0.0.1" in base) or ("localhost" in base) or ("[::1]" in base)


def _rank(e):
    """路由排序，返回 (档位, 探测加成, 延迟)。数越小越先试。

    这个顺序是**踩出来的**，不是拍脑袋（2026-10-29 实际表现）：

      · 一开始按「免 key 优先」，结果配好了 zen-gate（本机 8787、40B、
        1.9 秒）却仍走海外 pollinations（7~8 秒）—— 用户配了却用不上，
        比不配更让人困惑。
      · 把本机端点一律提前也不行：`llama-decide`/`ollama` 是「装了才有」的，
        服务大概率没起，排前面等于每次决策先连一个死端口。
      ⇒ 分界线是 **localhost + 有 key**：这是「用户主动配过、且确定能连」
        的组合（zen-gate 这种本机网关）。

      · 第二轮踩到的：「刚测通」不能跨档夺权。点了「测一下」之后
        pollinations（10.2 秒）立刻把 zen-gate（1.9 秒）挤掉了 ——
        因为它进了「探测通过」那一档，而 zen-gate 这个进程里还没测过。
        **测通的语义是「它可用」，不是「选它当默认」**；要选默认，
        用户去下拉框钉死。所以探测结果只在**同档内**起作用。
    """
    tier = 4                       # 远端付费：最后
    if _is_loopback(e.base_url) and e.has_key():
        tier = 1                   # 本机 + 有 key：用户主动配过，最可控
    elif not e.need_key:
        tier = 2                   # 免 key：开箱兜底
    elif _is_loopback(e.base_url):
        tier = 3                   # 本机无 key（llama/ollama），默认不启用

    recent = _LAST_PROBE.get(e.id)
    if recent is None:
        return (tier, 1, 0)        # 没测过：同档里排在「测通了的」后面
    if recent[0]:
        return (tier, 0, recent[1])   # 测通了：同档第一，且越快越前
    return (tier, 2, 0)            # 测挂了：同档垫底


def route(purpose):
    """某个用途该用哪个端点。返回 Endpoint 或 None。

    先看用户钉死的；没钉就在**可用**的里面按 `_rank` 挑第一个。
    「可用」= `ready()`（配了 key 的必须有 key）且 enabled。
    """
    eps, conf = _load()
    pinned = (conf.get("route") or {}).get(purpose)
    if pinned:
        for e in eps:
            if e.id == pinned:
                # 钉了就用它；但它不可用就返回 None —— **绝不悄悄换人顶上**。
                # 悄悄换的后果：用户钉的是云端（为了质量），结果半夜额度用完
                # 偷偷退回本地 2B，答案质量掉一档而没人知道。
                return e if e.ready() else None
    pool = [e for e in eps if e.ready()]
    if not pool:
        return None
    return min(pool, key=_rank)


def set_route(purpose, eid):
    """钉死某用途走哪个端点。eid 传空串 = 取消钉死。"""
    if purpose not in PURPOSES:
        return False, "未知用途：%s（可选：%s）" % (purpose, "、".join(PURPOSES))
    eps, conf = _load()
    if eid:
        if not any(e.id == eid for e in eps):
            return False, "没有这个端点：%s" % eid
    r = conf.get("route") or {}
    if eid:
        r[purpose] = eid
    else:
        r.pop(purpose, None)
    conf["route"] = r
    _save(eps, conf)
    return True, ""


def routes():
    """当前每个用途实际会走哪个（解析后的结果，不是配置原文）。"""
    eps, conf = _load()
    pinned = conf.get("route") or {}
    out = {}
    for p in PURPOSES:
        ep = route(p)
        out[p] = {
            "purpose": p,
            "label": PURPOSES[p],
            "pinned": pinned.get(p) or "",
            "endpoint": ep.id if ep else "",
            "title": ep.title if ep else "",
            "model": (ep.model or ep.seen_model) if ep else "",
        }
    return out


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------

def _selftest():
    import tempfile
    global _DIR, CONF_PATH, KEYS_PATH

    ok = fail = 0

    def ck(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  ok   %s" % msg)
        else:
            fail += 1
            print("  FAIL %s" % msg)

    # 用临时目录，绝不碰真实的 ~/.fleet
    tmp = tempfile.mkdtemp(prefix="compute_st_")
    _DIR = tmp
    CONF_PATH = os.path.join(tmp, "compute.json")
    KEYS_PATH = os.path.join(tmp, "compute_keys.json")

    eps = _load()[0]
    ck(len(eps) >= 8, "内置端点 %d 个" % len(eps))
    ck(all(e.base_url.startswith("http") for e in eps), "每个端点都有 base_url")
    ck(all(e.base_url.rstrip("/").endswith("/v1")
           or "/api/paas/v4" in e.base_url
           or "/compatible-mode/v1" in e.base_url for e in eps),
       "base_url 都写到版本层（不会再被拼一层 /v1）")

    d = get("deepseek")
    ck(d is not None and d.need_key and not d.ready(),
       "需要 key 的端点没 key 时 ready()=False（不会拿 401 去试）")

    pub, err = upsert({"id": "mine", "title": "我的中转", "base_url": "https://x.test/v1",
                       "model": "m", "need_key": True, "key": "sk-abcdefgh1234"})
    ck(not err and pub["id"] == "mine", "新增自定义端点")
    ck(pub["key_hint"] == "***1234", "key 只回末四位（实得 %r）" % pub["key_hint"])
    ck("sk-abcdefgh1234" not in json.dumps(pub), "公开视图里没有完整 key")

    ck(os.path.exists(KEYS_PATH), "key 落单独文件（明确落盘，不假装）")
    ck(not os.path.exists(os.path.join(tmp, "compute.json")) or
       "sk-abcdefgh1234" not in open(os.path.join(tmp, "compute.json"),
                                     encoding="utf-8").read(),
       "compute.json 里没有 key")

    # 不传 key ≠ 清空 key
    pub2, _ = upsert({"id": "mine", "base_url": "https://x.test/v1", "model": "m2"})
    ck(pub2["has_key"], "改模型名不会把 key 洗掉")
    pub3, _ = upsert({"id": "mine", "base_url": "https://x.test/v1", "key": ""})
    ck(not pub3["has_key"], "显式传空 key 才是清空")

    ck(not remove("pollinations")[0], "内置端点不许删")
    ck(remove("mine")[0], "自定义端点可删")

    # 路由：钉死 → 取消 → 回到默认
    ck(set_route("decide", "deepseek")[0], "钉死决策走 deepseek")
    ck(route("decide") is None,
       "钉死的端点没 key 时 route() 返回 None（不静默换别的顶上）")
    upsert({"id": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "key": "sk-test"})
    r = route("decide")
    ck(r is not None and r.id == "deepseek", "填了 key 之后才走它")
    ck(set_route("decide", "")[0], "取消钉死")
    r2 = route("decide")
    ck(r2 is not None and not r2.need_key, "取消后回到免 key 端点")

    ck(set_route("nope", "x")[0] is False, "未知用途会被拒")

    # base_url 校验
    ck(upsert({"id": "bad", "base_url": "x.test/v1"})[1] != "", "缺协议头会被拒")

    # route 视图
    rr = routes()
    ck(set(rr) == set(PURPOSES), "routes() 覆盖全部用途")

    print()
    print("compute selftest: %d ok / %d fail" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
