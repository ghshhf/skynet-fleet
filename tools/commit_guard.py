#!/usr/bin/env python
"""提交前防呆：核对待提交内容里有没有混进别人的在途文件。

为什么需要这个（2026-10-21 真事故两次）：
  ① agent-1 的 adb_wifi.py 被我 `git add commander.py` 顺带卷进提交；
  ② agent-1 的工作区被裸 `git stash` 卷走（stash 会把所有人的未提交改动一起收走）。

所以：**在共享仓库上，提交前必须核对自己到底要提交什么。**

用法：
    python tools/commit_guard.py <路径1> <路径2> ...
    python tools/commit_guard.py --all          # 看当前暂存区（不传路径时）

退出码：
    0 = 暂存区只含你列出的路径，可以提交
    1 = 暂存区有未列出的文件（可能混进了别人的活）——先核清再提交
    2 = 你列出的路径还没被 add
"""
import subprocess
import sys


def _git(*args):
    # core.quotepath=false：中文路径必须原样输出。
    # git 默认把非 ASCII 路径转义成八进制（"docs/\350\212\213..."），
    # 那样脚本认不出「这就是我列出的那个文件」，会同时误报多与漏。
    return subprocess.run(["git", "-c", "core.quotepath=false"] + list(args),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def main(argv):
    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "--all":
        staged = [l for l in _git("diff", "--cached", "--name-only").splitlines() if l]
        if not staged:
            print("[commit-guard] 暂存区是空的。")
            return 0
        print("[commit-guard] 当前暂存区内容：")
        for s in staged:
            print("  -", s)
        print(f"[commit-guard] 共 {len(staged)} 个文件。逐条核对是否都是你的活。")
        return 0

    wanted = {p.strip('"').rstrip("/\\").replace("\\", "/") for p in argv}

    staged = [l for l in _git("diff", "--cached", "--name-only").splitlines() if l]
    # 双保险：万一 git 仍带引号（路径含空格等特殊字符时会加），去掉。
    staged_set = {s.strip('"').replace("\\", "/") for s in staged}

    missing = sorted(wanted - staged_set)
    extra = sorted(staged_set - wanted)

    if extra:
        print("[commit-guard] ✗ 暂存区里有你**没列出**的文件：")
        for e in extra:
            print("    -", e)
        print("  ⇒ 这些可能是别的 agent 的在途活。逐条核清：是你的就补进清单；")
        print("    不是你的就用 `git restore --staged <路径>` 撤出暂存区。")
    if missing:
        print("[commit-guard] ✗ 你列出但**还没 add**的路径：")
        for m in missing:
            print("    -", m)
        print("  ⇒ `git add` 后再提交。")

    if extra or missing:
        return 1

    print(f"[commit-guard] ✓ 暂存区只含你列出的 {len(wanted)} 个路径，可以提交。")
    for w in sorted(wanted):
        print("    -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
