#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ad_classify.py —— 用免费 LLM 批量给采集到的搜索结果定性。

━━ 分工（和全项目同一条纪律）━━
· **模型只做判断**：每条结果是什么性质（正常 / SEO垃圾 / 菠菜 / 成人 /
  黑五类 / 诈骗 / 其他违法）。判断依据只有标题 + URL + 域名这些字面证据，
  不打开任何链接。
· **统计全部在本地**：域名形状、TLD 分布、标题长度/问号数、同模板站群——
  这些确定性聚合由 Python 算，不让模型「目测」。

用法：
    python tools/ad_classify.py tmp/ad_harvest_2026-10-24.jsonl
    python tools/ad_classify.py --batch 12   # 调整每批条数
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SYSTEM = (
    "你是网络广告安全分析器。只根据给定的搜索结果元数据（标题/URL/域名）"
    "判断每条结果的性质。不要打开链接，不要推测页面内容，只依据字面证据。"
    "输出严格 JSON 数组，不要任何解释文字。"
)

PROMPT_TMPL = """分析下面 {n} 条搜索结果。对每条输出一个 JSON 对象：
{{"i": 序号, "kind": 类型, "sig": [证据词], "why": "不超过20字的理由"}}

kind 取值（只能选一个）：
- normal        正常网页（企业官网/文档/新闻/百科/论坛正常帖）
- seo_spam      SEO垃圾：堆关键词抢排名，但不涉及违法品类
- gambling      博彩/菠菜（娱乐场/首存/返利/开户等）
- adult         色情/成人内容导流
- medical       黑五类医疗（减肥/壮阳/脱发/偏方/假药）
- fraud         诈骗导流（假客服/假办理/假查询/资金盘/贷款套路）
- other_illegal 其他违法（假证/代开/盗版/刷量等）
- unknown       字面证据不足，拿不准

规则：
1. sig 必须是从标题或URL里原样摘出的词，没有就空数组
2. 只有字面出现导流词或堆砌形状才判违法类型，不要脑补
3. 拿不准一律 unknown，不要硬猜
4. 只输出 JSON 数组本身

{items}
"""


def _fmt_items(hits):
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f'{i}. 标题: {h["title"][:80]}')
        lines.append(f'   URL: {h["url"][:110]}')
        lines.append(f'   域名: {h["host"]}')
    return "\n".join(lines)


def _parse_json_array(text):
    """从模型输出里抠出 JSON 数组。容忍前后废话和 markdown 代码块。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        return None


def classify_batch(hits):
    """一批送进 LLM。返回 {序号: (kind, sig, why)}。失败返回 {}。"""
    from fleet import re_llm
    prompt = PROMPT_TMPL.format(n=len(hits), items=_fmt_items(hits))
    text, used = re_llm.ask(prompt, system=SYSTEM, n_try=2)
    arr = _parse_json_array(text)
    if not isinstance(arr, list):
        return {}, used, "返回不是 JSON 数组"
    out = {}
    for r in arr:
        if not isinstance(r, dict):
            continue
        try:
            out[int(r.get("i"))] = (
                str(r.get("kind") or "unknown"),
                list(r.get("sig") or []),
                str(r.get("why") or "")[:40],
            )
        except (TypeError, ValueError):
            continue
    return out, used, ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    hits = []
    with open(args.jsonl, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                hits.append(json.loads(ln))
    print(f"读入 {len(hits)} 条")

    out_path = args.out or args.jsonl.replace(".jsonl", "_classified.jsonl")
    t0 = time.time()
    n_done = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for s in range(0, len(hits), args.batch):
            batch = hits[s:s + args.batch]
            try:
                res, used, err = classify_batch(batch)
            except Exception as e:               # noqa: BLE001
                res, used, err = {}, "-", f"{type(e).__name__}: {e}"
            if not res:
                print(f"  ! 批 {s//args.batch + 1} 失败：{err}（{used}）")
            for i, h in enumerate(batch, 1):
                kind, sig, why = res.get(i, ("unknown", [], "分类失败"))
                h["kind"] = kind
                h["sig"] = sig
                h["llm_why"] = why
                f.write(json.dumps(h, ensure_ascii=False) + "\n")
                n_done += 1
            print(f"  批 {s//args.batch + 1}/{(len(hits) + args.batch - 1)//args.batch}"
                  f"（{used}）：{len(res)}/{len(batch)} 条")
    print(f"分类 {n_done} 条 / {round(time.time() - t0, 1)}s → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
