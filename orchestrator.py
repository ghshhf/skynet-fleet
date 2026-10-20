"""统一意图编排（L2 本命位）：一个意图 -> 多步计划 -> 带依赖执行 -> 失败回滚。

与 broadcast（命令扇出）的区别：
- 扇出 = 一条命令发多台；
- 编排 = 一个意图拆成多步、步与步有依赖（after），某步失败则把**已完成的步**按逆序
  回滚（rollback）——这才叫「一个系统在动」，否则只是一堆并行命令。

步 = {"id","desc","cap","node","args","after":[...],"rollback":{同结构，可省}}
接口与「能力自描述」对齐：步声明 cap，节点由「谁能干」选出（pick/nodes 回调）。
"""
import re


def parse_intent(text, nodes=None, pick=None):
    """把一句话意图解析成计划（规则式；后续可换成 LLM 解析，对外接口不变）。"""
    nodes = [n for n in (nodes or []) if n]
    t = (text or "").strip()
    if not t:
        return []

    if any(k in t for k in ("巡检", "体检", "盘点", "检查一遍")):
        plan = []
        for i, n in enumerate(nodes):
            plan.append({"id": f"probe-{i}", "desc": f"读取 {n} 主机名",
                         "cap": "exec", "node": n, "args": {"cmd": "hostname"}})
        ids = [s["id"] for s in plan]
        plan.append({"id": "summary", "desc": "汇总巡检结果", "cap": "exec",
                     "node": nodes[0] if nodes else None, "args": {"cmd": "echo 巡检完成"},
                     "after": ids})
        return plan

    if "备份" in t:
        src = pick("exec") if pick else (nodes[0] if nodes else None)
        return [
            {"id": "snap", "desc": "列出待备份内容", "cap": "exec", "node": src,
             "args": {"cmd": "dir"}},
            {"id": "pack", "desc": "打包（示意）", "cap": "exec", "node": src,
             "args": {"cmd": "echo 打包完成"}, "after": ["snap"],
             "rollback": {"cap": "exec", "node": src, "args": {"cmd": "echo 回滚:删除临时包"}}},
        ]

    m = re.match(r"^对\s*(\S+)\s*执行\s*(.+)$", t)
    if m:
        return [{"id": "s1", "desc": f"在 {m.group(1)} 执行", "cap": "exec",
                 "node": m.group(1), "args": {"cmd": m.group(2)}}]
    return []


def _topo(plan):
    """按 after 依赖做拓扑排序（保证先跑被依赖的步）；检测环。"""
    steps = {s["id"]: s for s in plan}
    order, state = [], {}

    def visit(sid):
        st = state.get(sid)
        if st == "done":
            return
        if st == "visiting":
            raise ValueError(f"计划存在环: {sid}")
        state[sid] = "visiting"
        for dep in steps[sid].get("after", []):
            if dep in steps:
                visit(dep)
        state[sid] = "done"
        order.append(sid)

    for sid in steps:
        visit(sid)
    return [steps[s] for s in order]


def _invoke(step, exec_fn):
    node = step.get("node")
    if not node:
        return {"ok": False, "err": "该步未指定节点（编队里没人能干）"}
    if step.get("cap") != "exec":
        return {"ok": False, "err": f"暂不支持的步类型: {step.get('cap')}"}
    r = exec_fn(node, step.get("args", {}).get("cmd", ""))
    ok = bool(r.get("ok")) and r.get("rc") == 0
    return {"ok": ok, "out": r.get("out", ""),
            "err": r.get("err") or f"rc={r.get('rc')}"}


def _rollback(done, exec_fn, log):
    """把已完成的步按逆序执行其 rollback（补偿）。返回已回滚的步 id。"""
    rolled = []
    for s in reversed(done):
        rb = s.get("rollback")
        if not rb:
            continue
        r = _invoke(rb, exec_fn)
        log("rollback", s, r)
        rolled.append(s["id"])
    return rolled


def run_plan(plan, exec_fn, log=None):
    """带依赖执行计划；任一步失败即回滚已完成的步。返回执行摘要。"""
    log = log or (lambda *a: None)
    done = []
    for s in _topo(plan):
        r = _invoke(s, exec_fn)
        if r.get("ok"):
            log("ok", s, r)
            done.append(s)
            continue
        log("fail", s, r)
        rolled = _rollback(done, exec_fn, log)
        return {"ok": False, "failed": s["id"], "done": [x["id"] for x in done],
                "rolled_back": rolled, "err": r.get("err")}
    return {"ok": True, "done": [x["id"] for x in done], "rolled_back": []}
