#!/usr/bin/env python3
"""手机侧人格运行时：记忆包 → system prompt → llama.cpp。

**这个脚本是跑在手机上的**，不依赖 PC。PC 只负责把「记忆包」和「权重」放上去。

为什么渲染要在手机上做（而不是 PC 渲好再推字符串）：
  手机是脱机节点。主人把手机拿出门、PC 关了，它还得记得自己是谁。
  只推一段渲染好的文本，等于把人格绑在 PC 上在线；推「记忆包 + 渲染器」
  才是真下放。两端跑同一份 memory_pack.py，渲染结果字节级一致。

典型用法（在 Termux 里）：
    python3 persona.py --pack ~/skynet/memory/dafeiyu --query "编队怎么样" 
    python3 persona.py --pack ~/skynet/memory/dafeiyu --ask "在吗" \
            --model ~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf --n 160
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_pack import Pack, est_tokens        # noqa: E402

# Qwen 系是 ChatML。这里手拼而不依赖 llama-cli 的 --jinja：
# 不同 Termux 版本的 llama.cpp 对模板读取行为不一致，手拼最稳。
SYS_OPEN = "<|im_start|>system\n"
SYS_CLOSE = "<|im_end|>\n"
USR_OPEN = "<|im_start|>user\n"
ASST_OPEN = "<|im_start|>assistant\n"
STOP = "<|im_end|>"


def build_prompt(system: str, user: str) -> str:
    return (SYS_OPEN + system.rstrip() + SYS_CLOSE
            + USR_OPEN + user.strip() + SYS_CLOSE
            + ASST_OPEN)


def find_llama():
    for name in ("llama-cli", "llama-completion", "main"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def run_llama(binary, model, prompt, n=192, temp=0.7, ctx=2048, threads=0, extra=()):
    """调 llama.cpp 出一次回答。

    prompt 走临时文件（-f）而不是 -p：prompt 里含 <|im_start|> 这类特殊 token，
    走命令行会被 shell 与 llama 的转义各改一次，实测常把模板吃掉。
    """
    fd, pf = tempfile.mkstemp(suffix=".txt", prefix="prompt_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = [binary, "-m", model, "-f", pf,
           "-n", str(n), "--temp", str(temp), "-c", str(ctx),
           "-r", STOP, "--no-display-prompt", "-cnv"]
    if threads:
        cmd += ["-t", str(threads)]
    cmd += list(extra)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=900)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip(), err
    finally:
        try:
            os.unlink(pf)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="手机侧人格运行时")
    ap.add_argument("--pack", required=True, help="记忆包目录（含 pack.json）")
    ap.add_argument("--budget", type=int, default=800, help="system prompt token 预算")
    ap.add_argument("--query", default="", help="相关度查询（决定哪些经历被拉进来）")
    ap.add_argument("--sys-out", default="", help="把渲染结果写到这个文件")
    ap.add_argument("--show", action="store_true", help="把渲染结果打印到 stdout")
    ap.add_argument("--ask", default="", help="问一句话（触发一次真推理）")
    ap.add_argument("--model", default="", help="gguf 路径")
    ap.add_argument("--n", type=int, default=192)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--threads", type=int, default=0)
    args = ap.parse_args(argv)

    if not os.path.isdir(args.pack):
        print(f"[X] 记忆包不存在：{args.pack}", file=sys.stderr)
        return 2
    pack = Pack.load(args.pack)
    if not len(pack):
        print(f"[X] 记忆包是空的：{args.pack}", file=sys.stderr)
        return 2

    r = pack.render(budget_tokens=args.budget, query=args.query)
    # 统计一律走 stderr：stdout 要留给「回答」或「纯 prompt」，方便管道
    print(f"[i] pack={r['pack']} name={r['name']} tokens={r['tokens']}/{args.budget} "
          f"kept={r['kept']} dropped={r['dropped']}"
          + (" OVER_BUDGET" if r["over_budget"] else ""), file=sys.stderr)
    print(f"[i] by_kind={r['by_kind']}", file=sys.stderr)

    if args.sys_out:
        with open(args.sys_out, "w", encoding="utf-8") as f:
            f.write(r["text"])
    if args.show or not args.ask:
        sys.stdout.write(r["text"] + "\n")

    if not args.ask:
        return 0

    if not args.model:
        print("[X] --ask 需要 --model", file=sys.stderr)
        return 2
    if not os.path.exists(args.model):
        print(f"[X] 模型不存在：{args.model}", file=sys.stderr)
        return 2
    binary = find_llama()
    if not binary:
        print("[X] 找不到 llama-cli（Termux: pkg install llama-cpp）", file=sys.stderr)
        return 2

    prompt = build_prompt(r["text"], args.ask)
    print(f"[i] prompt_tokens≈{est_tokens(prompt)} ctx={args.ctx}", file=sys.stderr)
    rc, out, err = run_llama(binary, args.model, prompt, n=args.n, temp=args.temp,
                             ctx=args.ctx, threads=args.threads)
    if rc != 0 and not out:
        print(f"[X] llama 退出 {rc}：{err.strip()[-400:]}", file=sys.stderr)
        return 3
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
