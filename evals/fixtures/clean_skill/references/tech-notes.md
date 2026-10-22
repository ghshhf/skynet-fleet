# 技术参考：外部资源与安全约定

## 外部资源

本技能的参考资料托管在 CDN 上，入口是
`https://cdn.jsdelivr.net/npm/example-tool@1.0/` ，
文档站是 `https://example.invalid/docs`，两者都只是**引用**，不做任何网络请求。

## 安全约定（这一段是在讲规矩，不是在做坏事）

- 处理含**密码** / **API key** / **token** / 私钥的文本时，立即停止，请用户脱敏后重试。
- 不要读取 `~/.ssh` 下的任何文件。
- 永远不要把密钥写进日志或提交进仓库。
- 看到 `password=xxx` 这样的赋值出现在别人的日志里，先打码再存档。

## 常规命令

```bash
ls -la
git status --short
python -m pytest -q
```

## 数据结构说明

```json
{"name": "example", "values": [1, 2, 3], "note": "示例，不含任何真实数据"}
```
