# RFC：通用规则引擎（Event → Condition → Action）

> 分支 `rfc/rule-engine` · 横向机制，与主线「agent 里写 if/else」不同的发散方向
> 状态：提案 + 最小实现

## 0. 一句话

把「判断该干什么」从**代码里的 if/else** 抽成**数据里的规则**。规则是 JSON，引擎统一 evaluate；改行为不改代码，只改规则。这正好规避用户明确反感的反模式——「agent 里一写范围/逻辑就出 bug」。

## 1. 为什么需要

主线有一堆散落的判断：
- 跳广告：节点文本匹配 → 点击（`ui_automation`）
- 算力调度：GPU 空闲 + 任务重 → 路由到 GPU 节点
- 她的主动性：静默时段内 + 有新事件 + 未超频 → 发一条消息
- 监控：节点掉线 > 30s → 告警 + 重试

每处都各自写 `if` → 逻辑分散、难改、易漏。统一成一个引擎后：**这些判断全都是规则 JSON**，可热更新、可审计、可回滚。

## 2. 模型

```json
{
  "name": "夜间静默",
  "when": {
    "event": "tick",
    "match": {
      "hour":      {"op": "gte", "value": 23},
      "her_msg_n": {"op": "lt",  "value": 1}
    },
    "all": true          // true=全部条件满足(AND), false=任一(OR)
  },
  "then": [
    {"action": "persona.silence", "args": {"until": "07:00"}}
  ],
  "priority": 10
}
```

- **event**：触发类型（`tick` / `ui_node` / `node_offline` / `compute_job` …）
- **match**：字段 → 操作符 + 值。操作符：`eq ne contains regex gt gte lt lte in`
- **then**：动作列表（引擎只产出动作描述，由调用方执行 → 引擎不绑业务）
- **priority**：多条命中时排序

## 3. 设计原则

1. **引擎不执行副作用**：只把「该做什么」算出来，执行交给能力层。这样规则可单测、可 dry-run。
2. **数据驱动**：规则存 L3 / JSON 文件，热加载；不重编译。
3. **可审计**：每次 evaluate 记录命中哪条、为什么（哪个字段哪个 op），与主线 `audit.py` 对齐。
4. **无状态**：引擎纯函数 `evaluate(rules, event) -> [actions]`，好测。

## 4. 最小实现

`rule_engine.py`：
- `evaluate(rules, event)` — 返回 `[(rule_name, action), ...]`
- `_match_one(value, cond)` — 单条件
- `load(path)` / `dump(rules, path)`
- 自带 `--selftest`：三条规则（夜间静默 / GPU 路由 / 掉线告警）用假事件验证。

## 5. 风险

- 规则过复杂 → 变成「另一种代码」。约束：规则只做**匹配 + 选动作**，不含循环/嵌套执行。
- 规则冲突 → priority + 审计日志解决。
- 安全 → 规则来自 L3 时须签名/校验来源，防止被注入恶意动作。

## 6. 与主线关系

这是**横向能力**，被多条线复用（ui_automation / 算力调度 / 她 / 监控）。它不替代 agent，而是把 agent 里「条件判断」这部分**外置成数据**，减少你反感的运行期分支 bug。
