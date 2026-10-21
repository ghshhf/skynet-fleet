"""统一意图编排（L2 本命位）：一个意图 -> 多步计划 -> 带依赖执行 -> 失败回滚。

与 broadcast（命令扇出）的区别：
- 扇出 = 一条命令发多台；
- 编排 = 一个意图拆成多步、步与步有依赖（after），某步失败则把**已完成的步**按逆序
  回滚（rollback）——这才叫「一个系统在动」，否则只是一堆并行命令。

步 = {"id","desc","cap","node","args","after":[...],"rollback":{同结构，可省}}
接口与「能力自描述」对齐：步声明 cap，节点由「谁能干」选出（pick/nodes 回调）。

【容量闸接入】（学 DeepSeek Harness）
步可以额外带 "role"（这个步由哪个角色执行）与 "depth"（该角色处在第几层，
根编排者=0，它直接创建的子角色=1）。带 role 的步在执行前必须**从共享容量池预留名额**：
  - 容量满 ⇒ **立刻 fail-fast 拒绝整份计划**，绝不排队
    （排队会让「等待后代的父代理」去等「自己占着的名额」，互相死等）；
  - 名额在**这一步真正结束**后才归还（收到首字节不算结束）。
详见 fleet/agents/capacity.py。
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


def _acquire(step, gate):
    """为带 role 的步预留一个容量名额。返回 (lease, err)。

    没有 role 的步**不占名额**（根编排者自己的普通命令步）。
    声明了 role 却没有 gate ⇒ 报错，**不静默放过**（那会让容量闸形同虚设）。
    """
    role = step.get("role")
    if not role:
        return None, None
    if gate is None:
        return None, f"步「{step.get('id')}」声明了 role={role}，但没给容量闸（CapacityGate）"
    try:
        lease = gate.reserve(role, depth=int(step.get("depth", 1)),
                             provider=step.get("provider", ""))
    except Exception as e:      # CapacityError 等
        return None, str(e)
    return lease, None


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


def run_plan(plan, exec_fn, log=None, gate=None):
    """带依赖执行计划；任一步失败即回滚已完成的步。返回执行摘要。

    gate 是可选的 CapacityGate：只有声明了 "role" 的步会走容量闸。
    容量满 ⇒ 立刻失败（fail-fast），并回滚已完成的步——**不排队**。
    """
    log = log or (lambda *a: None)
    done = []
    for s in _topo(plan):
        lease, aerr = _acquire(s, gate)
        if aerr:
            # 容量/深度闸拒绝：不是「等一等」，是**现在就不行**。
            log("reject", s, {"ok": False, "err": aerr})
            rolled = _rollback(done, exec_fn, log)
            return {"ok": False, "failed": s["id"], "done": [x["id"] for x in done],
                    "rolled_back": rolled, "err": aerr, "rejected_by": "capacity"}
        try:
            r = _invoke(s, exec_fn)
        finally:
            # 名额在**这一步真正结束**后才归还（不是收到首字节）。
            if lease is not None:
                lease.release()
        if r.get("ok"):
            log("ok", s, r)
            done.append(s)
            continue
        log("fail", s, r)
        rolled = _rollback(done, exec_fn, log)
        return {"ok": False, "failed": s["id"], "done": [x["id"] for x in done],
                "rolled_back": rolled, "err": r.get("err")}
    return {"ok": True, "done": [x["id"] for x in done], "rolled_back": []}
