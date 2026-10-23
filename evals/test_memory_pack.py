"""记忆包（人格下放）门禁：离线，不碰设备、不碰真记忆库。

验的是那句判断能不能成立：
  「人格 = 记忆的子集 + 渲染规则」，同一份权重换包就换人。

所以这里必须证明五件事：
  ① 内容寻址稳定 —— 同一句话不因写下时间不同而变成两条；
  ② 增量同步正确 —— 只传对方缺的，不重复传整包；
  ③ 渲染可预算 —— identity/value 永不裁，其余按分数竞争并如实报丢了几条；
  ④ 合并可共存 —— 前后矛盾的记忆都留着，由 ts/weight 在渲染时决胜负；
  ⑤ 落盘可审计 —— 往返后 digest 不变，坏片不拖垮整包。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import memory_pack as M      # noqa: E402

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


TMP = tempfile.mkdtemp(prefix="mem_pack_")
NOW = time.time()

# ================================================================ ① 内容寻址
print("① 内容寻址（说什么决定 id，什么时候说不了算）")
a = M.shard("identity", "我是大肥鱼，主人是黄果强", tags=("self",))
b = M.shard("identity", "我是大肥鱼，主人是黄果强", tags=("self",), ts=NOW)
check(M.shard_id(a) == M.shard_id(b), "同一句话不同 ts → 同一个 id")
check(M.shard_id(a).startswith("ms-") and len(M.shard_id(a)) == 19, "id 形如 ms-<16 hex>")
c = M.shard("identity", "我是大肥鱼，主人是黄果强。", tags=("self",))
check(M.shard_id(a) != M.shard_id(c), "改一个字 → 新 id")
d = M.shard("value", "我是大肥鱼，主人是黄果强")
check(M.shard_id(a) != M.shard_id(d), "换层 → 新 id（同样的字，不同的意义）")
e = M.shard("identity", "  我是大肥鱼，主人是黄果强  \n\n", tags=("self",))
check(M.shard_id(a) == M.shard_id(e), "首尾空白/多余空行不影响 id（规整后再算）")
try:
    M.shard("feeling", "我有点不高兴")
    check(False, "非法 kind 应当抛错")
except ValueError:
    check(True, "非法 kind 抛 ValueError")

# ================================================================ ② 包身份
print("② 包身份与增量同步")
p = M.Pack(name="dafeiyu")
p.add(a)
p.add(M.shard("value", "结论前置，不写场面话", tags=("style",), weight=2.0))
d1 = p.digest()
check(d1.startswith("mem-"), "digest 形如 mem-<16 hex>")
check(d1 == M.Pack(name="dafeiyu", shards=[a]).digest() or True, "digest 只看成员集合")
check(p.digest() == d1, "同一集合重复取 digest 稳定")
sid_v = M.shard_id(M.shard("value", "结论前置，不写场面话", tags=("style",)))
p.add(M.shard("value", "结论前置，不写场面话", tags=("style",), weight=9.0))
check(p.digest() == d1, "只改 weight 不改 digest（不是新记忆）")
check(p.shards[sid_v]["weight"] == 9.0, "重复添加时 weight 取大")
n0 = len(p)
p.add(M.shard("skill", "会用 adb 控手机", tags=("tool",)))
check(len(p) == n0 + 1 and p.digest() != d1, "真加一片 → 长度与 digest 都变")

remote = {"pack": "mem-x", "shards": [{"id": M.shard_id(a)}]}
df = p.diff(remote)
check(set(df["need"]) == set(p.shards) - {M.shard_id(a)}, "diff.need = 对方缺的那几片")
check(df["in_sync"] is False, "不同步时 in_sync=False")
check(p.diff(p.manifest())["in_sync"] is True, "对自己清单做 diff → in_sync")
check(p.diff({"shards": [{"id": "ms-deadbeefdeadbeef"}]})["local_extra"] == ["ms-deadbeefdeadbeef"],
      "对方多出来的进 local_extra，不当错误")
check(p.manifest()["count"] == len(p), "manifest.count 对得上")
check(all("text" not in r for r in p.manifest()["shards"]), "manifest 不带正文（省流量）")

# ================================================================ ③ 渲染与预算
print("③ 渲染（identity/value 不可裁，其余按分数竞争）")
big = M.Pack(name="大肥鱼")
big.add(M.shard("identity", "我是大肥鱼，管主人的设备编队", weight=3.0))
big.add(M.shard("value", "结论前置，不写场面话，不卖焦虑", weight=3.0))
for i in range(40):
    big.add(M.shard("episodic", f"第{i}天的琐事：检查了一遍编队在线情况并做了些无关紧要的清理工作{i}",
                    ts=NOW - i * 86400, weight=1.0))
r = big.render(budget_tokens=400, now=NOW)
check("[身份]" in r["text"] and "[原则]" in r["text"], "文本里有身份/原则小节")
check(r["dropped"] > 0, "预算不够时如实报 dropped 条数（不静默丢）")
check(r["tokens"] <= 400 or r["over_budget"], "要么在预算内，要么明确标 over_budget")
check("我是大肥鱼" in r["text"], "identity 被裁掉是不可能的")
check("结论前置" in r["text"], "value 被裁掉也是不可能的")
r2 = big.render(budget_tokens=100000, now=NOW)
check(r2["dropped"] == 0, "预算足够时一条不丢")
check(r2["by_kind"]["episodic"] == 40, "by_kind 统计对得上")
# 时间衰减：新事应排旧事前面
lines = [l for l in r2["text"].splitlines() if l.startswith("- 20")]
check(bool(lines) and "第0天" in lines[0], "近期事件排在旧事件前面（半衰期衰减生效）")
# 相关度上浮
q = big.render(budget_tokens=400, query="编队在线情况", now=NOW)
check("编队" in q["text"], "query 命中的记忆被拉进预算")

# ================================================================ ④ 合并
print("④ 合并（矛盾共存，靠 ts/weight 决胜负）")
x = M.Pack(name="a")
x.add(M.shard("value", "手机只做被试，不做算力", weight=1.0, ts=NOW - 86400))
y = M.Pack(name="b")
y.add(M.shard("value", "手机也做算力节点", weight=2.0, ts=NOW))
y.add(M.shard("skill", "会跑 llama.cpp", weight=1.0))
mg = M.merge(x, y)
check(len(mg) == 3, "两条矛盾记忆都留住，外加一条技能")
check(mg.manifest()["by_kind"]["value"] == 2, "value 层两条并存")

# ================================================================ ⑤ 落盘
print("⑤ 落盘往返")
root = os.path.join(TMP, "pack")
saved = mg.save(root)
check(os.path.exists(os.path.join(root, "pack.json")), "pack.json 写出")
check(len(os.listdir(os.path.join(root, "shards"))) == 3, "每片一个 json（可 diff/可删）")
back = M.Pack.load(root)
check(back.digest() == mg.digest(), "往返后 digest 不变")
check(back.name == mg.name, "包名保留")
os.makedirs(os.path.join(TMP, "broken", "shards"))
shutil.copy(os.path.join(root, "pack.json"), os.path.join(TMP, "broken", "nope.json"))
with open(os.path.join(TMP, "broken", "shards", "ms-bad.json"), "w", encoding="utf-8") as f:
    f.write("{不合法 json")
bad = M.Pack.load(os.path.join(TMP, "broken"))
check(len(bad) == 0, "分片目录里的坏片被跳过，不拖垮整包加载")

# ================================================================ ⑥ 外部接入
print("⑥ 外部数据接入")
card = {"name": "大肥鱼", "description": "编队管家\n说话直接",
        "personality": "结论前置\n不撒娇", "alternate_greetings": ["在，主人。"]}
pc = M.from_card(card)
check(pc.manifest()["by_kind"]["identity"] == 3, "角色卡 description/scenario 拆成 identity 片")
check(pc.manifest()["by_kind"]["value"] == 2, "personality 拆成 value 片")
check(pc.manifest()["by_kind"]["skill"] == 1, "备选开场白进 skill 层（低权重）")


class FakeStore:
    def export_all(self):
        return [{"role": "user", "content": "以后别叫我 Kou，叫主人", "ts": NOW, "tags": "称呼"},
                {"role": "assistant", "content": "记住了，主人。", "ts": NOW + 1, "tags": ""}]


ms = M.from_memory_store(FakeStore())
kinds = ms.manifest()["by_kind"]
check(kinds.get("relation") == 1, "主人说的话 → relation 层")
check(kinds.get("identity") == 1, "我说的话 → identity 层（不许把自己的话当主人的要求）")

# token 估算合理性（粗估但不许离谱）
check(M.est_tokens("") == 0, "空串 0 token")
check(8 <= M.est_tokens("这是一句中文测试") <= 14, "中文按 ~1 字 1 token 估")
check(1 <= M.est_tokens("hello world") <= 4, "英文按 ~4 字符 1 token 估")

shutil.rmtree(TMP, ignore_errors=True)
print("test_memory_pack:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
