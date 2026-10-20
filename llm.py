"""本地推理适配层：让「她」从占位回声变成真会说话（stdlib-only，多后端，优雅降级）。

不绑定任何框架：三个后端按环境变量优先，**改环境变量即可切换，业务代码零改动**
（这正是「代码即通用胶水」——AI 让所有设备都用同一套可替换的胶水说话）：

  1) FLEET_LLM_CMD   自定义命令行。命令里含 {prompt} 占位则替换后执行；否则把 prompt
                     经 stdin 喂入，stdout 即回复（逐行回调=流式）。
                     例：FLEET_LLM_CMD='ollama run qwen3:4b'
                         FLEET_LLM_CMD='llama-cli -m models/qwen3-4b.gguf -p {prompt}'
  2) FLEET_LLM_URL   本地 OpenAI 兼容端点（llama.cpp server / MLC / Ollama / vLLM 均暴露它）。
                     POST {url}/v1/chat/completions；FLEET_LLM_MODEL 指定模型名。
  3) 都没有           -> 占位实现（backend=placeholder），分片模拟流式，链路仍可端到端跑通。

统一入口（node_agent 的 compute / ai 通道都用它）：
  complete(prompt, model=None, system=None)                 -> (text, backend)
  stream(prompt, model=None, system=None, on_delta=None)    -> (text, backend)

「她」跑在哪台设备不重要——只要那台设备接了任一后端，她就在那里真说话。
"""
import json
import os
import shlex
import subprocess
import urllib.request

TIMEOUT = float(os.environ.get("FLEET_LLM_TIMEOUT", "120"))


def backend():
    """当前生效的推理后端（改环境变量即可切换）。"""
    if os.environ.get("FLEET_LLM_CMD"):
        return "cmd"
    if os.environ.get("FLEET_LLM_URL"):
        return "http"
    return "placeholder"


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
                                stderr=subprocess.DEVNULL, text=True)
    else:
        proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
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


def _stream_http(prompt, model, system, on_delta, do_stream):
    url = os.environ["FLEET_LLM_URL"].rstrip("/")
    if not url.endswith("/v1/chat/completions"):
        url += "/v1/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(_chat_payload(prompt, model, system, do_stream)).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    parts = []
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if not do_stream:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
            text = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
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
            delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
            parts.append(delta)
            _emit(on_delta, delta)
    return "".join(parts).strip()


def stream(prompt, model=None, system=None, on_delta=None):
    """流式推理：逐段回调 on_delta，最后返回 (完整文本, 后端名)。任何后端都不抛。"""
    b = backend()
    emitted = []

    def _d(x):
        emitted.append(x)
        if on_delta:
            on_delta(x)

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
            text = _stream_placeholder(prompt, _d)
    except Exception as e:
        text = f"〔她·出错〕本地推理失败：{e}"
        _emit(on_delta, text)
    return text, b


def complete(prompt, model=None, system=None):
    """一次拿到完整回复。"""
    return stream(prompt, model=model, system=system, on_delta=None)


if __name__ == "__main__":
    print("当前后端:", backend())
    t, b = complete("你好，介绍一下你自己")
    print(f"[{b}] {t}")
