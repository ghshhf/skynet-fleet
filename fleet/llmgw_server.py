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
import os
import sys
import threading
from . import endpoints
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

if sys.stdout is None or sys.stderr is None:
    # 计划任务用 pythonw 拉起时没有控制台，流可能为 None —— 那时任何 print 都会抛
    # AttributeError，而网关是在后台线程里打日志的，一炸就是整条服务静默死掉。
    # 挂在 Windows 计划任务上跑，这种死法没人看得见，所以必须先把流补上。
    try:
        _dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tmp")
        os.makedirs(_dir, exist_ok=True)
        _fd = open(os.path.join(_dir, "gw8790.log"), "a",
                   encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = _fd
        if sys.stderr is None:
            sys.stderr = _fd
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .catalog import build
from .kinds import llmgw_device as L

_DEFAULT_PORT = endpoints.port_num("LLMGW", 8790)
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


def _fails_brief(data, n=4):
    """把失败跳压成一行：`ofm/xxx 401｜amd/yyy timeout`。

    只记个数没有诊断价值 —— 「retries=7」告诉你有 7 跳挂了，但看不出是哪家、
    为什么挂，于是每次只能靠猜（11-01 就是这样把 ofm 的 401 拖了好几轮的）。
    """
    fa = (data or {}).get("fallback_attempts") or []
    if not fa:
        return "-"
    bits = []
    for a in fa[-n:]:
        bits.append("%s/%s %s" % (
            str(a.get("gateway") or "?")[:10],
            str(a.get("model") or "?")[:14],
            str(a.get("err") or "?")[:44].replace(" ", "_")))
    return " | ".join(bits)


class Handler(BaseHTTPRequestHandler):
    server_version = "tianwang-llmgw"
    protocol_version = "HTTP/1.1"   # chunked 传输需要 1.1；每个响应都自带 Content-Length 或 chunked

    def log_message(self, fmt, *a):
        sys.stderr.write("[%s] [llmgw] " % time.strftime("%H:%M:%S")
                         + (fmt % a) + "\n")

    def note(self, kind, **kw):
        """结构化一行日志。

        没有时间戳的日志等于没有日志：客户端说「等了 50 秒」，我们事后只能靠猜
        到底是哪一跳慢、几个候选挂了、是不是并发闸在排队。每次请求都记一行，
        把「谁答的 / 失败几次 / 各跳错误 / 总耗时」全留下，事后 grep 就有答案。
        """
        bits = " ".join("%s=%s" % (k, kw[k]) for k in sorted(kw)
                        if kw[k] not in (None, ""))
        sys.stderr.write("[%s] [llmgw] %s %s\n"
                         % (time.strftime("%H:%M:%S"), kind, bits))
        sys.stderr.flush()

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

    def _stream_chat(self, payload, t0=None):
        """上游吐一块就转一块，首字节一到客户端就能看见字。

        关键设计：200 响应头在**第一个 delta 到达时**才发。这样首字节前的候选失败
        （换下一跳、全部挂掉）时我们手里还有干净的连接，可以体面地回 502，
        而不是先发 200 再吐一个错误帧（那会让客户端以为请求成功了）。
        """
        t0 = t0 or time.time()
        started = {"ok": False}
        dead = {"ok": False}
        first = {"at": None}
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
            # 走到这里两种可能，必须分开处理：
            #   ① 真失败（err 非空 / 没数据）→ 干净 502，客户端能重试；
            #   ② **成功但一个文本增量都没有** —— 典型是「这一轮只回工具调用」：
            #      智能体的 tool_calls 轮次本来就没有正文，on_delta 一次都不会触发。
            #      11-01 Pi 接入实测踩到：把它当失败会 502，于是「代理能用工具」直接失效。
            #      正解是整段补发一帧（含 tool_calls），流式语义照样成立。
            if err or not data:
                dt = round(time.time() - t0, 2)
                self.note("stream-FAIL", dt=dt, model=model_name,
                          n_msg=len(payload.get("messages") or []),
                          has_tools=bool(payload.get("tools")),
                          err=str(err or "上游无输出")[:200])
                return self._send(502, {"error": {"message": err or "上游无输出",
                                                  "type": "upstream_error"}})
            body = _as_completion(data, model_name)
            msg = (body.get("choices") or [{}])[0].get("message") or {}
            delta = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                delta["tool_calls"] = msg["tool_calls"]
            self.note("stream-ok", dt=round(time.time() - t0, 2), model=model_name,
                      ttft="-", chunks=0, tool_calls=bool(msg.get("tool_calls")),
                      via=(body.get("_tianwang") or {}).get("endpoint"),
                      retries=(body.get("_tianwang") or {}).get("fallback_attempts"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self._chunk(frame(delta, "tool_calls" if msg.get("tool_calls") else "stop",
                              {"_tianwang": body.get("_tianwang")}))
            self._chunk(b"data: [DONE]\n\n")
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            return
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
        self.note("stream-ok", dt=round(time.time() - t0, 2), model=model_name,
                  ttft=meta.get("ttft_s"), via=meta.get("endpoint"),
                  retries=meta.get("fallback_attempts"),
                  mid_stream_err=bool(meta.get("stream_err")),
                  degraded=meta.get("degraded_tools"),
                  fails=_fails_brief(data))
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
        t0 = time.time()
        if want_stream:
            return self._stream_chat(payload, t0)
        data, err = _chat(payload)
        dt = round(time.time() - t0, 2)
        # 注意：路由明细在 data **顶层**（endpoint/model/latency_s/fallback_attempts），
        # 而 "_tianwang" 是 _as_completion 之后才造出来的包装。这里取顶层，
        # 否则日志里 via 永远是空的 —— 等于白装黑匣子。
        if err:
            self.note("chat-FAIL", dt=dt, model=payload.get("model"),
                      n_msg=len(payload.get("messages") or []),
                      has_tools=bool(payload.get("tools")), err=str(err)[:160])
            return self._send(502, {"error": {"message": err, "type": "upstream_error"}})
        self.note("chat-ok", dt=dt, model=payload.get("model"),
                  n_msg=len(payload.get("messages") or []),
                  tools=bool(payload.get("tools")),
                  via=(data or {}).get("endpoint"), real=(data or {}).get("model"),
                  lat=(data or {}).get("latency_s"),
                  retries=len((data or {}).get("fallback_attempts") or []),
                  degraded=bool((data or {}).get("degraded_tools")),
                  fails=_fails_brief(data))
        body = _as_completion(data, payload.get("model"))
        return self._send(200, body)


def _warmup_loop(delay=3.0, gap=1.0):
    """启动后异步体检：每条分类链真打一次，把死端点提前记住。

    为什么值得做：健康表是「撞过才记得」的，而第一次撞要用户陪着等
    （实测 cn 连撞 7 跳 17.8s，客户端重试一次就是 50s+）。启动时自己先撞一遍，
    用户就永远撞不到。用后台线程、串行跑，不挡启动、不跟用户的请求抢并发。
    """
    time.sleep(delay)
    try:
        classes = _classes()
    except Exception:
        return
    for cls in classes:
        try:
            t0 = time.time()
            data, err = _chat({"class": cls, "max_tokens": 4,
                               "messages": [{"role": "user", "content": "1"}]})
            tw = data or {}
            print("[%s] [llmgw] warmup %-7s %s %.2fs via=%s %s"
                  % (time.strftime("%H:%M:%S"), cls,
                     "ok" if data else "FAIL", time.time() - t0,
                     tw.get("endpoint"), "" if data else str(err)[:60]),
                  file=sys.stderr, flush=True)
        except Exception as e:
            print("[%s] [llmgw] warmup %s 异常 %r"
                  % (time.strftime("%H:%M:%S"), cls, e), file=sys.stderr, flush=True)
        time.sleep(gap)


class _ExclusiveServer(ThreadingHTTPServer):
    """同端口只许一个实例。

    ThreadingHTTPServer 默认 allow_reuse_address=1 → Windows 上会设 SO_REUSEADDR，
    而 Windows 的 SO_REUSEADDR 语义和 POSIX 完全不同：它允许**第二个进程绑同一端口**
    「成功」（端口劫持），两个进程各收一部分连接，行为完全不可预测。
    11-01 把网关挂上计划任务后立刻撞上：自愈和手动拉起撞车，netstat 里出现两个 pid
    同时 LISTENING 在 8790，客户端随机转到其中一个，报的错互相打架。
    SO_EXCLUSIVEADDRUSE 才是 Windows 的独占语义 —— 第二个实例绑定时直接失败退出，
    这是我们要的结果：宁可这个实例起不来，也不能两个半死不活地分流量。
    """
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt":
            import socket as _s
            try:
                self.socket.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        ThreadingHTTPServer.server_bind(self)


def serve(port=_DEFAULT_PORT, host="127.0.0.1"):
    try:
        srv = _ExclusiveServer((host, port), Handler)
    except OSError as e:
        # 绑不上 = 已经有一个在跑。这是正常情况（自愈和手动会撞），不是故障。
        print("端口 %d 已被占用（可能已有一个网关在跑）：%s" % (port, e),
              file=sys.stderr, flush=True)
        return 2
    print("天网 llmgw 网关已起：http://%s:%d/v1" % (host, port))
    print("  分类：%s" % " / ".join(_classes()))
    print("  用法：OpenAI 兼容客户端里 url 填 http://%s:%d/v1/chat/completions，"
          "model 填上面的分类名" % (host, port))
    threading.Thread(target=_warmup_loop, name="warmup", daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C，收工。")
    finally:
        srv.server_close()


def _selftest():
    """离线自测：同端口只许一个实例。

    这条必须钉住，因为网关已经挂到 Windows 计划任务上（tools/gw_up.py --install）：
    自愈和手动拉起撞车是常态。两个进程同时 LISTENING 在 8790 时，Windows 会把
    连接随机分给其中一个，报的错互相打架 —— 从日志里根本看不出有两个实例。
    11-01 装计划任务当天就撞了一次（netstat 里两个 pid 并排）。
    """
    bad = 0
    s1 = _ExclusiveServer(("127.0.0.1", 0), Handler)
    port = s1.server_address[1]
    try:
        s2 = _ExclusiveServer(("127.0.0.1", port), Handler)
        s2.server_close()
        print("❌ 第二个实例居然绑上了同端口（独占失效）")
        bad += 1
    except OSError:
        print("✅ 第二个实例被拒（独占端口生效）")
    # 端口被占时 serve() 必须立刻返回 2，而不是进去 serve_forever 干等
    rc = serve(port)
    if rc == 2:
        print("✅ 端口被占时 serve() 立刻返回 2")
    else:
        print("❌ 端口被占时 serve() 返回了 %r" % (rc,))
        bad += 1
    s1.server_close()
    print("llmgw_server 自测：%s" % ("全过" if not bad else "%d 项失败" % bad))
    return 0 if not bad else 1


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="天网 llmgw OpenAI 兼容端点")
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--selftest", action="store_true", help="跑离线自测")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    return serve(a.port, a.host)


if __name__ == "__main__":
    sys.exit(main() or 0)
