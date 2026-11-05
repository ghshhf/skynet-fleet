"""契约校验：catalog 与设备之间那四件事是否对得上。

**这一项替代几十项按 import 闭包拉起来的白跑。**
2026-11-06 实测：往 catalog.py 追加 162 行能力定义，`--changed` 拉起 43 项、193 秒，
那 43 项里没有一项的断言跟新增内容有关 —— 它们只是 import 了 catalog。
「被 import」不等于「受影响」，把它当影响面是过度防御。

真正会被这类改动弄坏的只有四件事，一项就能全查：
  ① catalog 起不来（import 崩、重名、构造器炸）
  ② 名字不合 domain.verb，上层拼不出来
  ③ 设备 caps() 返回了 Capability 对象而不是字符串（真踩过：登记了却调不通）
  ④ 设备实现了、caps() 也声明了，但 catalog 没登记（实现了却没人看得见）

第 ④ 条抓到的真 bug 就在本文件写下之前：`web:net` 的 web.quote 一直孤悬。

规矩：只读、不联网、不碰真机。跑一次 2 秒以内，所以它配得上每次都跑。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fleet.catalog as CAT

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


print("契约校验：catalog × 设备")

# ① 起得来
try:
    reg = CAT.build()
except Exception as e:
    print("  [X]  catalog.build() 直接崩：%s: %s" % (type(e).__name__, e))
    print("契约校验 FAIL (1 项)")
    sys.exit(1)
caps = reg.capabilities()
devs = reg.devices()
names = [c.name for c in caps]
check(len(names) == len(set(names)), "能力名唯一（%d 项）" % len(names))
check(len(devs) > 0, "设备表非空（%d 台）" % len(devs))

# ② 名字形状：至少两段，全小写，段内只有字母数字下划线
PAT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
bad_name = [n for n in names if not PAT.match(n)]
check(not bad_name, "名字都是 domain.verb 形状（越界：%s）" % (bad_name[:4] or "无"))

# 字段自洽
bad_ap = [c.name for c in caps if c.approval not in ("auto", "ask", "deny")]
check(not bad_ap, "approval 只在 auto/ask/deny 里（越界：%s）" % (bad_ap[:4] or "无"))
contra = [c.name for c in caps if c.destructive and not c.side_effect]
check(not contra, "没有「不可逆却没副作用」的矛盾登记（%s）" % (contra[:4] or "无"))
bad_params = [c.name for c in caps if not isinstance(c.params, tuple)]
check(not bad_params, "params 全是元组（不是元组的：%s）" % (bad_params[:4] or "无"))

# ③ + ④ 逐台设备核caps()
as_obj, ghost, broken, dotted = [], [], [], 0
for d in devs:
    try:
        cs = list(d.caps() or ())
    except Exception as e:
        broken.append("%s（caps() 崩：%s）" % (d.name, type(e).__name__))
        continue
    for c in cs:
        if not isinstance(c, str):
            # 历史坑：返回 Capability 对象 ⇒ 注册表按字符串索引找不到 ⇒ 调不通
            as_obj.append("%s → %r" % (d.name, type(c).__name__))
            continue
        if "." not in c:
            # 不带点的多半是节点协议动词（exec/push/pull），不属能力命名空间
            continue
        dotted += 1
        if reg.capability(c) is None:
            ghost.append("%s → %s" % (d.name, c))
check(not broken, "每台设备 caps() 都能调（坏的：%s）" % (broken[:3] or "无"))
check(not as_obj, "caps() 返回字符串而不是 Capability 对象（坏的：%s）" % (as_obj[:3] or "无"))
check(not ghost,
      "设备声明的带点能力都已登记（%d 条声明里漂着的：%s）" % (dotted, ghost[:6] or "无"))

# 反向只报数：catalog 有、暂时没有设备认领 —— 这是「声明待实现」，允许存在。
# 不判失败：本轮之前就有 9 条是这种，先声明后实现是正常节奏，不是缺陷。
def _safe_caps(d):
    try:
        return d.caps() or ()
    except Exception:
        return ()


owned = set()
for _d in devs:
    owned.update(s for s in _safe_caps(_d) if isinstance(s, str))
unowned = [c.name for c in caps if c.name not in owned]
print("  [--] 已声明但暂无设备认领：%d 项%s" % (
    len(unowned), "（%s…）" % "、".join(unowned[:3]) if unowned else ""))


print("契约校验:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
