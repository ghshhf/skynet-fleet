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
                  "FLEET_LLM7_TOKEN"):
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
    # ③ llm7：免费但要 token（每天 10 万 token）——有才上，没有自动离线
    tok = os.environ.get("FLEET_LLM7_TOKEN")
    if tok:
        bs.append({
            "id": "llm7", "title": "llm7（token）",
            "protocol": "openai", "base_url": "https://api.llm7.io/v1",
            "model": "gpt-4o-mini", "key_env": "FLEET_LLM7_TOKEN",
            "need_key": True, "key": tok,
        })
    # ④ 自定义端点：来自 local.env [ai_custom]（用户给的免费 token 落地处）
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
