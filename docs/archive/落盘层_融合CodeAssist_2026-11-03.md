# 落盘层：从 CodeAssist 学的一条纪律（2026-11-03）

> 对象：`tyron12233/CodeAssist`（Android 上跑 IDE，Kotlin，2.1k★，GPL-3.0，2026-11-02 还在推）
> 结论：**它的域搬不动，它的工程约束能搬**。这一轮只搬了一条，但这条是硬伤。

## 一、它是什么，为什么值得看

在安卓手机上写 Kotlin/Java、直接在本机打出签好名的 APK：Gradle 跑不动，所以它自己写了一套
轻量构建系统（任务 DAG + up-to-date 检查）、自己的代码分析（Eclipse JDT + 自研 Kotlin 符号）、
磁盘上的符号索引（读多少取多少，内存不随工程膨胀）。

真值钱的是它的 `docs/`：架构、模块图、插件 SPI、构建引擎、语言后端、Git 后端，一篇篇写得很实。
一个 2.1k★ 的项目把"为什么这么做"写清楚，比代码本身更值得读。

它的设计目标里有一句直接命中天网：

> "The process can be killed at any time, so persistence is durable
> (atomic write-and-rename) and the model loads lazily."

## 二、为什么这条对天网是硬伤（不是洁癖）

天网全仓 **23 处**各自写同一套：

```python
tmp = path + ".tmp"
json.dump(obj, open(tmp, "w"))
os.replace(tmp, path)        # 看着像原子写
```

两个洞，都不报错：

1. **共享临时名**。两个写者同时写同一个状态文件时，后开的 `open(tmp, "w")` 把先写的半截**截断**，
   两边交错写出的内容既不是 A 也不是 B。
2. **没有 fsync**。`os.replace` 只保证目录项替换，数据还在页缓存；断电后可能拿零长度/半截文件
   顶掉原本好的那份。

第 1 条不是理论。2026-11-03 的确定性复现（`evals/t74_durable.py` 第一组，不靠线程调度）：

```
A 写一半 → B 用同名 tmp 从头覆写 → A 补写剩余 → replace
产物：JSON 解析成功，who="B"，条数却只有 A 的一半
```

**坏在语义层，不在解析层**——任何 `json.load` 都不会报错。

## 三、落到哪

新增 `fleet/store.py`，五步齐活：

```
唯一临时名（mkstemp）→ 写入 → flush → fsync(文件) → os.replace → fsync(目录)
```

Windows 打不开目录句柄做 fsync，**如实跳过**并写在注释里（不假装做了）。

分级持久性，这是本层唯一的"策略"：

| 场景 | fsync | 为什么 |
|---|---|---|
| 配置 / 索引 / 分类结果 / 库清单 | `True`（默认） | 低频、关键，丢一次就是全丢 |
| 爬虫每 20 条一刷 | `False` | 高频，真断电最多丢几秒，下一次 flush 重写 |

接线（三处真实写点，都是原来最危险的地方）：

- `fleet/compute.py::_write_json` —— 配置与算力池
- `github_runner/nf/resource_finder.py::flush_now` —— 爬虫热路径（每 20 条刷一次，`fsync=False`）
- `github_runner/nf/aiclassify.py`（两处 + 待判清单）、`ai_batch_parse.py` —— 1.47 万条分类结果

## 四、门禁 `evals/t74_durable.py`（26 条断言）

1. 旧写法确实会写坏（留证据，防以后有人"简化"回去）
2. 新写法在同一交错下只能是某一个完整 payload
3. 失败不毁旧文件：序列化失败 / 替换失败 → 目标原样、临时文件不残留
4. 口径统一：中文不转义、NaN 拒写、坏 JSON 报错带行列、空文件当坏
5. 真实体量：800 条真实分类记录 + 1.5 万条构造数据往返一致，耗时在护栏内
6. 接线到位：三个真实写点都走 store（静态 + 行为双查）
7. 并发：六路写同一文件，结果必是某一个完整 payload；连续 20 轮不侥幸

## 五、明确没搬的（以及什么时候才该搬）

| 它的能力 | 为什么不搬 | 什么时候再想 |
|---|---|---|
| 任务 DAG + up-to-date 检查 | 天网已有 `tools/gate_map.py --changed`（静态影响面），缺的是"结果指纹缓存" | 全量自检涨到跑不动时（现在 153 项） |
| 磁盘符号索引、就地读 | 天网资料库 1.5 万条实测 **114 ms / 13 MB**，还不疼 | 涨到 5 万条以上再上 |
| Eclipse JDT / Kotlin PSI / 字节码解释器 | 是 JVM 域的实现，天网不吃这条线 | 不做 |
| 插件 SPI 那套扩展点 | 天网的路由表（CAPABILITIES + reg）已经在做同一件事 | 不做 |

**方法固定下来**：先读它的 `docs/` 判断哪层是算法、哪层是框架；算法层用 Python 重写行为，
框架层和域实现一律不搬；搬完必须有一条门禁把语义钉死。
