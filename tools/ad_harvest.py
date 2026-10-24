#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ad_harvest.py —— 采集「违法广告劫持搜索结果」的原始证据。

━━ 这东西在采什么 ━━
搜索模块每一轮都会撞上一堆广告：菠菜、成人、黑五类（减肥/脱发/医疗）、
诈骗导流。webtool.py 的过滤器把它们挡在上下文外（这是对的），但**挡掉
的数据本身是研究材料**：违法广告靠什么形状的关键词劫持排名、用什么域名
形状站群、标题长什么样——把这些摸清楚，过滤规则才能从「见过什么拦什么」
升级成「按形状拦」。

━━ 纪律（和网段拆解同一条）━━
· **只采元数据**：标题 / 摘要 / URL / 域名 / 引擎来源。不抓广告页正文，
  不点跳转链，不登录。我们研究的是「广告长什么样、从哪个口进来」，
  不是广告内容本身。
· 查询词全部是**正常用户真会搜的句子**（社保/减肥/直播/比特币……）。
  违法广告专门劫持这类词，所以撞上它们是必然，不需要主动去搜违禁词。
· 产出是一份 JSONL 证据文件，后续交给免费 LLM 批量分类。

用法：
    python tools/ad_harvest.py                 # 全量采集
    python tools/ad_harvest.py --q "减肥最快的方法"   # 单条试跑
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 正常用户会搜、而违法广告专门劫持的查询。分四组，覆盖四类广告的主战场：
#: 生活服务（诈骗导流）、黑五类（成人/医疗）、娱乐（菠菜/色情）、
#: 金融与科技（资金盘/假交易所）。技术类两条是已实测被劫持过的。
QUERIES = [
    # 生活服务 —— 假客服/假办理/假查询导流的主战场
    "养老保险查询个人账户",
    "社保转移怎么办理",
    "电子发票怎么开",
    "快递单号查询",
    "天气预报",
    # 黑五类 —— 减肥/医疗/成人广告的固定目标词
    "减肥最快的方法",
    "脱发怎么办",
    "失眠怎么调理",
    # 娱乐 —— 菠菜/色情/盗版直播的入口
    "足球直播在线看",
    "手机游戏充值中心",
    "电影在线观看",
    # 金融 —— 资金盘/假交易所/贷款导流
    "贷款计算器",
    "信用卡申请条件",
    "今天比特币什么价",
    # 技术类 —— 已被实测劫持过的两条（见 webtool.py 自测注释）
    "最新的安卓版本",
    "python 安装教程",
]

OUT_DIR = os.path.join(ROOT, "tmp")


def harvest_one(query, limit=20, budget=18.0):
    """单条查询：跑搜索内核，返回原始命中列表（不做任何过滤）。"""
    import fleet.netforce_skynet as NS
    ok, why = NS.available()
    if not ok:
        raise RuntimeError(f"搜索内核不可用：{why}")
    NS._ensure_path()
    from skynet import search as S
    t0 = time.time()
    hits = S.search(query, workers=5, budget=budget)
    if limit and limit > 0:
        hits = hits[:limit]
    out = []
    for h in hits:
        out.append({
            "query": query,
            "url": getattr(h, "url", "") or "",
            "host": (getattr(h, "host", "") or "").lower(),
            "title": (getattr(h, "title", "") or "").strip(),
            # desc：2026-10-24 起 skynet 引擎层开始透传搜索引擎摘要
            # （RSS description / DDG snippet / GitHub API description）。
            # 摘要是广告话术最密集的字段，务必随采。
            "desc": (getattr(h, "desc", "") or "").strip(),
            "engines": list(getattr(h, "engines", []) or []),
            "rel": round(float(getattr(h, "rel", 0) or 0), 4),
            "score": round(float(getattr(h, "score", 0) or 0), 4),
        })
    return out, round(time.time() - t0, 2)


def mark_webtool_verdict(rec):
    """这条 webtool 现在会怎么处理：keep / drop(理由)。

    直接复用 webtool 的真规则，不另写一份——另写一份迟早和真规则漂移，
    得出的「漏网率」就是假的。
    """
    try:
        import webtool
        kws = webtool._keywords(rec["query"])
        why = webtool._junk_reason(
            {"title": rec["title"], "host": rec["host"],
             "url": rec["url"], "snippet": rec["desc"]}, kws)
        if why:
            rec["webtool"] = "drop"
            rec["webtool_why"] = why
        else:
            rec["webtool"] = "keep"
            rec["webtool_why"] = ""
    except Exception as e:
        rec["webtool"] = "err"
        rec["webtool_why"] = f"{type(e).__name__}: {e}"
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", action="append", default=None, help="只跑指定查询")
    ap.add_argument("--out", default=None, help="输出 JSONL 路径")
    args = ap.parse_args(argv)

    queries = args.q or QUERIES
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = args.out or os.path.join(
        OUT_DIR, time.strftime("ad_harvest_%Y-%m-%d.jsonl"))

    total = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for q in queries:
            try:
                recs, dt = harvest_one(q)
            except Exception as e:
                print(f"  ! {q}: {type(e).__name__}: {e}")
                continue
            for r in recs:
                mark_webtool_verdict(r)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(recs)
            print(f"  {q}: {len(recs)} 条 / {dt}s")
    print(f"共 {total} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
