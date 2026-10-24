"""天网·后台循环心跳（fleet.beat）—— 让「还活着吗」这个问题有答案。

为什么要有它
------------
后台循环最坏的失败形态不是崩，是**静默卡住**：
自愈循环不再重建 reverse、配对循环不再广播、relay 不再接连接 —— 进程都在、
端口都开着、日志一条不刷，用户看到的现象是「点了没反应」，而没有任何一处
能回答「这个循环最后一次成功是什么时候」。

所以每个常驻循环都必须做两件事（总纲 P0-2）：
  ① 每轮/每次事件 **beat 一次**（记周期数、成功数、失败数、最后一次时间）；
  ② 失败时把原因记下来，不是 pass 掉。
然后把这两样东西挂到 `/api/health` 上：**卡住的循环会被标成 stale**，
一眼看得出是「没在跑」还是「在跑但一直失败」。

设计纪律
--------
· 零依赖、线程安全（一把锁保护注册表）。
· **不刷屏**：默认不打日志，只记状态；要不要打由调用方决定（现有循环自己
  已经有可见日志，这里补的是「可查询」）。
· 名字是唯一键：同名重复 register 会拿到同一个对象，不会出现两个账本。
"""
import threading
import time

# name -> Beat
_REG = {}
_LK = threading.Lock()

# 多久没心跳算「卡住」。取 6 倍于常见循环周期（15s）——比这更久，
# 已经不是「刚好在忙」，而是真的停在那了。
DEFAULT_STALE_AFTER = 90.0


class Beat:
    """一个循环的心跳账本。

    :param name: 循环名字（唯一键，出现在 /api/health 里）
    :param stale_after: 秒；超过这么久没 beat 就判 stale
    """

    __slots__ = ("name", "stale_after", "started_at", "last_beat", "cycles",
                 "oks", "errors", "last_ok", "last_error", "last_error_at")

    def __init__(self, name, stale_after=DEFAULT_STALE_AFTER):
        self.name = name
        self.stale_after = float(stale_after)
        self.started_at = time.time()
        self.last_beat = 0.0       # 0 = 一次都没 beat 过（还没进循环）
        self.cycles = 0
        self.oks = 0
        self.errors = 0
        self.last_ok = 0.0
        self.last_error = ""
        self.last_error_at = 0.0

    # ---- 记账 ----
    def beat(self, ok=True, error="", now=None):
        """记一次心跳。ok=False 时请把「为什么」带上——空原因的失败等于没记。"""
        t = now if now is not None else time.time()
        self.cycles += 1
        self.last_beat = t
        if ok:
            self.oks += 1
            self.last_ok = t
        else:
            self.errors += 1
            self.last_error = str(error) or "（未说明原因）"
            self.last_error_at = t

    def allow(self, error, now=None):
        """记一次失败（语义糖）：beat(ok=False, error=error)。"""
        self.beat(False, error, now=now)

    # ---- 查询 ----
    def age(self, now=None):
        """距上次心跳多少秒；从没 beat 过返回 None。"""
        if not self.last_beat:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.last_beat)

    def stale(self, now=None):
        """是否卡住。**从没 beat 过也算 stale**——「起了线程但一轮没跑」
        正是最需要看见的状态，不能因为「还没开始」就当作正常。"""
        a = self.age(now=now)
        return True if a is None else a > self.stale_after

    def as_dict(self, now=None):
        n = now if now is not None else time.time()
        a = self.age(now=n)
        return {
            "name": self.name,
            "cycles": self.cycles,
            "ok": self.oks,
            "errors": self.errors,
            "age": None if a is None else round(a, 1),
            "stale": self.stale(now=n),
            "stale_after": self.stale_after,
            "uptime": round(max(0.0, n - self.started_at), 1),
            "last_error": self.last_error,
            "last_error_age": (None if not self.last_error_at
                               else round(max(0.0, n - self.last_error_at), 1)),
        }


def register(name, stale_after=DEFAULT_STALE_AFTER):
    """取得（或创建）某个循环的心跳账本。同名返回同一个对象。"""
    with _LK:
        b = _REG.get(name)
        if b is None:
            b = Beat(name, stale_after=stale_after)
            _REG[name] = b
        return b


def get(name):
    with _LK:
        return _REG.get(name)


def snapshot(now=None):
    """所有已注册循环的状态快照（按名字排序，顺带给出汇总）。"""
    n = now if now is not None else time.time()
    with _LK:
        rows = [b.as_dict(now=n) for b in _REG.values()]
    rows.sort(key=lambda r: r["name"])
    return {
        "loops": rows,
        "count": len(rows),
        "stale": [r["name"] for r in rows if r["stale"]],
        "any_error": [r["name"] for r in rows if r["errors"]],
    }


def reset():
    """清空注册表（**只给自测用**；线上调用会让状态凭空消失）。"""
    with _LK:
        _REG.clear()


# ---------------------------------------------------------------- 自测（离线）
if __name__ == "__main__":
    reset()
    b = register("demo", stale_after=5)
    assert register("demo") is b, "同名 register 必须返回同一个账本"
    assert b.stale(), "一次都没 beat 应判 stale（起了线程没跑正是最该看见的）"
    t0 = 1000.0
    b.beat(now=t0)
    assert not b.stale(now=t0), "刚 beat 过不该 stale"
    assert b.stale(now=t0 + 6), "超过 stale_after 应 stale"
    b.allow("网卡断开", now=t0 + 1)
    d = b.as_dict(now=t0 + 2)
    assert d["cycles"] == 2 and d["errors"] == 1, d
    assert d["last_error"] == "网卡断开", d
    b.beat(ok=False, error="", now=t0 + 3)
    assert b.as_dict(now=t0 + 3)["last_error"] == "（未说明原因）", "空原因要显式标出来"
    snap = snapshot(now=t0 + 4)
    assert snap["count"] == 1 and snap["stale"] == [], snap
    print("fleet.beat selftest: PASS")
