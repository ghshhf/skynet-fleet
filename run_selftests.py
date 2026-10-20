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
    ("node_agent", ["node_agent.py", "--selftest"], HERE, 30),
    ("install_windows", ["install_windows.py", "--selftest", "--password", "demo123"], HERE, 90),
    ("ui_automation", ["ui_automation.py", "--selftest"], os.path.join(HERE, "ui_automation"), 30),
    ("rules_relay", ["rules_relay.py", "--selftest"], os.path.join(HERE, "ui_automation"), 30),
    ("rule_engine", ["rule_engine.py", "--selftest"], os.path.join(HERE, "rfc", "rule-engine"), 30),
    ("policies", ["policies.py", "--selftest"], HERE, 30),
    ("arbiter", ["arbiter.py", "--selftest"], HERE, 30),
    ("discovery", ["discovery.py", "--selftest"], HERE, 120),
    ("sweep", ["sweep.py", "--selftest"], HERE, 120),
    ("accounts", ["accounts.py", "--selftest"], HERE, 60),
    ("compute_runner", ["compute_runner.py", "--selftest"], HERE, 40),
    ("queue_store", ["queue_store.py", "--selftest"], HERE, 30),
    ("persona", ["persona.py", "--selftest"], HERE, 30),
    ("recall", ["recall.py", "--selftest"], HERE, 30),
    ("enroll", ["enroll.py", "--selftest"], HERE, 30),
    ("enroll_install", ["install_windows.py", "--enroll-selftest"], HERE, 30),
    ("journal", ["journal.py", "--selftest"], HERE, 30),
    ("followups", ["followups.py", "--selftest"], HERE, 30),
    ("android_device", ["android_device.py", "--selftest"], HERE, 30),
    ("infrared", ["infrared.py", "--selftest"], HERE, 30),
    ("agent_phone", ["agent_phone.py", "--selftest"], HERE, 60),
    ("survey", ["survey.py", "--selftest"], HERE, 30),
    ("ble", ["ble.py", "--selftest"], HERE, 30),
    ("nearby", ["nearby.py", "--selftest"], HERE, 30),
    ("perceive", ["perceive.py", "--selftest"], HERE, 30),
    ("phone_state", ["phone_state.py", "--selftest"], HERE, 30),
    ("pc_state", ["pc_state.py", "--selftest"], HERE, 30),
    ("pc_usage", ["pc_usage.py", "--selftest"], HERE, 30),
    ("netpath", ["netpath.py", "--selftest"], HERE, 30),
    ("registry", ["registry.py", "--selftest"], HERE, 30),
    ("capabilities", ["capabilities.py", "--selftest"], HERE, 30),
    # ---- D0 契约层（结果信封 / 设备基类 / 注册表）----
    ("fleet_result", ["-m", "fleet.core.result"], HERE, 30),
    ("fleet_registry", ["-m", "fleet.core.registry"], HERE, 30),
    ("fleet_list", ["-m", "fleet", "list"], HERE, 120),
    # ---- evals：一次真事故一个脚本 ----
    ("eval_d0_bridge_offline", ["evals/d0_bridge_offline.py"], HERE, 90),
    ("eval_t0_input", ["evals/t0_input_not_clobbered.py"], HERE, 90),
    # ---- T1：全屏界面 + 两个显影面共用一套 dispatch ----
    ("fleet_tui_smoke", ["-m", "fleet.tui", "--smoke"], HERE, 240),
    ("eval_t1_two_faces", ["evals/t1_cli_and_tui_share_dispatch.py"], HERE, 240),
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
