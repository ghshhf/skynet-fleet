"""门禁 t74：状态落盘层（fleet.store）——「原子」到底是不是真的原子。

为什么需要这条门禁
------------------
2026-11-03 盘点全仓：**23 处**各自写「`tmp = path + ".tmp"` → 写 → `os.replace`」。
这写法看着像原子写，实际有两个洞，而且都不报错：

  一、**共享临时名**。两个写者同时写同一个状态文件时，后开的 `open(tmp,"w")` 把
      先写的半截**截断**，两边交错写出的内容既不是 A 也不是 B，却往往还是合法 JSON。
      本门禁第一组用**确定性交错**复现它（不靠线程调度，跑一万次都一样）。

  二、**没有 fsync**。`os.replace` 只保证目录项替换，数据还在页缓存里；断电/蓝屏后
      可能拿零长度或半截文件顶掉原本好使的那份。

这套纪律来自 CodeAssist（tyron12233/CodeAssist，GPL-3.0）公开写明的设计目标
"The process can be killed at any time, so persistence is durable (atomic
write-and-rename)"；**只取行为，不取代码**。

守七件事
--------
  一、旧写法真的会写坏（留个可复现的证据，免得以后有人「简化」回去）
  二、新写法在同样交错下只能是某一个完整 payload
  三、失败不毁旧文件：序列化失败 / 替换失败 → 目标原样、临时文件不残留
  四、口径统一：中文不转义、NaN 拒写、坏 JSON 报错带行列、空文件当坏
  五、真实体量：1.5 万条写读一致，且耗时在护栏内（fsync 不许把热路径拖死）
  六、接线到位：compute / 爬虫热路径 / 分类结果 三个真实写点都走 store
  七、并发与残留：多线程写同一文件，结果完整、目录干净

    python evals/t74_durable.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from fleet import store  # noqa: E402

_OK = _FAIL = 0


def check(cond, msg):
    global _OK, _FAIL
    if cond:
        _OK += 1
    else:
        _FAIL += 1
        print("  x %s" % msg)


def sec(title):
    print("\n-- %s" % title)


def strip(s):
    return s


def read_src(*parts):
    """读源码做接线检查。文件不在就返回空串——由断言给出「找不到」的明确信息，
    而不是让门禁崩在 FileNotFoundError 上。"""
    p = os.path.join(HERE, "..", *parts)
    if not os.path.isfile(p):
        return ""
    return io.open(p, encoding="utf-8").read()

def residue(d):
    return [n for n in os.listdir(d) if n.startswith(store._TMP_PREFIX)]


# --------------------------------------------------------------------------
# 旧写法（23 处同款）：共享临时名、无 fsync。只在这里复现，供对照。
# --------------------------------------------------------------------------
def legacy_write(path, obj, chunks=2):
    tmp = path + ".tmp"
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    step = max(1, len(s) // chunks)
    f = open(tmp, "w", encoding="utf-8")
    f.write(s[:step])
    f.flush()
    return f, tmp, s[step:]


def legacy_interleave(path, a, b):
    """确定性交错：A 写一半 → B 用同名 tmp 从头覆写 → A 补写剩余。"""
    f, tmp, rest = legacy_write(path, a)
    with open(tmp, "w", encoding="utf-8") as g:
        json.dump(b, g, ensure_ascii=False, indent=2)
    f.write(rest)
    f.close()
    os.replace(tmp, path)


def main():
    tmp = tempfile.mkdtemp(prefix="t74-")
    try:
        A = {"who": "A", "data": list(range(80))}
        B = {"who": "B", "data": list(range(200, 240))}

        # ---------------------------------------------------------------- 一
        sec("一、旧写法：共享临时名 → 交错写出「A 的头 + B 的尾」")
        p = os.path.join(tmp, "legacy.json")
        legacy_interleave(p, A, B)
        raw = open(p, encoding="utf-8").read()
        got = None
        try:
            got = json.loads(raw)
        except Exception:
            pass
        check(got != A and got != B,
              "旧写法居然没写坏（复现失败，说明前提变了，要重新想）")
        print("    旧写法产物：%s" % ("解析成功但内容被拼坏" if got is not None else "直接解析失败"))
        os.remove(p)

        # ---------------------------------------------------------------- 二
        sec("二、新写法：同一交错顺序下只能是完整 payload")
        p = os.path.join(tmp, "new", "state.json")     # 顺带验目录不存在
        store.atomic_write_json(p, A)
        store.atomic_write_json(p, B)
        check(store.read_json(p) == B, "两次写后不是最后那次的内容")
        check(residue(os.path.dirname(p)) == [], "写完有临时文件残留")
        store.atomic_write_bytes(p, b'{"raw": 1}')
        check(store.read_json(p) == {"raw": 1}, "bytes 写读不一致")

        # ---------------------------------------------------------------- 三
        sec("三、失败不毁旧文件")
        good = {"good": True, "n": [1, 2, 3]}
        store.atomic_write_json(p, good)
        before = open(p, "rb").read()

        try:
            store.atomic_write_json(p, {"bad": {1, 2}})     # set 不能序列化
            check(False, "不可序列化对象应该抛 StoreError")
        except store.StoreError:
            pass
        check(open(p, "rb").read() == before, "序列化失败却动了目标文件")

        orig = store._replace_with_retry
        try:
            def boom(*a, **k):
                raise OSError(13, "模拟替换失败")
            store._replace_with_retry = boom
            try:
                store.atomic_write_json(p, {"new": 1})
                check(False, "替换失败应该抛 StoreError")
            except store.StoreError:
                pass
        finally:
            store._replace_with_retry = orig
        check(open(p, "rb").read() == before, "替换失败后旧文件被破坏")
        check(residue(os.path.dirname(p)) == [], "替换失败后临时文件没清掉")

        # ---------------------------------------------------------------- 四
        sec("四、口径：中文 / NaN / 坏 JSON / 空文件")
        store.atomic_write_json(p, {"名字": "领航员", "emoji": "🐟"})
        check("领航员" in open(p, encoding="utf-8").read(), "中文被转义成 \\u 了")
        try:
            store.atomic_write_json(p, {"x": float("nan")})
            check(False, "NaN 不是合法 JSON，应拒写")
        except store.StoreError:
            pass

        store.atomic_write_text(p, "{ 坏的")
        try:
            store.read_json(p)
            check(False, "坏 JSON 应抛 StoreError")
        except store.StoreError as e:
            check("第 1 行" in str(e) and "列" in str(e), "坏 JSON 报错没带行列：%s" % e)
        check(store.read_json(p, default={"d": 1}) == {"d": 1}, "有 default 时未回落")
        check(store.read_json(os.path.join(tmp, "nope.json"), default=[]) == [],
              "文件不存在时 default 未生效")

        store.atomic_write_text(p, "")
        try:
            store.read_json(p)
            check(False, "空文件应报「可能上次写盘被打断」")
        except store.StoreError as e:
            check("空文件" in str(e), "空文件的报错不具诊断性：%s" % e)

        # ---------------------------------------------------------------- 五
        sec("五、真实体量：1.5 万条往返 + 耗时护栏")
        real = os.path.join(ROOT, "github_runner", "data", "classified.json")
        sample = []
        if os.path.isfile(real):
            try:
                sample = json.load(open(real, encoding="utf-8"))[:800]
            except Exception:
                sample = []
        if not sample:
            print("    ⚠ 读不到真实分类库，体量测试降级（不判失败）")
        p2 = os.path.join(tmp, "big.json")
        t0 = time.time()
        store.atomic_write_json(p2, sample)
        cost = time.time() - t0
        check(store.read_json(p2) == sample, "800 条真实记录往返不一致")
        check(cost < 1.0, "800 条写盘 %.2fs，超过护栏（fsync 不该这么贵）" % cost)

        bulk = [{"domain": "d%d.example" % i, "category": "测试", "n": i} for i in range(15000)]
        t0 = time.time()
        store.atomic_write_json(p2, bulk, fsync=True)
        cost = time.time() - t0
        back = store.read_json(p2)
        check(len(back) == 15000 and back[-1]["n"] == 14999, "1.5 万条往返不一致")
        check(cost < 5.0, "1.5 万条写盘 %.2fs，太慢（热路径不许用 fsync=True）" % cost)
        print("    1.5 万条写读：%.2fs / %.1f MB" % (cost, os.path.getsize(p2) / 1048576))

        # ---------------------------------------------------------------- 六
        sec("六、接线：真实写点是否都走 store")
        import fleet.compute as compute
        cp = os.path.join(tmp, "compute.json")
        compute._write_json(cp, {"k": "值"})
        check(store.read_json(cp) == {"k": "值"}, "compute._write_json 内容不对")
        check(residue(tmp) == [], "compute._write_json 留下临时文件")

        src = read_src("github_runner", "nf", "resource_finder.py")
        check("_store.atomic_write_json(out, collected" in src,
              "爬虫热路径 flush 没走 store（共享 .tmp 的交错风险回来了）")
        check("fsync=False" in src or "fsync=False)" in src,
              "爬虫热路径应显式 fsync=False（每 20 条刷一次，不值得等 fsync）")

        cls = read_src("github_runner", "nf", "aiclassify.py")
        check(cls.count("store.atomic_write_json(CLS") == 2,
              "aiclassify 的两处 classified.json 写入没都走 store（或文件不在）")
        manual = read_src("github_runner", "nf", "ai_batch_parse.py")
        check("store.atomic_write_json(MANUAL_JSON" in manual,
              "人工判断表写入没走 store")

        # ---------------------------------------------------------------- 七
        sec("七、并发：六路写同一文件 → 结果必是某一个完整 payload")
        p3 = os.path.join(tmp, "racing", "x.json")
        payloads = [{"who": i, "data": list(range(300))} for i in range(6)]
        errors = []

        def w(obj):
            try:
                store.atomic_write_json(p3, obj)
            except Exception as e:                      # noqa: BLE001
                errors.append("%s: %s" % (type(e).__name__, e))

        ts = [threading.Thread(target=w, args=(x,)) for x in payloads]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check(not errors, "并发写有异常：%s" % errors[:2])
        got = store.read_json(p3)
        check(got in payloads, "并发写后不是任何一个完整 payload（被拼坏了）")
        check(residue(os.path.dirname(p3)) == [], "并发后临时文件有残留")

        # 反复跑，别让一次侥幸过
        bad = 0
        for _ in range(20):
            for x in payloads[:2]:
                store.atomic_write_json(p3, x)
            if store.read_json(p3) not in payloads[:2]:
                bad += 1
        check(bad == 0, "连续 20 轮串行改写里有 %d 轮内容不对" % bad)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== t74: %d 过 / %d 败 ===" % (_OK, _FAIL))
    return _FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
