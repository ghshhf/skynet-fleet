# X 搜索语法速查（2026-11 实测）

## 一句话

X 搜索**默认就是按最新排序**，不需要额外设置。真正决定结果好不好的是
`min_faves:` —— 不加它，搜出来的前几十条全是零粉小号的垃圾。

## 三类运算符

### 人

| 写法 | 作用 |
|---|---|
| `from:someone` | 某人发的 |
| `to:someone` | 回复某人的 |
| `@someone` | 提到某人的 |
| `list:ID` | 某个列表成员发的 |
| `filter:verified` | 只要认证号 |

### 时间

| 写法 | 作用 |
|---|---|
| `since:2026-11-01` | 该日期之后（含） |
| `until:2026-11-03` | 该日期之前（不含） |
| `within_time:2d` | 最近 2 天（`24h` / `7d` 皆可） |
| `since_id:<推文ID>` | 比这条更新的 |

日期格式**只能** `YYYY-MM-DD`，不能写 `Nov 1` 或 `11/01/2026`。

### 质量筛选（最有用的三个）

| 写法 | 作用 |
|---|---|
| `min_faves:50` | 至少 50 赞 |
| `min_retweets:20` | 至少 20 转发 |
| `min_replies:10` | 至少 10 回复 |
| `-min_faves:500` | **上限**（取反即成上限，用来找冷门好帖） |

### 内容类型

| 写法 | 作用 |
|---|---|
| `filter:links` | 带链接 |
| `url:github.com` | 链到指定域名（可直接写 `url:github.com/xxx`） |
| `filter:media` / `images` / `videos` | 带图 / 图 / 视频 |
| `-filter:retweets` | 去掉转推 |
| `-filter:replies` | 去掉回复（只要原创） |

### 语法硬规则

- 空格 = AND，多个条件叠加就是同时满足
- `OR` **必须大写**，小写 `or` 会被当成普通单词
- 冒号后面**不能有空格**：`from:nasa` 对，`from: nasa` 错
- 括号括住 OR 组：`(webflow OR framer) pricing`

## 扒资源的四个模板

直接复制，把 `KEYWORD` 换掉：

```
# 1. 找带仓库链接的高质量帖（最主力）
KEYWORD filter:links url:github.com min_faves:20 -filter:replies

# 2. 找有人在推荐的东西
("looking for" OR "recommend" OR "found this") KEYWORD min_faves:10 lang:en

# 3. 挖某人的精华帖
from:someone min_faves:100 -filter:replies

# 4. 近期窗口内的高赞帖
KEYWORD min_faves:50 since:2026-10-26 lang:en
```

## 在浏览器里怎么用

直接拼进地址栏，`q=` 后面接 URL 编码后的整串：

```
https://x.com/search?q=<URL编码后的查询>&src=typed_query&f=live
```

`f=live` = Latest 标签；不写就是 Top（按互动排序，会漏掉最新内容）。

## 实测记录

- `free ai api key since:2026-11-01` + Latest：能拿到 14 分钟前的推文，但前 8 条里 6 条是 0 粉小号
- 结论：**关键词越泛，越必须加 `min_faves:` 和 `filter:links`**，否则等于在垃圾桶里翻
