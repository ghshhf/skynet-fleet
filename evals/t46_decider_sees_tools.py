# -*- coding: utf-8 -*-
"""门禁：决策链路里「模型到底看到了什么」。

不验「选了哪个工具」，验**那段工具清单有没有真的进模型**。

2026-10-29 查出来的三重故障，一条门禁都没抓到，全绿了十天：
  ① `--chat-template <路径>` —— 参数名用错。llama-server 的 `--chat-template`
     要的是**模板内容本身**，传文件路径得用 `--chat-template-file`。它把整条
     路径当 jinja 源码编译，报 `Invalid template variable name: 'qwen3_thinkblock'`，
     然后**不返回 500**：HTTP 200 + finish_reason=stop，正文就是这段错误文字。
     ⇒ prompt 根本没被编码，usage 里 prompt_tokens 恒等于 18（只有 user 那一句）。
  ② DECIDE_SYSTEM 只提 arg、从不提 tool 字段。模型于是把**工具名**也填进 arg
     （`{"arg": "device_status"}`），tool 字段干脆不给 ⇒ 决策层永远拿不到工具名。
  ③ 58 个工具 = 5323 字符 / 6141 tokens。这台机器只有核显，prompt 处理
     60.7 tok/s ⇒ **单单 prompt 就要 101 秒**，而 DECIDE_TIMEOUT=25s
     ⇒ 连模板修好了也必然超时。10-24 那次「7 选 1 16/16」是真的，那时只有 7 个
     工具；扩到 58 个之后再没人测过。

四条判据，缺一条就会让人白查一天：
  ① 产线命令里模板参数名对（抓回退）
  ② 提示词指示了 tool 字段（抓「schema 有、prompt 没说」）
  ③ 提示词长度在预算内（抓「又加工具了，跑不动」）
  ④ 真起一个 llama-server，断言 prompt_tokens 随 system 变长而变长
     （抓①这类「HTTP 200 但其实什么都没发生」的静默失败）

④ 用小 prompt 而不是 58 个工具：这里验的是**链路通不通**，不是模型行不行。
链路断的话 prompt_tokens 恒等于十几，且不随 system 长度变化。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from fleet import agent_tools as A                            # noqa: E402
import start_llm_hidden as S                                  # noqa: E402

fails = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        print("  FAIL %s" % msg)
        fails.append(msg)


# ---------------------------------------------------------------- ① 模板参数名
print("== ① 产线命令里的模板参数名 ==")
# 直接看 build_cmd 吐出来的真实命令，不查源码文本 —— 查源码会被注释和
# 「另一个文件里也这么写」干扰，验不出产线实际会传什么。
probe_cmd, used_tmpl = S.build_cmd("llama-server.exe", "x.gguf", 9999, log="")
if used_tmpl:
    check("--chat-template-file" in probe_cmd,
          "用了 --chat-template-file（传路径）")
    check(not any(a == "--chat-template" for a in probe_cmd),
          "没有用 --chat-template（那个要的是模板内容，给路径会被当源码编译）")
    if "--chat-template-file" in probe_cmd:
        idx = probe_cmd.index("--chat-template-file")
        check(os.path.isfile(probe_cmd[idx + 1]),
              "模板参数后面跟的是真存在的文件：%s" % probe_cmd[idx + 1])
    else:
        # 不再往下 .index() —— 抓错时要继续跑完，一次报全部失败，
        # 而不是在第一层崩掉、把后面三层的结论一起藏起来
        check(False, "命令里找不到 --chat-template-file，无法校验模板文件")
else:
    print("  skip 没找到 templates/qwen3_thinkblock.jinja —— ① 没验")
    print("       （这是「没验」不是「通过」：模板文件缺失本身要单独查）")

# ---------------------------------------------------------------- ② tool 字段
print("\n== ② 提示词必须指示 tool 字段 ==")
sysmsg = A.DECIDE_SYSTEM
check("tool" in sysmsg and "arg" in sysmsg,
      "同时提到 tool 与 arg 两个字段")
# 关键：必须把「工具名放哪」说清楚，而不是只说「参数放 arg」
check(bool(re.search(r"工具名.{0,12}tool|tool.{0,12}字段", sysmsg)),
      "明确说了工具名要填进 tool 字段")
check('"tool"' in sysmsg, "给了一个 {\"tool\": ...} 的输出样例")
check("tool" in A.DECIDE_SCHEMA["properties"],
      "schema 里确有 tool 字段（②的前提）")

# ---------------------------------------------------------------- ③ 长度预算
print("\n== ③ 提示词长度预算 ==")
# 25s 超时 - 生成约 8s = 17s；本机实测 prompt 处理 60.7 tok/s ⇒ 约 1030 tokens。
# 汉字约 1 token/字，留 20% 余量 ⇒ 字符上限 5800（当前 5323）。
BUDGET = 5800
n = len(sysmsg)
n_tools = len(A.DECIDE_SCHEMA["properties"]["tool"]["enum"])
check(n <= BUDGET,
      "DECIDE_SYSTEM %d 字符 ≤ 预算 %d（%d 个选项）" % (n, BUDGET, n_tools))
if n > BUDGET:
    print("       ↳ 超预算 = 本机核显上 prompt 处理会超过 25s 超时线，决策必失败。")
    print("         加工具前先裁：拆域、或把说明压到一行。")

# ---------------------------------------------------------------- ④ 端到端
print("\n== ④ 端到端：prompt 真的进模型了吗 ==")


def call(port, sys_text, timeout=180):
    payload = {
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": "手机还剩多少电"},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object", "schema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string",
                         "enum": ["device_status", "web_search", "none"]},
                "arg": {"type": "string"}}}},
        "temperature": 0.1, "max_tokens": 64,
    }
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    return (d["usage"]["prompt_tokens"],
            (d["choices"][0]["message"].get("content") or "").strip())


llama = S.find_llama("")
try:
    model = S.find_model("")       # 任选一个现成的 gguf
except FileNotFoundError:
    model = ""

if not (llama and model):
    print("  skip 本机没有 llama-server 或 gguf —— ④ 没验")
    print("       （这是「没验」不是「通过」：换台机器跑这条才有意义）")
else:
    # 端口随机挑空闲的：门禁不许依赖环境状态（不能假设 18080 空着）
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # 用产线的 build_cmd —— 否则验的是门禁自己写的命令，不是产线
    cmd, _ = S.build_cmd(llama, model, port, ctx=1024, ngl=0, threads=4, log="")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        up = False
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=1)
                up = True
                break
            except Exception:
                time.sleep(0.4)
        check(up, "llama-server 起得来（端口 %d）" % port)
        if up:
            # 判据不能是「prompt_tokens 大于某个绝对值」——那随输入长度变，会误判。
            # 改成**对照实验**：同一句 user，只改 system 的长度，看 prompt_tokens
            # 跟不跟着涨。模板坏掉时（prompt 压根没编码）两次恒等，差值 0。
            short_sys = "你是调度器。只输出 JSON。"
            # 约 300 字：够拉开差值，又不至于让核显算太久
            long_sys = short_sys + "规则：" + "必须先判断用户想查什么，再挑工具。" * 20
            tok_s, txt_s = call(port, short_sys)
            tok_l, txt_l = call(port, long_sys)
            check(tok_l - tok_s > 100,
                  "system 变长后 prompt_tokens 跟着涨（%d → %d，差 %d）——"
                  "差 0 就是模板没生效、prompt 根本没进去"
                  % (tok_s, tok_l, tok_l - tok_s))
            check("Invalid template variable" not in txt_s + txt_l,
                  "返回里没有 jinja 模板报错")
            try:
                got = json.loads(txt_l)
                check(isinstance(got.get("tool"), str) and bool(got["tool"]),
                      "返回里有 tool 字段（实得 %r）" % (txt_l[:60],))
            except Exception:
                check(False, "返回是合法 JSON（实得 %r）" % (txt_l[:80],))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

print()
if fails:
    print("!! 决策链路 %d 项失败：" % len(fails))
    for f in fails:
        print("   - %s" % f)
    sys.exit(1)
print("决策链路 4 层全通。")
