# -*- coding: utf-8 -*-
"""把 llmgw 暴露成一个 OpenAI 兼容端点，供客户端当「自定义节点」使用。

为什么要有这一层
----------------
`llmgw.*` 是**能力接口**（在进程内被 CLI / 手机 / MCP 调用），客户端要的是
**HTTP + OpenAI 协议**。中间缺一层，客户端就只能直连 zen-gate 那种第三方网关——
22 个模型里有两组重名（Nemotron Super VL / Lightning 各两份），模型名是上游
原样透传的，人分不清哪个是哪个，更分不清哪些其实是同一台机器。

这一层补的就是这个：
  GET  /v1/models            → 只报**分类**（claude/gpt/cn/coder/big/fast/vision）
                               可加 ?all=1 看归并后的真实模型名
  POST /v1/chat/completions  → model 填分类名就走那条链；填真名就点名那个模型
  GET  /health               → 家底摘要（端点/模型数/可用性），给人看的

设计上的三条取舍（都是踩过的）：
1. **只报分类，不默认报 75 个真名**。客户端下拉框不是数据库，塞 75 条没人选得动；
   分类是「我要哪一类智力」，真实模型由链去挑。
2. **stream=true 是真流式**：上游吐一块就转一块（_sse_once 的 on_delta 直推 socket），
   首字节 1.3s 就出现在客户端，不用等上游把整段说完。首字节前的候选失败一个 delta
   都不会发出去，所以「换下一跳」对已经发出去的内容永远无害。
   早期版本是「整段一次性发」——SSE 帧是真的、流式是假的：客户端要等全部说完才有字。
3. **不校验 API key**。绑 127.0.0.1，只给本机用；客户端随便填个 key 都能连。
   要对外必须另加鉴权，这里不做——做了就是「以为安全其实没有」。

启动：`python -m fleet.llmgw_server --port 8790`
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .catalog import build
from .kinds import llmgw_device as L

_DEFAULT_PORT = 8790
_DEV_LOCK = threading.Lock()
_DEV = None


def _dev():
    """catalog 构一次就复用：每请求重建会把端点探测全跑一遍，慢得没道理。"""
    global _DEV
    if _DEV is None:
        with _DEV_LOCK:
            if _DEV is None:
                _DEV = build().device("llmgw:bench")
    return _DEV


def _classes():
    return list(L.MODEL_CLASSES.keys())


def _model_rows(all_models=False):
    """给 /v1/models 用的行。默认只有分类；all_models 时追加归并后的真名。"""
    rows = []
    for name, spec in L.MODEL_CLASSES.items():
        rows.append({
            "id": name,
            "object": "model",
            "owned_by": "tianwang-llmgw",
            "description": spec["desc"],
            "chain": ["%s:%s" % (ep, m) for ep, m in spec["chain"]],
        })
    if all_models:
        seen = {}
        for ep, spec in _ENDPOINT_HINTS.items():
            for m in spec:
                k = L._model_identity(m)
                seen.setdefault(k, {"id": k, "object": "model",
                                    "owned_by": "tianwang-llmgw", "via": []})
                if ep not in seen[k]["via"]:
                    seen[k]["via"].append(ep)
        rows.extend(sorted(seen.values(), key=lambda x: x["id"]))
    return rows


# 真实模型清单（归并用）。只放「声明过、实测能打」的，不拿 /v1/models 全集充数。
_ENDPOINT_HINTS = {}


def _refresh_hints():
    """把各端点的声明清单抓一次，供 ?all=1 归并显示。失败静默——这只是一份展示名单。"""
    try:
        r = _dev().invoke("llmgw.models", {}).as_dict()
        eps = (r.get("data") or {}).get("endpoints") or {}
        _ENDPOINT_HINTS.clear()
        for ep, ev in eps.items():
            _ENDPOINT_HINTS[ep] = [m["id"] for m in (ev.get("models") or [])]
    except Exception:
        pass


def _chat(payload, on_delta=None):
    """把 OpenAI 请求转成 llmgw.chat 的调用。返回 (data_dict, err_str)。

    on_delta 非空时走真流式：上游每吐一块就回调一次（含换上游前一个 delta 都没发的情况）。
    """
    model = (payload.get("model") or "").strip()
    messages = payload.get("messages")
    if not messages:
        return None, "messages 必填"
    args = {"messages": messages}
    if model.lower() in L.MODEL_CLASSES:
        args["class"] = model.lower()
        args["_class_name"] = model.lower()
    elif model:
        args["model"] = model
    for k in ("temperature", "max_tokens", "top_p", "session", "timeout",
              "tools", "tool_choice", "compress", "fanout"):
        if payload.get(k) is not None:
            args[k] = payload[k]
    # 客户端普遍只发 max_tokens（有些发 max_completion_tokens），统一一下
    if payload.get("max_completion_tokens") and not args.get("max_tokens"):
        args["max_tokens"] = payload["max_completion_tokens"]
    if on_delta is not None:
        args["stream"] = True
        args["_on_delta"] = on_delta
    r = _dev().invoke("llmgw.chat", args).as_dict()
    if not r.get("ok"):
        return None, str(r.get("err") or "unknown")
    return r.get("data") or {}, None


def _as_completion(data, model_name):
    """把 llmgw 的返回包成 OpenAI ChatCompletion 形状。上游 body 原样透传（含 tool_calls）。"""
    body = dict(data.get("result") or {})
    body.setdefault("object", "chat.completion")
    body["id"] = body.get("id") or "chatcmpl-tianwang-%d" % int(time.time() * 1000)
    body["created"] = body.get("created") or int(time.time())
    body["model"] = model_name or body.get("model")
    body.setdefault("choices", [{"index": 0, "message": {"role": "assistant", "content": ""},
                                 "finish_reason": "stop"}])
    body["_tianwang"] = {
        "endpoint": data.get("endpoint"),
        "model": data.get("model"),
        "latency_s": data.get("latency_s"),
        "fallback_attempts": len(data.get("fallback_attempts") or []),
        # 带 tools 时若原分类链上没人支持 function calling，会降级到别家；
        # 这个标记让调用方知道「答的不是你以为的那个模型」——降级可以，冒充不行。
        "degraded_tools": bool(data.get("degraded_tools")),
    }
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "tianwang-llmgw"
    protocol_version = "HTTP/1.1"   # chunked 传输需要 1.1；每个响应都自带 Content-Length 或 chunked

    def log_message(self, fmt, *a):
        sys.stderr.write("[llmgw-server] " + (fmt % a) + "\n")

    def _send(self, code, obj, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    # ---------- 真流式（chunked） ----------
    def _chunk(self, raw):
        """写一个 HTTP chunk。客户端断开时静默收场——它不听了不是我们的错。"""
        try:
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()
            return True
        except Exception:
            return False

    def _stream_chat(self, payload):
        """上游吐一块就转一块，首字节一到客户端就能看见字。

        关键设计：200 响应头在**第一个 delta 到达时**才发。这样首字节前的候选失败
        （换下一跳、全部挂掉）时我们手里还有干净的连接，可以体面地回 502，
        而不是先发 200 再吐一个错误帧（那会让客户端以为请求成功了）。
        """
        started = {"ok": False}
        dead = {"ok": False}
        mid = "chatcmpl-tianwang-%d" % int(time.time() * 1000)
        model_name = payload.get("model")

        def frame(delta, finish=None, extra=None):
            obj = {"id": mid, "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model_name,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if extra:
                obj.update(extra)
            return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

        def on_delta(piece):
            if dead["ok"]:
                return
            if not started["ok"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                started["ok"] = True
                if not self._chunk(frame({"role": "assistant", "content": ""})):
                    dead["ok"] = True
                    return
            if not self._chunk(frame({"content": piece})):
                dead["ok"] = True

        err = None
        try:
            data, err = _chat(payload, on_delta=on_delta)
        except Exception as e:
            err = repr(e)[:200]
            data = None
        if not started["ok"]:
            # 一个字节都没发出去：干净地报错，客户端能正常重试
            return self._send(502, {"error": {"message": err or "上游无输出",
                                              "type": "upstream_error"}})
        if dead["ok"]:
            return
        meta = {
            "endpoint": (data or {}).get("endpoint"),
            "model": (data or {}).get("model"),
            "latency_s": (data or {}).get("latency_s"),
            "ttft_s": (data or {}).get("ttft_s"),
            "fallback_attempts": len((data or {}).get("fallback_attempts") or []),
            "degraded_tools": bool((data or {}).get("degraded_tools")),
        }
        if err:
            meta["stream_err"] = err
        elif (data or {}).get("stream_err"):
            meta["stream_err"] = data["stream_err"]
        self._chunk(frame({}, "stop", {"_tianwang": meta}))
        self._chunk(b"data: [DONE]\n\n")
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") in ("/v1/models", "/models"):
            q = parse_qs(u.query)
            all_models = str((q.get("all") or ["0"])[0]).lower() in ("1", "true", "yes")
            if all_models and not _ENDPOINT_HINTS:
                _refresh_hints()
            return self._send(200, {"object": "list", "data": _model_rows(all_models)})
        if u.path.rstrip("/") in ("/health", "/"):
            try:
                q = _dev().invoke("llmgw.quota", {}).as_dict()
                qd = q.get("data") or {}
            except Exception as e:
                qd = {"err": repr(e)[:200]}
            return self._send(200, {
                "ok": True, "service": "tianwang-llmgw-server",
                "classes": _classes(),
                "endpoints": sorted(_ENDPOINT_HINTS.keys()) or "未探测（访问 /v1/models?all=1）",
                "quota": qd,
            })
        return self._send(404, {"error": {"message": "not found: " + u.path}})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": {"message": "not found: " + u.path}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": {"message": "bad json: %s" % e}})
        want_stream = payload.get("stream") in (True, "true", "1", 1)
        if want_stream:
            return self._stream_chat(payload)
        data, err = _chat(payload)
        if err:
            return self._send(502, {"error": {"message": err, "type": "upstream_error"}})
        body = _as_completion(data, payload.get("model"))
        return self._send(200, body)


def serve(port=_DEFAULT_PORT, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), Handler)
    print("天网 llmgw 网关已起：http://%s:%d/v1" % (host, port))
    print("  分类：%s" % " / ".join(_classes()))
    print("  用法：OpenAI 兼容客户端里 url 填 http://%s:%d/v1/chat/completions，"
          "model 填上面的分类名" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，收工。")
    finally:
        srv.server_close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="天网 llmgw OpenAI 兼容端点")
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    serve(a.port, a.host)


if __name__ == "__main__":
    main()
