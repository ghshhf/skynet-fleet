"""天网 · 工程体检：起服务前先验一遍，别等聊天时才发现不对。

四项体检 + 一个速度基准，全部对着**真实运行中的 llama-server** 打，
不是对着配置猜的：

1. **模板对齐**：问一句，看回答里有没有 `<think>` 残留、markdown 围栏。
   （Qwen3 的 GGUF 里还留着官方 `enable_thinking` 条件模板，没 patch 过就会
   每句开头泄漏 `</think>`——这是 0.6B「不能正常交流」的病根之一。）
2. **终止符**：连问 3 轮，看第 2、3 轮是否还带前缀噪音、是否复读第一轮。
3. **复读**：同一句问两遍，看两次回答是否雷同。
4. **多轮连贯**：问「我叫X」，下一句问「我叫什么」，看它记不记得住。
5. **tok/s 基准**：跑一次生成，测实际速度（决定能不能上 4B 还是必须走小模型）。

用法：
    python triage.py                 # 全查
    python triage.py --url http://127.0.0.1:8080
    python triage.py --card whale_girl
"""
import argparse
import json
import re
import sys
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_CARD = "tianwang_default"

SYSTEM = ("[身份]\n你是「她」，本地陪伴助手。说话简洁，口语短句，不写多余解释。")


def chat(url, prompt, system=None, timeout=120, temperature=0.7, max_tokens=256):
    payload = {
        "model": "local",
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    dt = time.time() - t0
    content = ""
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except Exception:
        pass
    usage = data.get("usage") or {}
    ntok = usage.get("completion_tokens") or 0
    return {
        "text": content,
        "seconds": dt,
        "tokens": ntok,
        "tps": round(ntok / dt, 1) if dt > 0 and ntok else 0.0,
    }


def build_system(card_id):
    try:
        import tianwang_cards as persona_card
        s = persona_card.build_system(card_id)
        if s:
            return s
    except Exception:
        pass
    return SYSTEM


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print("  [%s] %-12s %s" % (mark, name, detail))
    return ok


def run(url, card_id):
    print("=" * 60)
    print("天网体检  url=%s  card=%s" % (url, card_id))
    print("=" * 60)

    system = build_system(card_id)

    # 0) 服务可达
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/models", timeout=5) as r:
            models = json.loads(r.read().decode("utf-8"))
        names = [m.get("id") for m in (models.get("data") or [])]
        print("  服务可达，模型：%s" % ", ".join(str(n) for n in names[:3]))
    except Exception as e:
        print("  [FAIL] 服务不可达：%s" % e)
        return False

    results = []

    # 1) 模板对齐
    r1 = chat(url, "你好，介绍一下自己。", system)
    t1 = r1["text"]
    has_think = "<think>" in t1 or "</think>" in t1
    has_fence = "```" in t1
    print("\n--- 模板对齐 ---")
    print("  回复：%s" % t1.replace("\n", " ")[:160])
    results.append(check("无 think 残留", not has_think,
                         "" if not has_think else "检测到 think 标签泄漏，需 --chat-template 修正"))
    results.append(check("无 markdown 围栏", not has_fence,
                         "" if not has_fence else "模型爱用 ``` 包裹自己"))

    # 2) 终止符 + 复读
    print("\n--- 终止符 / 复读 ---")
    r2 = chat(url, "你是谁？", system)
    r3 = chat(url, "你是谁？", system)
    t2, t3 = r2["text"], r3["text"]
    print("  二次：%s" % t2.replace("\n", " ")[:120])
    print("  三次：%s" % t3.replace("\n", " ")[:120])
    results.append(check("二次无残留", "<think>" not in t2 and "</think>" not in t2))
    dup = _similar(t2, t3)
    results.append(check("不复读", dup < 0.9, "相似度 %.2f" % dup))

    # 3) 多轮连贯
    print("\n--- 多轮连贯 ---")
    a = chat(url, "我叫阿泽，记住我的名字。", system)
    b = chat(url, "我叫什么名字？", system)
    print("  答：%s" % b["text"].replace("\n", " ")[:120])
    hit = ("阿泽" in b["text"]) or ("aze" in b["text"].lower())
    results.append(check("记住称呼", hit,
                         "" if hit else "答非所问说明上下文没接上"))

    # 4) 速度基准
    print("\n--- 速度基准 ---")
    # 🔴 踩过的坑：用「用一句话说明…」这种会早停的 prompt 测速，模型 4 个
    # token 就收工，算出 4.5 tok/s 的假数（llama-bench 硬数 5.23）。
    # 速度基准必须采够样本：强制长输出（数数），且 tokens < 32 时明说
    # 样本不足——数字不可信要比数字错更早说出来。
    rb = chat(url, "请从 1 数到 60，每行一个数字，只输出数字。",
              system, max_tokens=200)
    if rb["tokens"] < 32:
        print("  [WARN] 只生成 %d token，样本不足，下面的速度数字不可信"
              % rb["tokens"])
    print("  %d tokens / %.1fs = %.1f tok/s" % (rb["tokens"], rb["seconds"], rb["tps"]))
    tps = rb["tps"]
    if tps >= 20:
        verdict = "够快：4B 级模型可上桌"
    elif tps >= 10:
        verdict = "偏慢：建议小模型 + 三段式推理"
    else:
        verdict = "太慢：只能 0.6B 档 + 极短提示"
    print("  结论：%s" % verdict)

    print("\n" + "=" * 60)
    print("体检结果：%d/%d 项通过" % (sum(1 for x in results if x), len(results)))
    print("=" * 60)
    return all(results)


def _similar(a, b):
    """粗略文本相似度（字符集合 Jaccard），够用来抓复读。"""
    sa, sb = set(a.strip()), set(b.strip())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--card", default=DEFAULT_CARD)
    args = ap.parse_args()
    try:
        ok = run(args.url, args.card)
    except Exception as e:
        print("体检中断：%s" % e)
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
