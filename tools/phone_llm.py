"""手机上的 OpenAI 兼容端点：把「手机的算力」变成一个能直接调用的模型。

手机侧跑 llama-server（常驻，模型只加载一次 ≈1 秒），PC 通过局域网 HTTP 调它。
人格不在这条链路的任何一层里 —— 它在记忆包里，渲染成 system 注进去。
所以：换包就换人，llama-server 进程都不用重启，权重一个字节都没动。

为什么不用 llama-cli 而是 server：
  · 冷启动 7 秒 → 常驻后每次请求毫秒级排队，这才叫可用；
  · cli 的输出混着 banner、ASCII art、prompt 回显，解析逻辑永远在打补丁；
    server 回的是标准 JSON，机器可读，和 OpenAI 客户端直接兼容。

用法：
    python tools/phone_llm.py health
    python tools/phone_llm.py ask --pack personas/dafeiyu --ask "在吗"
    python tools/phone_llm.py bench
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import memory_pack as M      # noqa: E402

BASE = os.environ.get("PHONE_LLM_BASE", "http://192.168.137.40:8080")
TIMEOUT = float(os.environ.get("PHONE_LLM_TIMEOUT", "180"))

# 直连内网，绝不能走系统代理：
# 手机上跑的是局域网内的 192.168.137.40，一旦被系统代理接手，
# 表现是「502 Bad Gateway」或「远程主机强迫关闭连接」——
# 看起来像手机挂了，其实请求根本没出本机。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout=TIMEOUT):
    return _OPENER.open(req, timeout=timeout)


def _post(path, payload, timeout=TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _open(req, timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "replace")), time.time() - t0


def health():
    try:
        with _open(BASE.rstrip("/") + "/health", 8) as r:
            return {"ok": r.status == 200, "status": r.status, "base": BASE}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}", "base": BASE}


def models():
    with _open(BASE.rstrip("/") + "/v1/models", 15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def chat(messages, n=192, temp=0.7, stream=False):
    """一次对话。返回 {text, elapsed_s, tokens, tps, ttft_s}。"""
    payload = {"messages": messages, "max_tokens": n, "temperature": temp,
               "stream": bool(stream)}
    if not stream:
        data, dt = _post("/v1/chat/completions", payload)
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        ct = usage.get("completion_tokens") or 0
        return {"text": text.strip(), "elapsed_s": round(dt, 2),
                "tokens": ct, "tps": round(ct / dt, 1) if dt and ct else 0,
                "ttft_s": None, "model": data.get("model", "")}

    # 流式：TTFT（首字延迟）才是「体感快不快」的真指标
    payload["stream"] = True
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft, parts = None, []
    with _open(req) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            piece = delta.get("content")
            if piece:
                if ttft is None:
                    ttft = time.time() - t0
                parts.append(piece)
    dt = time.time() - t0
    text = "".join(parts).strip()
    return {"text": text, "elapsed_s": round(dt, 2),
            "tokens": None, "tps": None,
            "ttft_s": round(ttft, 3) if ttft else None, "model": ""}


def ask_with_pack(pack_dir, question, n=160, temp=0.7, budget=800,
                  query="", stream=True):
    pack = M.Pack.load(pack_dir)
    r = pack.render(budget_tokens=budget, query=query or question)
    msgs = [{"role": "system", "content": r["text"]},
            {"role": "user", "content": question}]
    out = chat(msgs, n=n, temp=temp, stream=stream)
    out.update({"pack": r["pack"], "pack_tokens": r["tokens"],
                "kept": r["kept"], "dropped": r["dropped"],
                "by_kind": r["by_kind"], "question": question})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="手机 LLM 端点客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("models")
    p = sub.add_parser("ask")
    p.add_argument("--pack", default="")
    p.add_argument("--ask", required=True)
    p.add_argument("--n", type=int, default=160)
    p.add_argument("--budget", type=int, default=800)
    p.add_argument("--temp", type=float, default=0.7)
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--base", default="", help="覆盖端点（比如走 adb forward）")
    b = sub.add_parser("bench")
    b.add_argument("--rounds", type=int, default=3)
    b.add_argument("--base", default="")
    args = ap.parse_args(argv)

    global BASE
    if getattr(args, "base", ""):
        BASE = args.base

    if args.cmd == "health":
        print(json.dumps(health(), ensure_ascii=False))
        return 0
    if args.cmd == "models":
        print(json.dumps(models(), ensure_ascii=False, indent=1))
        return 0

    if args.cmd == "bench":
        h = health()
        print(f"[i] endpoint={BASE} ok={h.get('ok')}")
        if not h.get("ok"):
            print(json.dumps(h, ensure_ascii=False))
            return 2
        for i in range(args.rounds):
            r = chat([{"role": "user", "content": "用一句话说明你在哪台设备上运行。"}],
                     n=64, stream=True)
            print(f"  round{i+1}: ttft={r['ttft_s']}s total={r['elapsed_s']}s "
                  f"chars={len(r['text'])} | {r['text'][:40]}")
        return 0

    if args.pack:
        r = ask_with_pack(args.pack, args.ask, n=args.n, temp=args.temp,
                          budget=args.budget, stream=not args.no_stream)
    else:
        r = chat([{"role": "user", "content": args.ask}], n=args.n,
                 temp=args.temp, stream=not args.no_stream)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        head = (f"[i] pack={r.get('pack','-')} tokens={r.get('pack_tokens','-')} "
                f"ttft={r.get('ttft_s')}s total={r.get('elapsed_s')}s "
                f"tps={r.get('tps')}")
        print(head)
        print(f"Q: {args.ask}")
        print(f"A: {r['text'] or '(空)'}")
    return 0 if r.get("text") else 3


if __name__ == "__main__":
    sys.exit(main())
