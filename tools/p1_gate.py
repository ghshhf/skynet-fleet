"""P1 门禁：元素级动作（TAPEL / SETTEXT / SCROLL）真机验收。

判据来自 docs/天网手机端_借力Operit_计划书_2026-10-22.md §六 P1：
  1. 元素寻址 + 点击：四种选择器（id / text / desc / bounds）都要点到正确界面
  2. 找不到必须明确报 not-found，绝不允许"点个差不多的地方"
  3. 匹配到多个必须报 ambiguous 并列出候选
  4. SETTEXT 设进去要能回读确认
  5. SCROLL 每次都要真的动（不是返回 OK 却没动）

一条不过就算整阶段不过。用法：
    python tmp/p1_gate.py [serial]
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import android_device as A  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<26} {detail}")
    return ok


def fg(serial):
    """当前前台 activity 的短名。"""
    txt = A.shell(serial, "dumpsys window | grep mCurrentFocus")[1]
    m = re.search(r"com\.\w[\w.]*/([\w.$]+)\}", txt)
    return m.group(1) if m else txt.strip()[:60]


def home_settings(serial):
    """强制回到设置首页。

    只用 `am start -a android.settings.SETTINGS` 不够：当设置本来就在前台
    （比如停在「无线调试」子页）时，它只把任务提到前台，不退出子页，
    结果门禁在子页上找「WLAN」当然找不到，看着像功能坏了。
    force-stop 之后再启动，一定是首页。
    """
    A.shell(serial, "am force-stop com.android.settings")
    time.sleep(0.4)
    A.shell(serial, "am start -a android.settings.SETTINGS")
    time.sleep(1.6)


def ensure_settings(serial, tries=3):
    """确保前台真的是设置。

    门禁里每条用例都必须在同一个 App 里跑，否则测的是别的东西
    （实测出现过 back 把设置整个退出、SCROLL 结果滚到了微信聊天页）。
    """
    for _ in range(tries):
        if "settings" in fg(serial).lower():
            return True
        home_settings(serial)
    return "settings" in fg(serial).lower()


def ensure_search(serial, tries=2):
    """确保前台是设置的搜索页（有输入框）。

    搜索页和目标页 activity 同名，认 activity 没用，只认输入框。
    """
    def has_edit():
        sc = A.bridge_screen(serial, 300)
        return any(n["cls"] == "EditText" for n in sc["nodes"])

    for _ in range(tries):
        if has_edit():
            return True
        if ensure_settings(serial):
            A.bridge_tap_el(serial, "desc", "搜索")
            time.sleep(1.6)
    return has_edit()


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else ""
    if not serial:
        d = A.attach()
        if not d["ok"]:
            print("设备接不上：", d.get("err"))
            return 2
        serial = d["serial"]
    print(f"设备：{serial}\n")

    p = A.bridge_cmd(serial, "PING")
    if not check("桥在线", p["ok"], p.get("out", p.get("err", ""))):
        return 2

    home_settings(serial)

    # ---- 1. 找不到：必须明确拒绝，且附上界面上真实存在的候选 ----------------
    r = A.bridge_tap_el(serial, "text", "绝不存在的按钮ZQX")
    ok = (not r["ok"]) and "not-found" in r["err"] and "候选" in r["err"]
    check("找不到会报 not-found", ok, (r.get("err") or "")[:110])

    # ---- 2. 歧义：不许自己挑一个，要把候选列出来 -----------------------------
    r = A.bridge_tap_el(serial, "id", "title")
    m = re.search(r"ambiguous (\d+)", r.get("err", ""))
    ok = (not r["ok"]) and bool(m) and "#1" in r.get("err", "")
    check("多匹配会报 ambiguous", ok, (r.get("err") or "")[:110])
    n_amb = int(m.group(1)) if m else 0

    # ---- 3. 序号越界：要说清楚只有几个，不是乱点 -----------------------------
    r = A.bridge_tap_el(serial, "id", "title", index=n_amb + 50)
    ok = (not r["ok"]) and "bad-index" in r.get("err", "")
    check("序号越界会报 bad-index", ok, (r.get("err") or "")[:110])

    # ---- 4. text 选择器：点 WLAN 要真的进到无线设置页 -----------------------
    before = fg(serial)
    r = A.bridge_tap_el(serial, "text", "WLAN")
    time.sleep(1.2)
    after = fg(serial)
    ok = r["ok"] and after != before and "Wifi" in after
    check("text 选择器点进 WLAN", ok, f"{before} -> {after}")

    # ---- 5. id 选择器（短名后缀匹配）：点返回按钮回到设置首页 ----------------
    # 判据必须正向认「回到设置首页」。早先写成"不含 Wifi 就算过"，
    # 结果人切到微信它也判 PASS——否命题不是判据。
    r = A.bridge_tap_el(serial, "id", "up")
    time.sleep(1.2)
    after = fg(serial)
    ok = r["ok"] and "settings" in after.lower() and "wifi" not in after.lower()
    check("id 短名匹配点返回", ok, f"回到 {after}")

    # ---- 6. desc 选择器：点"搜索"进搜索页 -----------------------------------
    # 注意：设置搜索页和目标页共用同一个 activity 名（MiuiSettings），
    # 所以判据不能看 activity，要看"搜索页才有的东西"——EditText 或取消按钮。
    if not ensure_settings(serial):
        check("desc 选择器点进搜索", False, f"前置失败：不在设置里（{fg(serial)}）")
    else:
        r = A.bridge_tap_el(serial, "desc", "搜索")
        time.sleep(1.6)
        sc = A.bridge_screen(serial, 300)
        has_edit = any(n["cls"] == "EditText" or "search" in n["id"].lower()
                       for n in sc["nodes"])
        check("desc 选择器点进搜索", r["ok"] and has_edit,
              f"点中 {r.get('hit', r.get('err', ''))[:50]}；搜索页特征={has_edit}")

    # ---- 7. SETTEXT：设进去要能回读 ----------------------------------------
    if not ensure_search(serial):
        check("SETTEXT 写入并回读", False, f"前置失败：不在搜索页（{fg(serial)}）")
    else:
        r = A.bridge_set_text(serial, "id", "android:id/input", "WLAN 设置")
        time.sleep(0.8)
        sc = A.bridge_screen(serial, 300)
        got = next((n["text"] for n in sc["nodes"] if n["cls"] == "EditText"), "")
        ok = r["ok"] and got == "WLAN 设置"
        check("SETTEXT 写入并回读", ok, f"回读={got!r}")

    # ---- 8. bounds 选择器：回到首页，用 WLAN 那一行的 bounds 再点一次 --------
    # 搜索页和目标页 activity 同名，am start 不一定能退出搜索页，
    # 所以以「能读到 WLAN 那一行」为准，读不到就 BACK 再试。
    wlan = None
    for i in range(3):
        home_settings(serial)
        sc = A.bridge_screen(serial, 300)
        wlan = next((n for n in sc["nodes"] if n["text"] == "WLAN"), None)
        if wlan:
            break
        A.bridge_cmd(serial, "BACK")
        time.sleep(1.0)
    if not wlan:
        check("bounds 选择器", False, "界面上没找到 WLAN 行，无法取 bounds")
    else:
        b = wlan["bounds"]
        sel = f"[{b[0]},{b[1]}][{b[2]},{b[3]}]"
        r = A.bridge_tap_el(serial, "bounds", sel)
        time.sleep(1.2)
        after = fg(serial)
        ok = r["ok"] and "Wifi" in after
        check("bounds 选择器点 WLAN 行", ok, f"sel={sel} -> {after}")
        if "Wifi" in after:
            A.bridge_cmd(serial, "BACK")
            time.sleep(1.0)

    # ---- 9. SCROLL：每次都要真的动 -----------------------------------------
    # 先钉死"确实在设置里"：否则滚的是别的 App，测了等于没测。
    if not check("SCROLL 前置：在设置里", ensure_settings(serial), fg(serial)):
        return 1

    # 判据不写死具体文字（机型/系统版本一变就废），改成「回到起点」：
    # 起点就是本轮滚动之前列表里 y 最小的那个 title，滚回来必须一致。
    # 「搜索系统设置项」在固定头部（header_view）不随列表滚动，不能拿它当顶。
    def top_title(nodes):
        ts = [n for n in nodes if n["id"].endswith("/title") and n["text"]]
        return min(ts, key=lambda n: n["bounds"][1])["text"] if ts else ""

    want = top_title(A.bridge_screen(serial, 400)["nodes"])
    states = []
    for _ in range(3):
        sc = A.bridge_screen(serial, 400)
        states.append([n["text"] or n["id"] for n in sc["nodes"]])
        A.bridge_scroll(serial, "down")
        time.sleep(0.9)
    sc = A.bridge_screen(serial, 400)
    states.append([n["text"] or n["id"] for n in sc["nodes"]])
    moved = sum(1 for i in range(3) if states[i] != states[i + 1])
    ok = moved >= 2
    check("SCROLL down 真的动了", ok, f"3 次里 {moved} 次界面变化")

    for _ in range(5):
        A.bridge_scroll(serial, "up")
        time.sleep(0.7)
    got = top_title(A.bridge_screen(serial, 400)["nodes"])
    ok = bool(want) and want == got
    check("SCROLL up 能滚回顶部", ok, f"起点首项 {want!r} -> 回滚后 {got!r}")

    # ---- 汇总 --------------------------------------------------------------
    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    print(f"P1 门禁：{len(RESULTS) - len(bad)}/{len(RESULTS)} 项通过")
    if bad:
        print("未通过：" + "、".join(r[0] for r in bad))
        return 1
    print("P1 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
