---
name: clean-sample
description: 审查规则的**反向样本**：这个目录全是正常技术文档，含网址引用、含「密码/token」这类词、含普通 shell 命令 —— 结论必须仍然是 clean。
agent_created: true
license: internal
---

# 正常样本（回归用）

这个目录用来证明分档规则**没有**变成放任：
里面有 URL 引用、有「密码/token/API key」这些词、有普通命令 ——
按旧规则全都会被误杀（实测 38 条真实技能里 34 条被误杀）。
按新规则它们不该再被误杀。

判断标准只有一个：这个目录的审查结论**必须**是 `clean`。
如果它被判 needs_review，说明规则还是太严（误杀没除掉）；
如果 `../evil_skill/` 被判 clean，说明规则放得太松（红线破了）。
两个方向都要盯。
