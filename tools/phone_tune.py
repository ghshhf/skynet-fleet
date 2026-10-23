"""手机端 llama-server 调参台：把「参数 → 实测」的对应关系钉死。

为什么不能只看 llama-bench：
  bench 测的是裸的 pp512 / tg128，而用户真正感受到的是「问一句话，多久出第一个字、
  后面每秒几个字」。这中间还隔着 server 的 prompt 模板、KV cache、排队、HTTP。
  所以这里不猜，直接把某组参数真的起起来，打真请求，读 server 自己报的 timings。

为什么一定要 cache_prompt=false：
  llama-server 默认会复用上一次的 KV 前缀。调参时如果复用，第二次测同一段 prompt
  的 prefill 时间会接近 0，测出来的是「缓存命中速度」，不是模型真实预填充速度。
  调参必须每次都让它在冷前缀上重算——虽然更慢，但那才是可比的数。

用法：
    python tools/phone_tune.py                       # 扫默认几组
    python tools/phone_tune.py --configs 4/6/on,6/6/off
    python tools/phone_tune.py --base http://192.168.137.40:8080 --keep

每组配置形如 线程数/批线程数/flash-attn，例：4/6/on
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

import adbx                                  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 两段 prompt：短的模拟「随口一问」，长的模拟「贴一段代码让模型看」。
# 只有长 prompt 才能把 prefill 的差距显出来——短 prompt 里 prefill 只占几十毫秒，
# 参数差异全被噪声吃掉。
SHORT = "用一句话说明 CPU 和 GPU 在逐 token 生成时谁更合适。"
LONG = ("下面是一段需要审阅的日志，请在最后用一句话总结它暴露了什么风险。\n" +
        "\n".join(f"2026-10-23 20:{i:02d}:00 worker[{i%8}] dequeued job {1000+i} "
                  f"lease=30s state=running retries={i%3} queue_depth={120-i}"
                  for i in range(60)))


def _post(base, path, payload, timeout=300):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _OPENER.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data, time.time() - t0


def _restart(threads, tb, fa, ctx=4096, kvq=0, timeout=180):
    """用指定参数重启手机上的 llama-server，等它健康。

    路径是 $HOME/skynet/lib/serve.sh —— provision_phone 把 scripts/phone/* 平铺下发到
    lib/ 下（不保留 scripts/phone 这层目录），这里跟着它的规矩走。
    """
    env = (f'THREADS={threads} TB={tb} FA={fa} CTX={ctx} KVQ={kvq} ')
    script = (f'{env}sh $HOME/skynet/lib/serve.sh '
              f'models/北极星-V7-Q8_0.gguf 8080')
    rc, out, err = adbx.termux(script, timeout=timeout)
    return rc == 0 and "ready in" in out, (out or err or "").strip()[-200:]


def _measure(base, prompt, n_predict=96):
    data, wall = _post(base, "/v1/completions", {
        "prompt": prompt, "n_predict": n_predict, "temperature": 0,
        "cache_prompt": False, "stream": False,
    })
    t = data.get("timings") or {}
    return {
        "prompt_n": t.get("prompt_n"),
        "predicted_n": t.get("predicted_n"),
        "pp": t.get("prompt_per_second"),        # 预填充 tok/s
        "tg": t.get("predicted_per_second"),     # 逐 token tok/s
        "ttft": (t.get("prompt_ms") or 0) / 1000.0,   # 第一个字之前要等多久
        "wall": round(wall, 2),
    }


def run_one(base, cfg):
    threads, tb, fa = cfg
    ok, why = _restart(threads, tb, fa)
    if not ok:
        return {"cfg": cfg, "ok": False, "err": why}
    row = {"cfg": cfg, "ok": True}
    try:
        row["short"] = _measure(base, SHORT)
        row["long"] = _measure(base, LONG)
    except Exception as e:
        row["ok"] = False
        row["err"] = f"{type(e).__name__}: {e}"
    return row


def fmt(rows):
    head = (f"{'配置(t/tb/fa)':>16} | {'PP短':>7} {'TG短':>7} {'首字s':>6} | "
            f"{'PP长':>7} {'TG长':>7} {'首字s':>6}")
    lines = [head, "-" * len(head)]
    for r in rows:
        if not r["ok"]:
            lines.append(f"{'/'.join(map(str, r['cfg'])):>16} | 失败：{r.get('err','')[:60]}")
            continue
        s, l = r["short"], r["long"]
        lines.append(
            f"{'/'.join(map(str, r['cfg'])):>16} | "
            f"{s['pp']:7.1f} {s['tg']:7.1f} {s['ttft']:6.2f} | "
            f"{l['pp']:7.1f} {l['tg']:7.1f} {l['ttft']:6.2f}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("PHONE_LLM_BASE",
                                                     "http://192.168.137.40:8080"))
    ap.add_argument("--configs", default="4/6/on,6/6/on,4/4/on,4/6/off,8/8/on",
                    help="逗号分隔，每项 线程/批线程/flash-attn")
    ap.add_argument("--keep", action="store_true",
                    help="测完不停机（默认用最优那组重启并留着跑）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    cfgs = []
    for part in a.configs.split(","):
        t, tb, fa = part.strip().split("/")
        cfgs.append((int(t), int(tb), fa))

    rows = []
    for c in cfgs:
        print(f"[..] 试 {c[0]}/{c[1]}/{c[2]}", flush=True)
        r = run_one(a.base, c)
        rows.append(r)
        if r["ok"]:
            s, l = r["short"], r["long"]
            print(f"     短: PP {s['pp']:.1f} TG {s['tg']:.1f} 首字 {s['ttft']:.2f}s"
                  f" | 长: PP {l['pp']:.1f} TG {l['tg']:.1f} 首字 {l['ttft']:.2f}s",
                  flush=True)
        else:
            print(f"     失败：{r.get('err','')[:120]}", flush=True)

    print()
    print(fmt(rows))

    ok = [r for r in rows if r["ok"]]
    best = None
    if ok:
        # 打分口径：聊天体感 = 长 prompt 的首字延迟（占大头）+ 生成速度。
        # 首字延迟越短越好，生成越快越好 → 用 tg / 首字 做粗排，避免拍一个权重。
        def score(r):
            l = r["long"]
            return (l["tg"] or 0) / max(0.05, l["ttft"] or 0.05)
        best = max(ok, key=score)
        print(f"\n综合最优（长 prompt 首字延迟 + 生成速度）："
              f"{best['cfg'][0]}/{best['cfg'][1]}/{best['cfg'][2]}")

    if not a.keep and best:
        print("[..] 用最优配置重启并保持常驻")
        ok2, why = _restart(*best["cfg"])
        print("     已就绪" if ok2 else f"     重启失败：{why}")

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
