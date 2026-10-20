"""通用规则引擎 · 最小实现（分支 rfc/rule-engine）。

Event -> Condition -> Action。引擎只产出「该做什么」，不执行副作用（可单测/可 dry-run）。
纯 stdlib。
"""
import json
import re


def _match_one(value, cond):
    """单条件判断：cond = {"op":..., "value":...}"""
    op = cond.get("op", "eq")
    want = cond.get("value")
    try:
        if op == "eq":
            return value == want
        if op == "ne":
            return value != want
        if op == "contains":
            return want in (value or "")
        if op == "regex":
            return bool(re.search(str(want), str(value or "")))
        if op == "gt":
            return value > want
        if op == "gte":
            return value >= want
        if op == "lt":
            return value < want
        if op == "lte":
            return value <= want
        if op == "in":
            return value in want
    except TypeError:
        return False
    return False


def _match_event(when, event):
    if when.get("event") and when["event"] != event.get("event"):
        return False
    match = when.get("match") or {}
    results = [_match_one(event.get(k), cond) for k, cond in match.items()]
    if not results:
        return True
    return all(results) if when.get("all", True) else any(results)


def evaluate(rules, event):
    """返回 [(rule_name, action_dict), ...]，按 priority 降序。"""
    hits = []
    for r in rules:
        when = r.get("when") or {}
        if _match_event(when, event):
            for a in r.get("then", []):
                hits.append((r.get("priority", 0), r.get("name", "?"), a))
    hits.sort(key=lambda x: -x[0])
    return [(name, action) for _, name, action in hits]


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump(rules, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


def _selftest():
    rules = [
        {"name": "夜间静默", "priority": 10,
         "when": {"event": "tick", "match": {"hour": {"op": "gte", "value": 23}}},
         "then": [{"action": "persona.silence", "args": {"until": "07:00"}}]},
        {"name": "GPU路由", "priority": 5,
         "when": {"event": "compute_job",
                  "match": {"gpu_free": {"op": "gt", "value": 0.3},
                            "task": {"op": "contains", "value": "训练"}}},
         "then": [{"action": "compute.route", "args": {"target": "gpu"}}]},
        {"name": "掉线告警", "priority": 8,
         "when": {"event": "node_offline",
                  "match": {"seconds": {"op": "gt", "value": 30}}},
         "then": [{"action": "alert.send", "args": {"level": "warn"}},
                  {"action": "queue.enqueue", "args": {"retry": True}}]},
    ]
    # 1) 夜间 tick 命中
    a = evaluate(rules, {"event": "tick", "hour": 23})
    assert a and a[0][0] == "夜间静默" and a[0][1]["action"] == "persona.silence", a
    # 2) 白天 tick 不命中
    assert evaluate(rules, {"event": "tick", "hour": 12}) == [], evaluate(rules, {"event": "tick", "hour": 12})
    # 3) GPU 训练任务命中
    b = evaluate(rules, {"event": "compute_job", "gpu_free": 0.8, "task": "在GPU上训练LoRA"})
    assert b and b[0][1]["action"] == "compute.route", b
    # 4) GPU 不空闲不命中
    assert evaluate(rules, {"event": "compute_job", "gpu_free": 0.1, "task": "训练"}) == []
    # 5) 掉线 >30s 两个动作都有，按 priority 排序
    c = evaluate(rules, {"event": "node_offline", "seconds": 45})
    actions = [x[1]["action"] for x in c]
    assert actions == ["alert.send", "queue.enqueue"], actions
    # 6) 掉线 10s 不命中
    assert evaluate(rules, {"event": "node_offline", "seconds": 10}) == []
    print("[selftest] 夜间静默命中/白天不命中 OK")
    print("[selftest] GPU路由(contains+gt) OK")
    print("[selftest] 掉线告警多动作+priority排序 OK")
    print("[selftest] PASS")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        print("用法: python rule_engine.py --selftest")
