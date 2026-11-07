# -*- coding: utf-8 -*-
"""t81：生活小工具（fun_*）—— 10 个纯 stdlib 能力 + 工具面接线。

守三件事：
  ① 每个小工具的核心结果对（哈希已知值、温度/数据换算、摩斯往返、编解码往返）；
  ② 工具表接线没漏：fun_* 都进了 TOOLS / 权限表 / 决策 schema / 唯一一个域；
  ③ execute() 真的把 fun_* 路由到 fun.run，而不是走到未登记能力上。

全离线、无副作用、不依赖 Android。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import fun, toolselect
import fleet.agent_tools as AT

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


def call(tool, arg, today=None):
    return fun.run(tool, arg, today)


print("① 核心结果")
r = call("fun_hash", "sha256 abc")
check(r["ok"] and "ba7816bf8f01cfea414140de5dae2223" in r["text"], "SHA-256(abc) 已知值")

r = call("fun_password", "24 位")
check(r["ok"] and len(r["text"].split("：")[-1]) == 24, "密码长度按参数生成")
check(any(c in r["text"] for c in "!@#$%^&*-_=+"), "密码含符号")

r = call("fun_passphrase", "6 词")
phrase = r["text"].split("：")[-1]
check(r["ok"] and len(phrase.split("-")) == 7, "口令短语 = 6 词 + 2 位数字")
check("约 49 bit" in r["text"], "6 词口令熵约 49 bit")

r = call("fun_pwdict", "123456")
check(r["ok"] and "命中" in r["text"] and "评分 0/4" in r["text"], "常见弱密码命中且 0 分")
r = call("fun_pwdict", "P@ssw0rd")
check(r["ok"] and "命中" in r["text"] and "leet" in r["text"], "leet 变体被字典还原")
r = call("fun_pwdict", "dragon2026")
check(r["ok"] and "去尾号" in r["text"], "词表词 + 尾号仍命中")
r = call("fun_pwdict", "Tr0ub4dor&3")
check(r["ok"] and "命中" not in r["text"] and "评分 3/4" in r["text"], "非字典密码给 3/4")
check("123456" not in r["text"], "检查结果不回显密码")

r = call("fun_color", "#ff0000")
check(r["ok"] and "rgb(255, 0, 0)" in r["text"] and "hsl(0, 100%, 50%)" in r["text"],
      "颜色 HEX → RGB/HSL")
check("#00FFFF" in r["text"], "红色互补色是青色")

r = call("fun_unit", "10 km mi")
check(r["ok"] and "6.21371" in r["text"], "10 km = 6.21371 mi")
r = call("fun_unit", "100 c f")
check(r["ok"] and "212" in r["text"], "100C = 212F")
r = call("fun_unit", "1 GB MB")
check(r["ok"] and "1024" in r["text"], "1 GB = 1024 MB")
r = call("fun_unit", "1 斤 克")
check(r["ok"] and "500" in r["text"], "1 斤 = 500 克")

r = call("fun_morse", "SOS")
check(r["ok"] and "... --- ..." in r["text"], "SOS 摩斯编码")
r = call("fun_morse", "摩斯解码 ... --- ...")
check(r["ok"] and "SOS" in r["text"], "摩斯解码回 SOS")

r = call("fun_codec", "b64e 你好")
check(r["ok"] and "5L2g5aW9" in r["text"], "Base64 编码")
r = call("fun_codec", "b64d 5L2g5aW9")
check(r["ok"] and "你好" in r["text"], "Base64 解码往返")
r = call("fun_codec", "urle 你好")
check(r["ok"] and "%E4%BD%A0%E5%A5%BD" in r["text"], "URL 编码")

r = call("fun_random", "dice 2d6")
check(r["ok"] and "合计" in r["text"], "2d6 掷骰子有合计")
r = call("fun_random", "coin")
check(r["ok"] and ("正面" in r["text"] or "反面" in r["text"]), "抛硬币")
r = call("fun_random", "pick 甲,乙,丙")
check(r["ok"] and any(x in r["text"] for x in "甲乙丙"), "从列表抽一个")

r = call("fun_date", "2026-12-31", today=date(2026, 11, 7))
check(r["ok"] and "还有 54 天" in r["text"], "倒计时 54 天")
r = call("fun_date", "", today=date(2026, 11, 7))
check(r["ok"] and "星期六" in r["text"], "今天报星期（2026-11-07 是周六）")

print("② 工具面接线")
fun_tools = [t for t in AT.TOOLS if t.name.startswith("fun_")]
check(len(fun_tools) == 10, "10 个 fun_* 都在 TOOLS 里")
check(all(t.cap is None and t.perm == AT.PERM_AUTO and not t.side for t in fun_tools),
      "fun_* 不走注册表、auto 档、无副作用")
check(all(t.name in AT.DEFAULT_TIERS for t in fun_tools), "fun_* 都进了权限表")
check(all(t.name in AT._TOOLS_LINE for t in fun_tools), "fun_* 都在给模型看的描述行里")
check(all(t.name in AT.DECIDE_SCHEMA["properties"]["tool"]["enum"] for t in fun_tools),
      "fun_* 都在决策 schema 的 enum 里")

owners = {}
for dom, (_label, names) in toolselect.DOMAINS.items():
    for n in names:
        owners.setdefault(n, []).append(dom)
check(all(len(owners.get(t.name, [])) == 1 for t in fun_tools),
      "每个 fun_* 只属于一个域（分域没漏也没重）")
check(all(any(n == t.name for n in toolselect.DOMAINS["玩"][1]) for t in fun_tools),
      "10 个 fun_* 都在「玩」域")

check(toolselect.guess_domain("帮我算 sha256")[0] == "玩", "规则把「sha256」判进玩域")
check(toolselect.guess_domain("把 10 公里换算成英里")[0] == "玩", "规则把「换算」判进玩域")
check(toolselect.guess_domain("base64 解码 5L2g")[0] == "玩", "规则把 base64 判进玩域")

print("③ execute() 路由")
label, text, ok = AT.execute("fun_hash", "abc", lambda cap, args: None)
check(ok and "ba7816bf" in text, "execute('fun_hash') 真算出了哈希")
label, text, ok = AT.execute("fun_unit", "1 GB MB", lambda cap, args: None)
check(ok and "1024" in text, "execute('fun_unit') 真做了换算")

print("t81 fun_tools:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
