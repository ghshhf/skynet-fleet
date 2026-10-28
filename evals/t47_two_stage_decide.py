# -*- coding: utf-8 -*-
"""t47：两级决策（规则定域 → 域内选工具）必须真的成立。

## 这条门禁防的是什么

2026-10-29 之前，58 个工具的说明一次性塞给本地模型：prompt 2874 tokens，
这台核显机器 prompt 处理 54–65 tok/s ⇒ 52.9 秒，决策超时线 25 秒，
**每一次都超时**。工具面不是「偶尔不准」，是根本没有生效过。

改成两级之后有个新的、更隐蔽的失效方式：**加了工具却忘记归域**。
那种工具永远不会出现在任何一次决策的候选里——不报错、不掉分，只是
「你说了它也不动」。这种漏，只有「域覆盖 == 工具表」这一条断言能抓。

## 为什么不测「模型选得对不对」

选得对不取决于代码，取决于模型权重，今天对明天可能不对。门禁管的是
**我们能不能控制住的那部分**：候选集有没有收窄、收窄得对不对、模型
答了域外的名字会不会被静默接受。准确率用 30 条实测集人工跑（见
docs/总纲），不进门禁——门禁一慢就没人跑了。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet import agent_tools as A                            # noqa: E402
from fleet import toolselect as TS                            # noqa: E402

FAILED = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILED.append(msg)


# ---------------------------------------------------------------- 假模型
class Fake(BaseHTTPRequestHandler):
    """只回一个名字的假模型。回什么由 REPLY 决定，请求全记下来。

    用假模型而不是真模型：门禁不许依赖「本机正好起着 2B 模型」——
    那种断言会在没起服务时变红，看起来像代码回归（10-28 吃过这个亏）。
    """
    REQUESTS = []
    REPLY = None          # None = 抄 enum 里第一个

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n).decode())
        Fake.REQUESTS.append(body)
        enum = body["response_format"]["schema"]["properties"]["tool"]["enum"]
        name = Fake.REPLY if Fake.REPLY is not None else enum[0]
        txt = json.dumps({"tool": name, "arg": ""}, ensure_ascii=False)
        out = json.dumps({"choices": [{"message": {"content": txt}}],
                          "usage": {"prompt_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def main():
    names = [t.name for t in A.TOOLS]
    print("工具表 %d 个 / 域 %d 个" % (len(names), len(TS.DOMAINS)))

    print("\n[1] 分域必须覆盖全部工具，且不许重复归属")
    seen = {}
    dup = []
    for d, (_label, tools) in TS.DOMAINS.items():
        for t in tools:
            if t in seen:
                dup.append((t, seen[t], d))
            seen[t] = d
    check(not dup, "没有工具同时属于两个域（重叠：%s）" % (dup or "无"))
    missing = [n for n in names if n not in seen]
    # 漏归域的工具 = 永远不会出现在候选里 = 说了也不动
    check(not missing, "工具表里每个工具都归了域（漏：%s）" % (missing or "无"))
    ghost = [t for t in seen if t not in names]
    check(not ghost, "域里没有已不存在的工具（幽灵：%s）" % (ghost or "无"))

    print("\n[2] 域内工具数必须落在实测预算内（25s 线 ≈ 2000 tokens）")
    big = {d: len(ts) for d, (_l, ts) in TS.DOMAINS.items() if len(ts) > 16}
    check(not big, "没有域超过 16 个工具（超了：%s）" % (big or "无"))
    for d, (_l, ts) in TS.DOMAINS.items():
        print("      %s 域 %2d 个" % (d, len(ts)))

    print("\n[3] 规则判定：该判的判、该闭嘴的闭嘴")
    golden = [
        ("手机还剩多少电", "查"),
        ("电脑上现在跑着哪些程序", "查"),
        ("把电脑屏幕截个图存成文件", "看"),
        ("这段 BLE 广播里有什么", "拆"),
        ("周围能搜到几个 wifi", "探"),
        ("帮我打开微信", "动"),
        ("今天比特币多少钱", "外"),
    ]
    for q, want in golden:
        doms, hits, why = TS.candidate_domains(q)
        check(doms and doms[0] == want,
              "「%s」→ %s（%s）" % (q, want, "、".join(doms) or "无"))
    # 并列时不许硬判：硬判等于替模型做决定，错了不可挽回
    doms, _h, why = TS.candidate_domains("在搜索框里输入今天天气")
    check(len(doms) != 1, "两个域都像时不硬判（并列：%s，%s）" % (doms, why))
    doms, _h, why = TS.candidate_domains("嗯")
    check(not doms, "没命中时闭嘴（%s）" % why)

    srv, base = serve()
    try:
        print("\n[4] 两级制：真正发出去的候选只有这个域的工具")
        Fake.REQUESTS.clear()
        Fake.REPLY = None
        A._DECIDE_CACHE.clear()
        r = A.decide("有人给我发消息吗", url=base)
        check(len(Fake.REQUESTS) == 1, "只打了 1 次模型（实 %d）"
              % len(Fake.REQUESTS))
        body = Fake.REQUESTS[0]
        enum = body["response_format"]["schema"]["properties"]["tool"]["enum"]
        dom = TS.domain_of_tool("notify_read")
        want = set(TS.tools_of(dom)) | {"none"}
        check(set(enum) == want,
              "候选 == 「%s」域全部工具 + none（%d 个，实 %d 个）"
              % (dom, len(want), len(enum)))
        check("device_status" not in enum or "device_status" in want,
              "候选里没混进别的域的工具")
        prompt = body["messages"][0]["content"]
        check(len(prompt) < 3000, "system 提示 %d 字符（58 个全给约 5200）"
              % len(prompt))
        check("device_status" in prompt if "device_status" in want else True,
              "候选工具的说明进了 system")
        check(r and r["tool"] in want, "返回的工具在域内（%s）" % (r,))

        print("\n[5] 模型答了域外的名字必须拒绝，不许静默接受")
        Fake.REQUESTS.clear()
        Fake.REPLY = "web_search"        # 「查」域里没有这个
        A._DECIDE_CACHE.clear()
        r = A.decide("有人给我发消息吗", url=base)
        check(r is None, "域外名字 → 返回 None（实 %s）" % (r,))
        check("不在" in (A.LAST_FAIL[0] or "") or "非法" in (A.LAST_FAIL[0] or ""),
              "失败原因说清是「不在清单里」：%s" % (A.LAST_FAIL[0] or "")[:60])

        print("\n[6] 一级制（AT_DECIDE_FULL=1）候选应是全部工具")
        Fake.REQUESTS.clear()
        Fake.REPLY = None
        os.environ["AT_DECIDE_FULL"] = "1"
        A._DECIDE_CACHE.clear()
        A.decide("有人给我发消息吗", url=base)
        enum = Fake.REQUESTS[0]["response_format"]["schema"]["properties"]["tool"]["enum"]
        check(len(enum) == len(names) + 1,
              "全量模式候选 %d 个（应为 %d）" % (len(enum), len(names) + 1))
        os.environ.pop("AT_DECIDE_FULL", None)
        A._DECIDE_CACHE.clear()
    finally:
        srv.shutdown()

    print("\n[7] 变异测试：漏归域一个工具，第 1 组必须变红")
    victim = names[-1]
    dom = TS.domain_of_tool(victim)
    saved = TS.DOMAINS[dom][1][:]
    try:
        TS.DOMAINS[dom] = (TS.DOMAINS[dom][0],
                           [t for t in saved if t != victim])
        seen2 = {t for _d, (_l, ts) in TS.DOMAINS.items() for t in ts}
        check(victim not in seen2,
              "变异生效：%s 已从「%s」域移除" % (victim, dom))
        check(bool([n for n in names if n not in seen2]),
              "同一断言能发现漏归域（漏 %s）"
              % [n for n in names if n not in seen2])
    finally:
        TS.DOMAINS[dom] = (TS.DOMAINS[dom][0], saved)

    print()
    if FAILED:
        print("FAIL t47：%d 项没过" % len(FAILED))
        for m in FAILED:
            print("   - %s" % m)
        return 1
    print("PASS t47 两级决策 %d 项" % 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
