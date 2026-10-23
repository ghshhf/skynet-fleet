"""手机整体测速：不是"跑一次看看"，是把每一层分开计时。

分开计时的理由：一个「2 秒」的数字说明不了任何问题。得知道时间花在
渲染记忆包、排队、prefill、还是逐 token 生成上——否则优化就是瞎猜。

测这几层：
  1. 端点健康（TCP + HTTP 握手）
  2. 换包后的首答（system 前缀变了 ⇒ 全量 prefill，这是人格切换的真实成本）
  3. 同一包连问 5 轮（第 2 轮起 KV 命中，这才是"常驻"该有的速度）
  4. 长输出吞吐（连续生成 200 token，看 tok/s 是否随长度衰减）
  5. 手机侧自己渲染记忆包的耗时（不经过 PC）

用法：
    python tools/phone_bench.py                 # 走 adb forward
    python tools/phone_bench.py --base http://192.168.137.40:8080
    python tools/phone_bench.py --pack personas/dafeiyu --rounds 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import phone_llm as P                       # noqa: E402
from fleet import memory_pack as M          # noqa: E402


def _post_raw(path, payload, timeout=600):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(P.BASE.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with P._open(req, timeout) as r:        # 绕代理，内网直连
        raw = r.read()
    return json.loads(raw.decode("utf-8", "replace")), time.time() - t0


def turn(messages, n=64, temp=0.6):
    """一次完整对话，带 llama.cpp 自己报的 timings（比自己掐表准）。"""
    data, dt = _post_raw("/v1/chat/completions",
                         {"messages": messages, "max_tokens": n,
                          "temperature": temp, "stream": False})
    ch = (data.get("choices") or [{}])[0]
    text = ((ch.get("message") or {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    tm = data.get("timings") or {}
    return {"text": text, "wall_s": round(dt, 3),
            "prompt_tokens": usage.get("prompt_tokens") or tm.get("prompt_n") or 0,
            "completion_tokens": usage.get("completion_tokens")
            or tm.get("predicted_n") or 0,
            "prefill_tps": round(tm.get("prompt_per_second") or 0, 1),
            "pred_tps": round(tm.get("predicted_per_second") or 0, 1),
            "cached": tm.get("cache_n")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="手机整体测速")
    ap.add_argument("--base", default=os.environ.get("PHONE_LLM_BASE",
                                                     "http://127.0.0.1:18080"))
    ap.add_argument("--pack", default="personas/dafeiyu")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    P.BASE = args.base

    out = {"base": args.base, "pack": args.pack, "steps": []}

    # ---- 1. 端点 ----
    t0 = time.time()
    h = P.health()
    if not h.get("ok"):
        print("[X] 端点不通：%s" % h)
        return 2
    out["steps"].append({"name": "health", "ms": round((time.time() - t0) * 1000, 1)})
    print("[1] 端点 %s  握手 %.0f ms" % (args.base, (time.time() - t0) * 1000))

    # ---- 2. 换包首答（全量 prefill） ----
    t0 = time.time()
    pack = M.Pack.load(args.pack)
    r = pack.render(budget_tokens=args.budget, query="你在哪台机器上跑")
    render_ms = round((time.time() - t0) * 1000, 1)
    system = r["text"]
    q1 = "用一句话说你跑在哪台机器上。"
    a = turn([{"role": "system", "content": system},
              {"role": "user", "content": q1}], n=args.n)
    print("[2] 换包首答：prompt %d tok，prefill %.0f tok/s，墙钟 %.2fs（含全量 prefill）"
          % (a["prompt_tokens"], a["prefill_tps"], a["wall_s"]))
    print("    渲染记忆包 %.1f ms（%d 片，%d tok）"
          % (render_ms, r["kept"], r["tokens"]))
    out["steps"].append({"name": "first_after_pack_change", **a,
                         "render_ms": render_ms, "pack_tokens": r["tokens"]})

    # ---- 3. 同一包连问（第 2 轮起 KV 命中） ----
    print("[3] 同一包连问 %d 轮（第 2 轮起应命中 KV 缓存）" % args.rounds)
    print("     轮  prompt  cached  prefill(tok/s)  墙钟      生成(tok/s)")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": q1},
            {"role": "assistant", "content": a["text"]}]
    history = [dict(a)]
    follow = ["再说一句：你现在用的什么运行时？",
              "那我要是想看图片，需要额外加载什么？",
              "一句话：内存够不够跑更大的模型？",
              "总结一下你刚说的两点。",
              "最后一个问题：你会不会自己下载新模型？"]
    for i in range(args.rounds):
        q = follow[i % len(follow)]
        msgs.append({"role": "user", "content": q})
        b = turn(msgs, n=args.n)
        history.append(dict(b))
        print("     %2d  %6d  %6s  %10.1f  %6.2fs  %10.1f"
              % (i + 1, b["prompt_tokens"], str(b["cached"]), b["prefill_tps"],
                 b["wall_s"], b["pred_tps"]))
        msgs.append({"role": "assistant", "content": b["text"]})
    out["steps"].append({"name": "multi_turn", "turns": history})

    # ---- 4. 长输出吞吐 ----
    long_q = [{"role": "user", "content": "用中文写 150 字左右，说说手机跑本地模型的意义。"}]
    c = turn(long_q, n=220)
    print("[4] 长输出：%d tok / %.2fs = %.1f tok/s（停在前 %d tok，够看衰减）"
          % (c["completion_tokens"], c["wall_s"], c["pred_tps"],
             c["completion_tokens"]))
    out["steps"].append({"name": "long_gen", **c})

    # ---- 5. 手机侧自己渲染记忆包 ----
    out["steps"].append({"name": "render_pc_ms", "ms": render_ms})
    print("[5] 记忆包渲染（PC 侧同代码，手机上同脚本）：%.1f ms / %d tok"
          % (render_ms, r["tokens"]))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
