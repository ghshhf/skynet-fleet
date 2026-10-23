"""让手机用「指定记忆包 + 指定权重」回答一句话。

一条命令串完整条链：本地渲染记忆包 → 拼 ChatML → 推到手机 → llama-cli → 解析回答。

为什么要做成命令而不是手敲 adb：
  「同一份权重，换包就换人」这个结论必须**可重复、可对比**才站得住。
  手敲 adb 敲出来的两次实验，环境、参数、prompt 拼接都会漂，
  得出的「风格不同」根本不能归因于记忆包。

用法：
    python tools/phone_ask.py --pack personas/dafeiyu --ask "在吗"
    python tools/phone_ask.py --pack personas/jishuyuan --ask "在吗" --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import adbx                                     # noqa: E402
from fleet import memory_pack as M              # noqa: E402

PHONE_HOME = "/data/user/0/com.termux/files/home"
PROMPT_NAME = "skynet_tmp_prompt.txt"
# 手机上本来就有的中文 LLM（639MB Q8_0）。默认用它而不是我另下的 1.5B：
# 主人手机里的模型是挑过的，1.5B 只是我为了验证链路下的备胎。
DEFAULT_MODEL = "models/北极星-V7-Q8_0.gguf"
BIG_MODEL = "models/Qwen3VL-4B-Instruct-Q4_K_M.gguf"
BIG_MMPROJ = "models/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"

SYS_OPEN, SYS_CLOSE = "<|im_start|>system\n", "<|im_end|>\n"
USR_OPEN = "<|im_start|>user\n"
ASST_OPEN = "<|im_start|>assistant\n"
STOP = "<|im_end|>"

# banner 行形如 "model      : /path" / "ftype      : Q4_K - Medium"；
# 计时行形如 "[ Prompt: 28.8 t/s | Generation: 19.2 t/s ]"
_TIMING = re.compile(r"\[\s*Prompt:.*?t/s.*?\]")
_BANNER = re.compile(r"^[a-z][a-z0-9_ ]{2,14}\s{2,}: ")


def build_prompt(system: str, user: str) -> str:
    return (SYS_OPEN + (system or "").rstrip() + SYS_CLOSE
            + USR_OPEN + user.strip() + SYS_CLOSE + ASST_OPEN)


def clean_output(raw: str, prompt: str = "") -> tuple:
    """从 llama-cli 的混排输出里挑出「回答」。

    llama-cli 在 -f 模式下会把**整个 prompt 回显一遍**（连 <|im_start|> 都打出来），
    然后紧接着才是生成的内容。所以不能按行过滤，必须按最后一次 ASST_OPEN 切一刀——
    那是 prompt 的结尾，也是回答的起点。切完再砍 banner 与计时行。
    """
    text = (raw or "").replace("\r\n", "\n")
    m = _TIMING.search(text)
    stat = m.group(0) if m else ""
    if m:
        text = text[:m.start()]
    idx = text.rfind(ASST_OPEN)
    if idx >= 0:
        text = text[idx + len(ASST_OPEN):]
    kept = []
    for line in text.split("\n"):
        if _BANNER.match(line):
            continue
        if line.strip() in ("Exiting...", "available commands:"):
            continue
        if line.startswith("  /"):
            continue
        kept.append(line)
    body = "\n".join(kept).strip()
    body = body.replace(STOP, "").replace("> Exiting...", "").strip()
    body = re.sub(r"^>\s*", "", body)
    return body.strip(), stat


def push_prompt(text: str, remote_name=PROMPT_NAME):
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="skynet_prompt_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        stage = f"/data/local/tmp/{remote_name}"
        rc, out, err = adbx.push(tmp, stage)
        if rc != 0:
            raise SystemExit(f"推 prompt 失败：{err.strip()[:200]}")
        rc, out, err = adbx.termux(
            f"cp -f {stage} {PHONE_HOME}/{remote_name} && wc -c {PHONE_HOME}/{remote_name}")
        if rc != 0:
            raise SystemExit(f"落 prompt 失败：{err.strip()[:200]}")
        return f"{PHONE_HOME}/{remote_name}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def ask(pack_dir, question, model_rel=DEFAULT_MODEL, n=160, threads=6,
        budget=800, query="", temp=0.7, ctx=2048, timeout=600, mmproj=""):
    pack = M.Pack.load(pack_dir)
    if not len(pack):
        raise SystemExit(f"记忆包为空：{pack_dir}")
    r = pack.render(budget_tokens=budget, query=query or question)
    prompt = build_prompt(r["text"], question)
    remote = push_prompt(prompt)
    model = model_rel if model_rel.startswith("/") else f"{PHONE_HOME}/{model_rel}"

    # 注意两件事，都踩过：
    # ① 不能加 -r/--reverse-prompt：它的语义是「命中就交还控制权回到交互模式」，
    #    生成完会一直等 stdin，而 adb 的 stdin 永远不 EOF —— 表现为挂死几分钟不吐字节。
    # ② 必须重定向 stdin：adb shell 会给 pty，llama-cli 见 tty 就进交互模式。
    #    加 </dev/null 之后行为才确定（这一条比 ① 更隐蔽，症状一模一样）。
    mm = ""
    if mmproj:
        mm = " --mmproj " + (mmproj if mmproj.startswith("/") else f"{PHONE_HOME}/{mmproj}")
    script = (f"if [ ! -f {model} ]; then echo MODEL_MISSING; exit 4; fi\n"
              f"llama-cli -m {model} -f {remote} -n {n} -t {threads} "
              f"--no-warmup -c {ctx} --temp {temp}{mm} </dev/null 2>&1")
    t0 = time.time()
    rc, out, err = adbx.termux(script, timeout=timeout)
    dt = time.time() - t0
    if "MODEL_MISSING" in (out or ""):
        raise SystemExit(f"手机上没有这个权重：{model}")
    answer, stat = clean_output(out, prompt)
    return {"pack": r["pack"], "pack_tokens": r["tokens"], "kept": r["kept"],
            "dropped": r["dropped"], "by_kind": r["by_kind"],
            "question": question, "answer": answer, "timing": stat,
            "elapsed_s": round(dt, 1), "rc": rc, "model": model,
            "raw_tail": (out or "")[-600:] if not answer else ""}


def main(argv=None):
    ap = argparse.ArgumentParser(description="手机用指定记忆包回答")
    ap.add_argument("--pack", required=True)
    ap.add_argument("--ask", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL, help="手机上的相对路径或绝对路径")
    ap.add_argument("--mmproj", default="", help="多模态视觉投影（Qwen3VL 用）")
    ap.add_argument("--big", action="store_true", help="用手机自带的 Qwen3VL-4B（2.5G）")
    ap.add_argument("--n", type=int, default=160)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--budget", type=int, default=800)
    ap.add_argument("--query", default="")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    model, mmproj = args.model, args.mmproj
    if args.big:
        model, mmproj = BIG_MODEL, (mmproj or BIG_MMPROJ)
    r = ask(args.pack, args.ask, model, args.n, args.threads,
            args.budget, args.query, args.temp, mmproj=mmproj)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"[i] {r['pack']} tokens={r['pack_tokens']} kept={r['kept']} "
              f"dropped={r['dropped']} {r['timing']} {r['elapsed_s']}s")
        print(f"Q: {r['question']}")
        print(f"A: {r['answer'] or '(空)'}")
        if r["raw_tail"]:
            print("--- raw ---")
            print(r["raw_tail"])
    return 0 if r["answer"] else 3


if __name__ == "__main__":
    sys.exit(main())
