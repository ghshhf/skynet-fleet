"""天网工程 · 一键离线自测。

把仓库里所有「不需要真机、不需要网络」的自测串起来跑，作为每轮改动的门禁：
  python run_selftests.py            # 全跑
  python run_selftests.py ui rule    # 只跑名字含 ui/rule 的

任一失败即退出码 1。用便携 python 亦可：/d/tools/python/python.exe run_selftests.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# (名字, 命令参数, 工作目录, 超时秒)
CASES = [
    ("commander", ["commander.py", "--self-test", "--password", "demo123"], HERE, 120),
    ("install_windows", ["install_windows.py", "--selftest", "--password", "demo123"], HERE, 90),
    ("ui_automation", ["ui_automation.py", "--selftest"], os.path.join(HERE, "ui_automation"), 30),
    ("rule_engine", ["rule_engine.py", "--selftest"], os.path.join(HERE, "rfc", "rule-engine"), 30),
]


def _dec(b):
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp936", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def run_case(name, args, cwd, timeout):
    cmd = [PY] + args
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        out = _dec(r.stdout)
        err = _dec(r.stderr)
        ok = r.returncode == 0
        return ok, out, err, r.returncode
    except subprocess.TimeoutExpired:
        return False, "", f"超时({timeout}s)", -1
    except FileNotFoundError as e:
        return False, "", f"找不到: {e}", -1


def main():
    only = [a.lower() for a in sys.argv[1:]]
    cases = [c for c in CASES if not only or any(o in c[0].lower() for o in only)]
    print(f"=== 天网工程离线自测：{len(cases)} 项 ===", flush=True)
    results = []
    for name, args, cwd, timeout in cases:
        print(f"\n----- {name} -----", flush=True)
        ok, out, err, rc = run_case(name, args, cwd, timeout)
        tail = "\n".join((out or "").strip().splitlines()[-8:])
        if tail:
            print(tail)
        if err.strip():
            print("[stderr]", "\n".join(err.strip().splitlines()[-4:]))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} (rc={rc})", flush=True)
        results.append((name, ok))

    passed = sum(1 for _, ok in results if ok)
    print(f"\n=== 结果：{passed}/{len(results)} 通过 ===")
    if passed != len(results):
        print("失败项:", ", ".join(n for n, ok in results if not ok))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
