"""推理适配层：让「她」从占位回声变成真会说话（stdlib-only，多后端，优雅降级）。

不绑定任何框架：**后端链**按顺序试，第一个能用的就赢，改环境变量即可切换，
业务代码零改动。

## 后端链（今晚定下来的双脑结构）

实测教训：0.6B（北极星 v13）**载不动**完整人格卡——体检结果
「你是谁？」答"要不换个写法？"、多轮问名字直接吐出 python 代码。
这就是计划书里那句「卡救不了 0.6B」的实证，不是玄学。

所以链路是：

1. **云端增强**（`FLEET_CLOUD_URL` + `FLEET_CLOUD_KEY` + `FLEET_CLOUD_MODEL`）
   —— 免费端点，人格承载力实测远好于本地小模型（自称、口癖、动作都对）。
   联网、有隐私代价，所以默认**不自动开**，要显式配置才生效。
2. **本地**（`FLEET_LLM_URL` / `FLEET_LLM_CMD`）—— 离线、隐私、快，是保底。
3. **占位** —— 什么都没配时的回声，链路仍可端到端跑通。

链内每一步失败自动落到下一步；**云端先失败不会影响本地接管**。

统一入口：
  complete(prompt, model=None, system=None)                 -> (text, backend)
  stream(prompt, model=None, system=None, on_delta=None)    -> (text, backend)

后端优先序（学 dsh-our-free-model 的免费多端思路）：
  **免费多端(pollinations/ovh/llm7，failover+冷却+健康门控)** -> 云端增强 -> 本地 -> 占位
  免费多端默认开（FLEET_USE_FREE=0 关，退回原 cloud/local/mock 链）；
  开箱没配任何 cloud/local 时，大脑子也能经免费端点真说话，不再只会回声。

「她」跑在哪台设备不重要——只要那台设备接了任一后端，她就在那里真说话。
"""
import json
import os
import shlex
import subprocess
import urllib.request

TIMEOUT = float(os.environ.get("FLEET_LLM_TIMEOUT", "120"))
CLOUD_TIMEOUT = float(os.environ.get("FLEET_CLOUD_TIMEOUT", "45"))
# 云端连续失败几次后本会话内不再重试（避免每次对话都等它超时）
_CLOUD_FAIL = {"n": 0}
_CLOUD_MAX_FAIL = int(os.environ.get("FLEET_CLOUD_MAX_FAIL", "3"))


def backend():
    """当前生效的推理后端（改环境变量即可切换）。"""
    if os.environ.get("FLEET_LLM_CMD"):
        return "cmd"
    if os.environ.get("FLEET_LLM_URL"):
        return "http"
    return "placeholder"


def backends():
    """后端链（全量），供体检/状态展示。"""
    chain = []
    if os.environ.get("FLEET_CLOUD_URL"):
        chain.append("cloud")
    b = backend()
    if b != "placeholder":
        chain.append(b)
    chain.append("mock")
    return chain


def _placeholder_text(prompt):
    p = (prompt or "").strip().replace("\n", " ")
    snippet = p[:80] + ("…" if len(p) > 80 else "")
    return (f"〔她·占位〕我听见了：「{snippet}」。"
            "现在还没接上本地模型——设 FLEET_LLM_CMD 或 FLEET_LLM_URL 后我就真能回你了。")


def _emit(on_delta, chunk):
    if on_delta and chunk:
        on_delta(chunk)


def _stream_placeholder(prompt, on_delta):
    text = _placeholder_text(prompt)
    step = max(1, len(text) // 6)
    for i in range(0, len(text), step):
        _emit(on_delta, text[i:i + step])
    return text


def _stream_cmd(prompt, model, on_delta):
    cmd = os.environ["FLEET_LLM_CMD"]
    if "{prompt}" in cmd:
        proc = subprocess.Popen(cmd.replace("{prompt}", shlex.quote(prompt)),
                                shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace")
    else:
        proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace")
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
    parts = []
    for line in proc.stdout:
        parts.append(line)
        _emit(on_delta, line)
    proc.wait(timeout=TIMEOUT)
    return "".join(parts).strip()


def _chat_payload(prompt, model, system, do_stream):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return {"model": model or os.environ.get("FLEET_LLM_MODEL", "default"),
            "messages": msgs, "stream": do_stream,
            "temperature": float(os.environ.get("FLEET_LLM_TEMP", "0.7"))}


def _post_json(url, payload, key=None, timeout=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _chat_url(base):
    """把 base 规整成 OpenAI 兼容的 chat/completions 完整地址。

    要能吃下这几种写法（用户/脚本给的地址形式很杂）：
        http://h:p/v1
        http://h:p/v1/chat/completions
        http://h:p
        http://h:p/
    """
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _cloud_complete(prompt, model, system):
    """云端增强后端。返回 (text, 'cloud')，失败抛异常让链子往下走。"""
    if _CLOUD_FAIL["n"] >= _CLOUD_MAX_FAIL:
        raise RuntimeError("云端后端本会话已连续失败 %d 次，暂时跳过"
                           % _CLOUD_FAIL["n"])
    url = _chat_url(os.environ["FLEET_CLOUD_URL"])
    payload = _chat_payload(prompt, model or os.environ.get("FLEET_CLOUD_MODEL"),
                            system, False)
    obj = _post_json(url, payload,
                     key=os.environ.get("FLEET_CLOUD_KEY"),
                     timeout=CLOUD_TIMEOUT)
    text = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    if not text.strip():
        raise RuntimeError("云端返回空内容")
    _CLOUD_FAIL["n"] = 0
    return text.strip()


def _stream_http(prompt, model, system, on_delta, do_stream):
    url = _chat_url(os.environ["FLEET_LLM_URL"])
    headers = {"Content-Type": "application/json"}
    # 带 key 的端点（免费 AI 中继 / 商业 API）：只认环境变量，不落盘、不进产物
    key = os.environ.get("FLEET_LLM_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        url, data=json.dumps(_chat_payload(prompt, model, system, do_stream)).encode("utf-8"),
        headers=headers)
    parts = []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if not do_stream:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
            text = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            if not text.strip():
                raise RuntimeError("本地端点返回空内容")
            parts.append(text)
            _emit(on_delta, text)
            return text.strip()
        # OpenAI 兼容 SSE：data: {...} 逐行 / 结束符 data: [DONE]
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if not delta:
                continue
            parts.append(delta)
            _emit(on_delta, delta)
    if not "".join(parts).strip():
        raise RuntimeError("本地端点流式返回为空")
    return "".join(parts).strip()


def _mock_complete(prompt, model, system, on_delta=None):
    """宿主模拟后端：真模型没接上时，让「她」先按人设开口。"""
    try:
        import mock_brain
        text, b = mock_brain.complete(prompt, model, system)
    except Exception:
        text = _placeholder_text(prompt)
        b = "mock"
    if on_delta:
        on_delta(text)
    return text


def _free_complete(prompt, model, system, on_delta):
    """免费多端后端：复用 ai_backends.chat_fallback（pollinations/ovh/llm7 降级链 +
    冷却 + 健康门控）。返回 (text, 'free:<实际用的后端>')；任何失败抛异常让上层降级。

    这是「先走云端免费」的落地：开箱（没配任何 cloud/local）时，大脑子也能通过
    免费端点真说话，而不是只会回声；某条免费腿挂了自动切下一条，不坐实限流/封禁。
    """
    from fleet import ai_backends as AB
    if not AB.backends():
        raise RuntimeError("无可用免费后端（连 pollinations 都没起来）")
    text, via = AB.chat_fallback(
        prompt, system=system or "",
        on_token=on_delta,
        max_tokens=int(os.environ.get("FLEET_LLM_MAX_TOKENS", "1024")),
        timeout=CLOUD_TIMEOUT,
    )
    if not text.strip():
        raise RuntimeError("免费多端均返回空正文")
    return text, "free:" + via


def stream(prompt, model=None, system=None, on_delta=None):
    """流式推理：逐段回调 on_delta，最后返回 (完整文本, 后端名)。任何后端都不抛。

    后端链：云端增强 -> 本地 -> 占位。
    """
    b = backend()
    emitted = []

    def _d(x):
        emitted.append(x)
        if on_delta:
            on_delta(x)

    # ---- 0) 免费多端（pollinations/ovh/llm7，failover+冷却，先走云端免费）----
    # 直接复用 ai_backends.chat_fallback（失败分类 + 冷却 + 健康门控 + 降级链都已做好）。
    # 默认开；FLEET_USE_FREE=0 关掉，退回原 cloud/local/mock 链。
    if os.environ.get("FLEET_USE_FREE", "1") != "0":
        try:
            text, bk = _free_complete(prompt, model, system, on_delta)
            return text, bk
        except Exception as e:
            print("[llm] 免费多端失败，降级到已配置后端：%s" % e)

    # ---- 1) 云端增强（可选，配了才走）----
    if os.environ.get("FLEET_CLOUD_URL"):
        try:
            text = _cloud_complete(prompt, model, system)
            _d(text)
            return text, "cloud"
        except Exception as e:
            _CLOUD_FAIL["n"] += 1
            if not emitted:
                _d("〔她〕云端没接上，先用本地接着说。")
                emitted.clear()          # 这句是状态播报，不算正文
            print("[llm] 云端后端失败：%s" % e)

    # ---- 2) 本地 ----
    try:
        if b == "cmd":
            text = _stream_cmd(prompt, model, _d)
        elif b == "http":
            try:
                text = _stream_http(prompt, model, system, _d, True)
            except Exception:
                if emitted:
                    raise          # 已吐过片段，不再回退，避免重复
                text = _stream_http(prompt, model, system, _d, False)
        else:
            # 没配本地后端：默认走宿主模拟（今晚先能聊），可用 FLEET_USE_MOCK=0 关掉
            if os.environ.get("FLEET_USE_MOCK", "1") == "0":
                text = _stream_placeholder(prompt, _d)
            else:
                text = _mock_complete(prompt, model, system, _d)
                return text, "mock"
    except Exception as e:
        print("[llm] 本地后端失败：%s" % e)
        text = _mock_complete(prompt, model, system, _d)
        return text, "mock"
    return text, b


def complete(prompt, model=None, system=None):
    """一次拿到完整回复。"""
    return stream(prompt, model=model, system=system, on_delta=None)


if __name__ == "__main__":
    print("当前后端:", backend())
    t, b = complete("你好，介绍一下你自己")
    print(f"[{b}] {t}")
