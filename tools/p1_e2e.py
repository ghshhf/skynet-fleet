"""P1 端到端判据：把屏幕亮度调到 30%。

计划书 §六 P1 的判据原文是「"把亮度调到 30%" 端到端跑通」。
这里用**确定性脚本**跑（不经 LLM），验的是元素级能力本身够不够用；
LLM 驾驶的成功率另测，因为那多了一层模型质量噪声，两件事不能混为一谈。

判据（先说清，再跑）：
  1. 从桌面开始，点「设置」图标进设置           —— 元素寻址
  2. 在设置首页滚动找到「显示与亮度」并点进去   —— SCROLL + TAPEL
  3. 若自动亮度开着，关掉                       —— TAPEL 点 Switch
  4. 拖滑条到能产生「亮度 30%」的位置           —— SETSEEK
  5. `settings get system screen_brightness` 落在 64~90（0~255 的 25%~35%）

第 4 步为什么要 72 而不是 30：实测标定（2026-10-22，K80 Ultra / HyperOS）
  seekbar% → 系统亮度(0-255)：5→2  10→3  20→7  30→13  40→23  50→32
                              60→46  70→69  80→105  90→163  100→255
  这是伽马编码，滑条 30% 实际只有亮度 5%。亮度 30% 对应 seekbar 72~73。

用法：python tmp/p1_e2e.py [serial]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import android_device as A  # noqa: E402

TARGET_LO, TARGET_HI = 64, 90        # 亮度 25%~35%（0~255）
SEEKBAR_PCT = 72                     # 标定：≈ 亮度 30%


def brightness(serial):
    out = A.shell(serial, "settings get system screen_brightness")[1].strip()
    return int(out) if out.lstrip("-").isdigit() else -1


def auto_mode(serial):
    return A.shell(serial, "settings get system screen_brightness_mode")[1].strip()


def labels(sc):
    """当前界面上的可见文字（text 优先，其次 desc）。"""
    got = []
    for n in sc["nodes"]:
        t = (n["text"] or n["desc"]).strip()
        if t:
            got.append((t, n["cls"], n))
    return got


def find_text(serial, want, tries=1):
    """在当前界面找某段字，找不到返回 None（不猜、不模糊匹配）。"""
    for _ in range(tries):
        sc = A.bridge_screen(serial, 400)
        for t, _cls, n in labels(sc):
            if t == want:
                return n
        return None
    return None


def main():
    serial = sys.argv[1] if len(sys.argv) > 1 else ""
    if not serial:
        d = A.attach()
        if not d["ok"]:
            print("设备接不上：", d.get("err"))
            return 2
        serial = d["serial"]

    print(f"设备：{serial}")
    if not A.bridge_cmd(serial, "PING")["ok"]:
        print("桥不在线")
        return 2

    start = brightness(serial)
    print(f"起点：亮度 {start}（{start / 255 * 100:.0f}%），自动亮度 mode={auto_mode(serial)}\n")

    # ---- 1. 从桌面点「设置」 ------------------------------------------------
    A.shell(serial, "input keyevent KEYCODE_HOME")
    time.sleep(1.4)
    r = A.bridge_tap_el(serial, "text", "设置")
    if not r["ok"]:
        r = A.bridge_tap_el(serial, "desc", "设置")
    if not r["ok"]:
        print(f"FAIL 点桌面「设置」图标：{r.get('err', '')[:110]}")
        return 1
    time.sleep(1.8)
    fg = A.shell(serial, "dumpsys window | grep mCurrentFocus")[1]
    if "settings" not in fg.lower():
        print(f"FAIL 点「设置」后前台不是设置：{fg.strip()[:80]}")
        return 1
    print(f"PASS 桌面点「设置」        {r.get('hit', '')[:56]}")

    # ---- 2. 滚动找到「显示与亮度」并点进去 ----------------------------------
    hit, tried = None, 0
    for i in range(4):
        sc = A.bridge_screen(serial, 400)
        names = [t for t, _c, _n in labels(sc)]
        for cand in ("显示与亮度", "显示"):
            if cand in names:
                hit = cand
                break
        if hit:
            break
        A.bridge_scroll(serial, "down")
        tried += 1
        time.sleep(0.9)
    if not hit:
        print(f"FAIL 滚动 {tried} 次仍未出现「显示与亮度」，当前清单：{names[:12]}")
        return 1

    r = A.bridge_tap_el(serial, "text", hit)
    if not r["ok"]:
        print(f"FAIL 点「{hit}」：{r.get('err', '')[:110]}")
        return 1
    time.sleep(1.8)
    print(f"PASS 滚动 {tried} 次后点「{hit}」")

    # ---- 3. 关自动亮度（开着就会覆盖我们设的值） ----------------------------
    mode = auto_mode(serial)
    if mode == "1":
        sc = A.bridge_screen(serial, 400)
        sw = None
        for n in sc["nodes"]:
            if n["cls"] == "Switch" and "自动" in (n["desc"] or ""):
                sw = n
                break
        if sw is None:
            print("FAIL 找不到「自动调整亮度」开关")
            return 1
        b = sw["bounds"]
        r = A.bridge_tap_el(serial, "bounds", f"[{b[0]},{b[1]}][{b[2]},{b[3]}]")
        time.sleep(1.2)
        if auto_mode(serial) != "0":
            print(f"FAIL 点了开关但自动亮度仍 mode={auto_mode(serial)}")
            return 1
        print(f"PASS 关掉自动亮度          {sw['desc'][:30]}")
    else:
        print("PASS 自动亮度本来就是关的")

    # ---- 4. 滑条设到标定位置 ------------------------------------------------
    r = A.bridge_set_seek(serial, "id", "seekbar", SEEKBAR_PCT)
    if not r["ok"]:
        print(f"FAIL SETSEEK：{r.get('err', '')[:110]}")
        return 1
    time.sleep(1.0)
    got = brightness(serial)
    print(f"PASS SETSEEK {SEEKBAR_PCT}%      控件值={r.get('raw')} 系统亮度={got}")

    # ---- 5. 判据：亮度落在 25%~35% ------------------------------------------
    ok = TARGET_LO <= got <= TARGET_HI
    pct = got / 255 * 100
    print(f"{'PASS' if ok else 'FAIL'} 亮度落到 30%±5       {got}/255 = {pct:.0f}%"
          f"（要求 {TARGET_LO}~{TARGET_HI}）")
    if not ok:
        return 1
    print("\nP1 端到端：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
