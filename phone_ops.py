"""手机操作层：把「看 — 动 — 核对」串成一步。

上层（人，或者模型）只说"打开计算器，算 12 乘 34"，不需要知道 1440x3200 里
哪个坐标是哪个键。每步都留回执：点了什么、界面变没变、前台是不是预期的 app。

为什么每一步都要核对：没有核对的自动点击等于闭眼操作——点了不等于点对了，
UI 树没变就说明这一下没生效（或者点到了死区），必须让它自己发现。
"""
import json
import sys
import time

import android_device as A


def read_screen(serial):
    """当前屏幕：前台是谁 + 所有可见文字。这就是"看"。"""
    got = A.ui_xml(serial)
    nodes = A.parse_ui(got["xml"]) if got["ok"] else []
    info = A.info(serial)
    labels = [A.label(n) for n in nodes if A.label(n)]
    return {"ok": got["ok"], "foreground": info.get("foreground", ""),
            "nodes": nodes, "labels": labels,
            "err": "" if got["ok"] else got.get("err", "")}


def open_app(serial, target, expect="", timeout=8):
    """启动应用并等它真的到前台。expect 给包名时用它核对，否则用 target 本身。"""
    r = A.launch(serial, target)
    if not r["ok"]:
        return {"ok": False, "err": (r.get("out") or "").strip()[:200]}
    want = expect or target
    w = A.wait_foreground(serial, want, timeout=timeout)
    return {"ok": w["ok"], "foreground": want,
            "err": "" if w["ok"] else w.get("err", "")}


def press(serial, label, dry_run=False, verify=True):
    """按屏幕上的文字点一下，点完回读界面判断这一下有没有生效。"""
    before = read_screen(serial) if verify and not dry_run else {"labels": [], "foreground": ""}
    r = A.tap_text(serial, label, dry_run=dry_run)
    if not r["ok"] or dry_run:
        return r
    time.sleep(0.5)
    after = read_screen(serial) if verify else {"labels": [], "foreground": ""}
    return {**r, "changed": before["labels"] != after["labels"],
            "foreground": after["foreground"]}


def type_text(serial, text):
    return A.text_input(serial, text)


def read_value(serial):
    """读当前输入框的值（计算器结果、搜索框内容都在这）。"""
    r = A.bridge_cmd(serial, "FOCUS")
    if not r["ok"]:
        return {"ok": False, "err": r.get("err", "")}
    txt = r["out"][len("OK focus "):] if r["out"].startswith("OK focus ") else r["out"]
    cls, _, value = txt.partition(" | ")
    return {"ok": True, "widget": cls.strip(), "value": value.strip()}


def wait_for(serial, keyword, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        scr = read_screen(serial)
        if any(keyword in l for l in scr["labels"]):
            return {"ok": True, "foreground": scr["foreground"]}
        time.sleep(0.5)
    return {"ok": False, "err": f"{timeout}s 内界面上没出现「{keyword}」"}


def run_steps(serial, steps):
    """顺序执行。任何一步失败立刻停，不带着错误往下点。

    steps 形如 [["open","com.miui.calculator"], ["press","1"], ["wait","56"]]
    """
    log = []
    for step in steps:
        op = step[0]
        arg = step[1] if len(step) > 1 else ""
        if op == "open":
            r = open_app(serial, arg)
        elif op == "press":
            r = press(serial, arg)
        elif op == "type":
            r = type_text(serial, arg)
        elif op == "wait":
            r = wait_for(serial, arg)
        elif op == "read":
            r = read_value(serial)
        elif op == "tap":
            x, y = arg.split()
            r = A.bridge_cmd(serial, f"TAP {x} {y}")
        else:
            r = {"ok": False, "err": f"未知步骤 {op}"}
        log.append({op: arg, "ok": bool(r.get("ok")),
                    "detail": {k: v for k, v in r.items() if k != "nodes"}})
        if not r.get("ok"):
            log.append({"stopped": f"「{op} {arg}」失败，不再往下点"})
            break
    return log


# ---------------------------------------------------------------- CLI

DEMO = [["open", "com.miui.calculator"],
        ["press", "清除"],
        ["press", "1"], ["press", "2"], ["press", "乘"],
        ["press", "3"], ["press", "4"], ["press", "等于"]]


def read_result(serial):
    """读计算器那行结果。

    结果不在输入框里：输入框只放表达式「12×34」，算出来的数是大字号
    TextView「= 408」。所以按前缀找那一行，而不是读 EditText。
    """
    scr = read_screen(serial)
    for label in scr["labels"]:
        if label.strip().startswith("="):
            return {"ok": True, "line": label.strip(), "foreground": scr["foreground"]}
    return {"ok": False, "err": "屏幕上没有「= …」这一行"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    d = A.attach()
    if not d["ok"]:
        print("设备接不上：", d.get("err"), file=sys.stderr)
        return 2
    serial = d["serial"]

    if not argv or argv[0] == "demo":
        log = run_steps(serial, DEMO)
        print(json.dumps(log, ensure_ascii=False, indent=2)[-1200:])
        v = read_result(serial)
        print(f"\n结果行：{v.get('line') or v.get('err')}")
        ok = "408" in (v.get("line") or "")
        print("核对：", "对（12×34=408）" if ok else "不对")
        return 0 if ok else 1

    op = argv[0]
    if op == "read":
        print(json.dumps(read_screen(serial), ensure_ascii=False,
                         default=str)[:2000])
    elif op == "open":
        print(open_app(serial, argv[1]))
    elif op == "press":
        print(press(serial, argv[1], dry_run="--dry" in argv))
    elif op == "type":
        print(type_text(serial, argv[1]))
    elif op == "value":
        print(read_value(serial))
    elif op == "wait":
        print(wait_for(serial, argv[1]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
