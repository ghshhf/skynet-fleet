"""t25：22 工具表（单一事实源）+ 应用名解析 + 渲染器抗畸形数据。

这一组钉的是「下放能力」这条路的三个真实失效点，每一个都是从实测里捞出来的：

  ① 单一事实源：工具名曾经散在 schema/描述/权限/执行器四处，加工具必漏一处，
     漏了不报错、只是静默不生效。现在四处都由 TOOLS 派生。
  ② 应用名解析：模型给的是「微信」，设备要的是 com.tencent.mm。
     差这一步，app_launch 就是个永远失败的摆设。
  ③ 渲染器抗畸形：bt.scan 在蓝牙关着时 devices 是 {"ok":False,"err":...}
     而不是数组——按数组切片会 KeyError 崩掉整个工具（实测崩过）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fleet.agent_tools as AT

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


print("t25 工具表（单一事实源）")

# ---------------------------------------------------------------- ① 派生一致
print("① 四处结构全部由 TOOLS 派生")
names = [t.name for t in AT.TOOLS]
check(len(names) == len(set(names)), "工具名不重复（重名会让 schema 静默丢项）")
check(len(names) >= 20, f"工具数已下放到 {len(names)} 个（原来 6 个）")
check(set(names) == set(AT.EXECUTORS), "EXECUTORS 与 TOOLS 同键")
check(set(names) == set(AT.DEFAULT_TIERS), "权限表与 TOOLS 同键")
enum = AT.DECIDE_SCHEMA["properties"]["tool"]["enum"]
check(set(enum) == set(names) | {"none"}, "决策 schema 的 enum 与 TOOLS 同键 + none")
check(all(t.name in AT._TOOLS_LINE for t in AT.TOOLS),
      "每个工具都进了给模型看的描述行（漏了 = 模型永远选不到它）")
check(all(t.render is not None for t in AT.TOOLS), "每个工具都有渲染器（没有 = 回填空白）")
check(all(t.perm in (AT.PERM_AUTO, AT.PERM_ASK, AT.PERM_FORBID) for t in AT.TOOLS),
      "每个工具的权限档位都合法")
check(all(t.cap is not None or t.name == "web_search" for t in AT.TOOLS),
      "每个工具都指向真实能力（web_search 走 webtool，是唯一例外）")

# 有副作用的必须不是 auto——模型说要跑不代表就该跑
risky = [t.name for t in AT.TOOLS if t.side and t.perm == AT.PERM_AUTO]
check(not risky, f"有副作用的工具没有一个是 auto 档（实测 {risky or '全部为 ask'}）")
check(AT.permission_for("app_launch") == AT.PERM_ASK, "app_launch 是 ask（要人点头）")
check(AT.permission_for("input_tap") == AT.PERM_ASK, "input_tap 是 ask（会真点下去）")

print("② 应用名 → 包名（模型说的是人话，设备要的是包名）")

PKGS = ["com.tencent.mm", "com.eg.android.AlipayGphone", "com.android.settings",
        "com.android.chrome", "com.miui.calculator", "com.termux"]


def fake_installed(cap, args):
    return {"ok": True, "code": "ok", "data": {"packages": list(PKGS),
                                               "count": len(PKGS)}}


AT._INSTALLED["pkgs"] = []
AT._INSTALLED["ts"] = 0.0

pkg, why = AT.resolve_app("微信", fake_installed)
check(pkg == "com.tencent.mm", f"「微信」→ com.tencent.mm（实得 {pkg}）")
pkg, _ = AT.resolve_app("支付宝", fake_installed)
check(pkg == "com.eg.android.AlipayGphone", f"「支付宝」→ 真包名（实得 {pkg}）")
pkg, _ = AT.resolve_app("com.android.chrome", fake_installed)
check(pkg == "com.android.chrome", "本来就是包名 → 原样透传（不去查表）")
pkg, _ = AT.resolve_app("wechat", fake_installed)
check(pkg == "com.tencent.mm", "英文名 wechat 也能认（模型有时输出英文）")

pkg, why = AT.resolve_app("抖音", fake_installed)
check(pkg is None and "没装" in (why or ""),
      "认识但没装 → 说清「没装在这台手机上」（不是静默拿错包名）")
pkg, why = AT.resolve_app("一个不存在的应用", fake_installed)
check(pkg is None and "没找到" in (why or ""),
      "完全不认识 → 说清没找到（不瞎猜一个包名去启动）")

# 别名表里的包名必须像包名（防止手滑写成中文）
bad = {k: v for k, v in AT.APP_ALIASES.items()
       if not all(x[:1].isalpha() and "." in x for x in v)}
check(not bad, f"别名表里的值都是包名形状（异常项 {list(bad)[:3]}）")

print("③ 渲染器抗畸形数据（实测崩过的地方）")

# bt.scan 蓝牙关着：devices 是 dict 不是 list
bt_off = {"ok": True, "code": "ok",
          "data": {"devices": {"ok": False, "err": "ERR bt-off"}, "count": 0}}
try:
    label, text, ok = AT.execute("bt_scan", "", lambda c, a: bt_off)
    ok_render = ("bt-off" in (text or "")) and not ok or True
    check("不可用" in (text or "") or "bt-off" in (text or ""),
          f"蓝牙关着时说清「不可用 + 原因」，而不是当成 0 个设备（实得：{text[:40]}）")
except Exception as e:
    check(False, f"蓝牙关着时渲染崩了：{type(e).__name__}: {e}")

# 空列表不该崩、也不该被当成失败
empty = {"ok": True, "code": "ok", "data": {"jobs": []}}
_, text, _ = AT.execute("job_list", "", lambda c, a: empty)
check("0 条" in (text or ""), f"空列表说「0 条」，不谎报失败（实得：{text[:30]}）")

# 完全没数据的能力：不许说「没有」，要说「读到了但没字段」
blank = {"ok": True, "code": "ok", "data": {}}
_, text, _ = AT.execute("node_status", "", lambda c, a: blank)
check("没有可报告" in (text or "") or "原始" in (text or ""),
      f"data 为空时明说「没有可报告的字段」（不编造内容，实得：{text[:30]}）")

# 失败要带原因
failed = {"ok": False, "code": "unavailable", "err": "adb 没连上"}
_, text, ok = AT.execute("device_status", "", lambda c, a: failed)
check((not ok) and "adb 没连上" in (text or ""), "能力失败 → 把原因带进资料块")

# 真机实测过的形状：pc_sysinfo 的字段是 os/cpu_count/mem_total/disk_free
sysinfo = {"ok": True, "code": "ok", "data": {
    "os": "Windows", "release": "11", "cpu_count": 12,
    "mem_total": 17000000000, "mem_percent": 80.6,
    "disk_free": 19000000000, "disk_total": 500000000000,
    "hostname": "PC"}}
_, text, _ = AT.execute("pc_sysinfo", "", lambda c, a: sysinfo)
check("12 核" in (text or "") and "GB" in (text or ""),
      f"sysinfo 读真字段并把字节转成 GB（实得：{text[:60]}）")

# wifi：aps 键（真机）；也要兼容 networks（老形状）
wifi_aps = {"ok": True, "code": "ok", "data": {
    "count": 12, "bands": {"2.4G": 7, "5G": 5},
    "aps": [{"ssid": "HOME", "level": -50}]}}
_, text, _ = AT.execute("wifi_survey", "", lambda c, a: wifi_aps)
check("12" in (text or "") and "HOME" in (text or ""),
      f"wifi 读 aps/count/bands（实得：{text[:40]}）")
wifi_old = {"ok": True, "code": "ok",
            "data": {"networks": [{"ssid": "OLD", "level": -70}]}}
_, text, _ = AT.execute("wifi_survey", "", lambda c, a: wifi_old)
check("OLD" in (text or ""), "wifi 也兼容老键 networks（换设备不至于失明）")

print("④ 决策层：schema 里没有的工具一律不许执行")
check(AT.permission_for("shell_exec") == AT.PERM_FORBID,
      "shell_exec 不在表里 → forbid（短信/联系人/shell 一律不进表）")
label, text, ok = AT.execute("shell_exec", "rm -rf /", lambda c, a: None)
check((not ok) and "forbid" in text, "未登记工具根本不进执行（caller 不会被调用）")

# 每条描述都要够长——1.7B 是靠描述选的，写太短它就靠猜
short = [t.name for t in AT.TOOLS if len(t.desc) < 12]
check(not short, f"没有短到没法判断的描述（{short or '全部 ≥12 字'}）")

print("t25 tool_catalog:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
