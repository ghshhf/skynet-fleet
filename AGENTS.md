# AGENTS · 开发约定

## 完成即推送（主人 2026-11-07 定的规矩）

每个可交付的小步完成后，不要攒批，按顺序走完：

1. 跑对应验证：纯 Kotlin 单测、`python3` 自测、文档门禁 `evals/t63_docs_sync.py` 与 `evals/t54_stats_sync.py`；
2. `git add` 明确文件，不 `git add -A`；
3. 写 conventional commit（`feat(phone): ...` / `fix(...): ...`），说清为什么；
4. `git push origin main`；
5. 改动命中 `tianwang_app/**` 或 `.github/workflows/build-app.yml` 时，用
   `gh run watch <run-id> --exit-status` 等到 CI 绿才算完成；只改文档时跑门禁即可。

红线：不 force push、不跳过失败的门禁或 CI、不在 key/凭据文件上提交。
