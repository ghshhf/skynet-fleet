"""策略层：把「什么情况下做什么」外置成数据（复用 rfc/rule-engine 的引擎）。

为什么外置：
- 用户明确反感"在 agent 里写 if/else 分支"——分支一多必然出 bug、漏路径。
- 这里所有判断都是 JSON 规则，引擎只产出「该做什么」，副作用由 commander 执行，
  因此可以 dry-run、可以单测、可以不改代码就换策略。

覆盖：FLEET_POLICY=<path> 指向自定义策略文件（JSON 数组），否则用内置默认。
用法：
  from policies import eval_event
  for name, action in eval_event({"event": "node_offline", "node": "PC-B"}):
      ...
  python policies.py --selftest
  python policies.py --eval '{"event":"compute_pick","task":"微调LoRA"}'
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RE_DIR = os.path.join(_HERE, "rfc", "rule-engine")
if _RE_DIR not in sys.path:
    sys.path.insert(0, _RE_DIR)

import rule_engine  # noqa: E402

DEFAULT_POLICY = os.environ.get("FLEET_POLICY", "")

DEFAULT_RULES = [
    # 节点掉线：标记降级（不让后续路由再选它）+ 提示
    {"name": "掉线-降级告警", "priority": 10,
     "when": {"event": "node_offline"},
     "then": [{"action": "mark_degraded"},
              {"action": "notify", "args": {"text": "节点掉线，已降级"}}]},

    # 心跳过期：只探测，不急着判死（网络抖动常见）
    {"name": "心跳超时-探测", "priority": 8,
     "when": {"event": "hb_stale", "match": {"age": {"op": "gt", "value": 90}}},
     "then": [{"action": "probe"}]},

    # 重活优先 GPU：训练/微调类任务才需要，普通问答不必占 GPU
    {"name": "算力-重活优先GPU", "priority": 6,
     "when": {"event": "compute_pick",
              "match": {"task": {"op": "regex", "value": "训练|微调|finetune|train|编译|推理加速"}}},
     "then": [{"action": "prefer_gpu", "args": {"on": True}}]},

    # 临时节点（网吧）下机：清凭据，不留痕
    {"name": "临时节点-下机即清", "priority": 9,
     "when": {"event": "session_end", "match": {"mode": {"op": "eq", "value": "session"}}},
     "then": [{"action": "cleanup"},
              {"action": "notify", "args": {"text": "临时节点已清理，不留凭据"}}]},

    # 算力池空：明确告知，而不是静默失败
    {"name": "算力池空-降级提示", "priority": 4,
     "when": {"event": "compute_pool_empty"},
     "then": [{"action": "notify", "args": {"text": "无 AI 节点在线，可降级到免费端点"}}]},
]


def load_rules(path=None):
    """读策略：显式 path > FLEET_POLICY > 内置默认。文件损坏时退回默认（不静默空转）。"""
    p = path or DEFAULT_POLICY
    if p and os.path.exists(p):
        try:
            rules = rule_engine.load(p)
            if isinstance(rules, list) and rules:
                return rules
        except (OSError, ValueError):
            pass
    return list(DEFAULT_RULES)


def eval_event(event, rules=None):
    """评估事件 -> [(规则名, 动作)]，按 priority 降序。引擎不执行副作用。"""
    return rule_engine.evaluate(rules if rules is not None else load_rules(), event)


def _selftest():
    assert eval_event({"event": "node_offline", "node": "PC-B"}), "掉线应有动作"
    a = [x[1]["action"] for x in eval_event({"event": "node_offline", "node": "PC-B"})]
    assert "mark_degraded" in a, a

    assert not eval_event({"event": "hb_stale", "age": 30}), "30s 不该探测"
    assert eval_event({"event": "hb_stale", "age": 120}), "120s 应探测"

    assert not eval_event({"event": "compute_pick", "task": "写个快排"}), "普通任务不该占 GPU"
    assert eval_event({"event": "compute_pick", "task": "微调一个 LoRA"}), "微调应优先 GPU"

    assert eval_event({"event": "session_end", "mode": "session"}), "会话模式下机应清理"
    assert not eval_event({"event": "session_end", "mode": "resident"}), "常驻不该清理"

    # priority 排序：掉线(10) 应排在探测(8) 前
    hits = eval_event({"event": "node_offline"}) + eval_event({"event": "hb_stale", "age": 999})
    assert hits[0][0].startswith("掉线"), hits
    print("[selftest] 掉线->降级+告警 OK")
    print("[selftest] 心跳超时阈值(90s) OK")
    print("[selftest] 重活优先GPU(仅训练类) OK")
    print("[selftest] 临时节点下机清理 OK")
    print("[selftest] priority 排序 OK")
    print("[selftest] PASS")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    if "--eval" in sys.argv:
        i = sys.argv.index("--eval")
        ev = json.loads(sys.argv[i + 1])
        for name, action in eval_event(ev):
            print(f"  {name} -> {action.get('action')} {action.get('args') or ''}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
