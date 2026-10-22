"""证据播种：用**真实能力调用**产生 exec: 级证据。

为什么需要这个（2026-10-22 实测）：
    证据层门禁通过时，证据库里 exec 级证据还是 **0 条** —— 全部 7543 条都是
    从记忆导入的 mem 级。mem 级只能证明「当时记下了这么一件事」，
    证明不了「某个能力真的跑过」。
    ⇒ 技能验证门要的那条强证据，必须靠**真的调一次**来产生。

这个脚本只跑**只读**能力（本机 38 个只读里挑），不碰任何有副作用的能力。
实测可跑的有 31 个（adb.wifi.*、pcctl.*、host.*、net.*、card.*、ai.* …）。

一个 trace = 一次巡检 = 连续多次调用：
    这正好是「一张技能的原型的证据链」——
    技能描述的步骤顺序 = 这条 trace 里能力被调用的顺序。
    验证门要核的「caps 在源事件里真出现过（Pass ∧ Use）」就有东西可核了。

诚实边界：
    这只证明「这些能力在这台机器上按这个顺序跑通了」，
    不证明「在任何机器上都成立」。后者靠多节点统计（P4）才发现得了。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import journal as J                                                  # noqa: E402
from fleet import catalog                                           # noqa: E402
from fleet import execute as X                                       # noqa: E402

EVIDENCE_DB = os.path.join(ROOT, "fleet_evidence.db")

# 默认巡检组：全是只读、不花钱、不截图、不联网
DEFAULT_CAPS = [
    "pcctl.sysinfo",
    "pcctl.proc.list",
    "pc.state",
    "host.apps",
    "card.lint",
    "ai.status",
]


def seed(caps=None, label="巡检", db=EVIDENCE_DB, dry=False):
    """跑一遍只读能力，返回 (结果列表, trace)。"""
    caps = list(caps or DEFAULT_CAPS)
    reg = catalog.build(with_nodes=True, ttl=2.0)
    j = J.Journal(db, cap=200000)
    ex = X.Executor(reg, journal=j, actor="evidence-seed")
    trace = "%s%s" % (X.EXEC_PREFIX, label)
    out = []
    try:
        for cap in caps:
            if dry:
                out.append((cap, "dry-run"))
                continue
            r = ex.call(cap, trace=trace)
            out.append((cap, "ok" if r.ok else "fail:%s" % r.code))
    finally:
        j.close_connection()
    return out, trace


def _main(argv):
    args = list(argv)
    caps = None
    label = "巡检"
    dry = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--caps":
            i += 1
            caps = [c.strip() for c in args[i].split(",") if c.strip()]
        elif a == "--label":
            i += 1
            label = args[i]
        elif a == "--dry":
            dry = True
        elif a in ("-h", "--help"):
            print("用法：python tools/evidence_seed.py [--caps a,b] [--label 名] [--dry]")
            return 0
        i += 1

    out, trace = seed(caps, label, dry=dry)
    print("trace = %s" % trace)
    for cap, st in out:
        print("  %-24s %s" % (cap, st))
    ok = sum(1 for _c, s in out if s == "ok")
    print("\n成功 %d / %d" % (ok, len(out)))
    if not dry:
        print("证据已写入 %s" % EVIDENCE_DB)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
