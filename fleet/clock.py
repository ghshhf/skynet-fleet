"""天网 · 单调高分辨率计时层（唯一正确写法；调用点不要再自己算时间差）

为什么有这个文件
----------------
2026-11-03 在本机（Windows 10 + Python 3.12.10）实测三个时钟，结果是**两个常用名字
都是错的**，正确答案是第三个：

    time.time()          GetSystemTimeAsFileTime()   15.625 ms   非单调
    time.monotonic()     GetTickCount64()            15.625 ms   单调但精度烂
    time.perf_counter()  QueryPerformanceCounter()   100 ns      单调 + 高分辨率

测同一段约 11 ms 的计算，各跑五次：

    time          11.02  11.99  11.99  11.02  11.98   ← 精度够，但会被校时改
    monotonic     16.00  15.00   0.00  16.00  15.00   ← **测出了 0 ms**，完全不能用
    perf_counter  11.70  11.69  11.68  11.75  11.83   ← 正确

`monotonic()` 出现 0 的原因：两次读数落在同一个 15.6 ms 时钟节拍里，差值就是 0。
凡是「用 monotonic 量毫秒」的代码，量快操作时都会得到一堆 0 或 15.6 的整数倍——
耗时统计会变成假的，而且看不出是假的。

`time.time()` 的问题不是精度（它返回值细到 100 ns），是**非单调**：
NTP 校时、用户改时间、跨时区调整都会让它跳。用墙钟算超时的后果是双向的：

    预算 10 s，刚过去 0 s，墙钟被往回拨 5 s  ⇒ 剩余算成 15 s  ⇒ 超时永不触发（卡死）
    同样条件墙钟往前跳 5 s                  ⇒ 剩余算成 5 s   ⇒ 提前超时（假失败）

所以本层只有一条口径：
    **凡是「过去了多久 / 还剩多久 / 超时没有」，一律走 perf_counter。**
    **墙上时间只用来「记录这是几点」，绝不参与任何控制流。**

`Deadline` / `Stopwatch` 就是这条口径的落地形态。分片 sleep 是为了长睡被信号打断后
能续上剩余时长，而不是重新睡一遍。

`fmt_dur` 有一条刻意的规矩：**小于 1 ms 显示微秒，不显示 "0.0ms"**。0.0ms 这个数字
本身没有信息量，只会让人以为「这步不花时间」——上面 monotonic 的坑就是这么藏起来的。
"""
import contextlib
import time

__all__ = [
    "now", "wall", "stamp", "date_stamp",
    "elapsed", "Stopwatch", "Deadline", "budget", "timed",
    "sleep", "sleep_until", "fmt_dur", "clock_report", "CLOCK_NAME",
]

# 本层唯一依赖的时钟。换实现只需改这一行，全仓跟着变。
_now = time.perf_counter
CLOCK_NAME = "perf_counter"


def now():
    """单调、高分辨率的读数。只保证「两次调用相减有意义」，绝对值无意义、不可跨进程。"""
    return _now()


def wall():
    """墙上时间（Unix 秒）。**只用于记录时刻 / 格式化输出，不许参与超时判断。**"""
    return time.time()


def stamp(with_ms=True):
    """本地时间串，用作日志/文件名里的「几点」。"""
    fmt = "%Y-%m-%d %H:%M:%S.%f" if with_ms else "%Y-%m-%d %H:%M:%S"
    s = time.strftime(fmt, time.localtime(wall()))
    return s[:-3] if with_ms else s


def date_stamp():
    """只要日期 `YYYY-MM-DD`，给按天分目录用。"""
    return time.strftime("%Y-%m-%d", time.localtime(wall()))


def elapsed(t0):
    """从 t0（由 now() 取得）到现在的秒数。"""
    return _now() - t0


# ---------------------------------------------------------------- 计时器


class Stopwatch:
    """测耗时。`sw.elapsed()` 可反复调用（不停止），`sw.lap()` 取一段并清零。"""

    __slots__ = ("_t0", "_laps")

    def __init__(self, start=True):
        self._t0 = _now() if start else None
        self._laps = []

    def start(self):
        if self._t0 is None:
            self._t0 = _now()
        return self

    def reset(self):
        self._t0 = _now()
        self._laps = []
        return self

    def elapsed(self):
        if self._t0 is None:
            return 0.0
        return _now() - self._t0

    def lap(self, tag=None):
        """取一段耗时并**清零**，用于「分阶段各花多久」。"""
        cur = _now()
        if self._t0 is None:
            self._t0 = cur
            return 0.0
        d = cur - self._t0
        self._t0 = cur
        if tag is not None:
            self._laps.append((str(tag), d))
        return d

    @property
    def laps(self):
        return list(self._laps)

    def __enter__(self):
        if self._t0 is None:
            self._t0 = _now()
        return self

    def __exit__(self, *exc):
        return False

    def __float__(self):
        return self.elapsed()

    def __repr__(self):
        return "<Stopwatch %.3fms>" % (self.elapsed() * 1000)


class Deadline:
    """一份预算：从创建时刻起算，`seconds` 秒后到期。

    与「自己记起点 + 每处拿墙钟减」的差别：
      - 墙钟被校时/改时间**不会**让它提前或延后（走 perf_counter）
      - `left()` 恒 ≥ 0，不会出现「剩余 -5 秒」这种要每处 max(0, ...) 擦屁股的值
      - `seconds=None` 表示无限预算，`left()` 返回 None，调用方照旧写 `if left is None`
    """

    __slots__ = ("_end", "total", "tag")

    def __init__(self, seconds, tag=None):
        self.total = None if seconds is None else max(0.0, float(seconds))
        self.tag = tag
        self._end = None if self.total is None else _now() + self.total

    @property
    def infinite(self):
        return self._end is None

    def left(self):
        """剩余秒数（≥0）；无限预算返回 None。"""
        if self._end is None:
            return None
        d = self._end - _now()
        return d if d > 0.0 else 0.0

    def used(self):
        """已用秒数；无限预算返回 None。"""
        if self.total is None:
            return None
        return self.total - self.left()

    def progress(self):
        """用了多少（0..1）；无限预算返回 None。"""
        if not self.total:
            return None if self.total is None else 1.0
        return min(1.0, max(0.0, self.used() / self.total))

    @property
    def expired(self):
        return False if self._end is None else _now() >= self._end

    def check(self, need=0.0):
        """还够不够再干 `need` 秒。够 → True。"""
        left = self.left()
        if left is None:
            return True
        return left > need

    def slice(self, cap):
        """给 socket/selector 之类的等待用：返回本次最多能等多久（≤cap）。"""
        left = self.left()
        if left is None:
            return cap
        return min(cap, left)

    def __bool__(self):
        return not self.expired

    def __repr__(self):
        left = self.left()
        if left is None:
            return "<Deadline 无限>"
        return "<Deadline 剩 %.3fs / 共 %.3fs>" % (left, self.total)


def budget(seconds, tag=None):
    """`Deadline` 的别名，读起来更顺：`dl = clock.budget(30)`。"""
    return Deadline(seconds, tag=tag)


@contextlib.contextmanager
def timed(sink=None, tag=None, min_ms=0.0):
    """`with clock.timed(sink, "抓取"):` —— 出块时把耗时记进 sink（dict / list / callable）。

    低于 min_ms 的**不记**（默认 0 = 全记）。sink 为 None 时只计时不落点，
    用于临时排查。
    """
    t0 = _now()
    box = {"tag": tag, "ms": None}
    try:
        yield box
    finally:
        ms = (_now() - t0) * 1000.0
        box["ms"] = ms
        if sink is not None and ms >= min_ms:
            if callable(sink):
                sink(tag, ms)
            elif isinstance(sink, dict):
                sink[tag] = sink.get(tag, 0.0) + ms
            elif isinstance(sink, list):
                sink.append((tag, ms))


# ---------------------------------------------------------------- 睡

_MAX_SLICE = 1.0


def sleep(seconds):
    """分片睡。

    Windows 上 `time.sleep(0.001)` 实测要 1.92 ms（受时钟节拍影响），这是系统行为，
    不是本层能修的；分片的意义只在于**长睡被信号/中断打断后能补足剩余时长**，
    而不是傻乎乎从头再睡一遍。
    """
    if seconds is None or seconds <= 0:
        return
    end = _now() + seconds
    while True:
        left = end - _now()
        if left <= 0:
            return
        time.sleep(left if left < _MAX_SLICE else _MAX_SLICE)


def sleep_until(deadline):
    """睡到 `Deadline` 到期（单次等待，不循环）。已在期 → 立即返回。"""
    if isinstance(deadline, Deadline):
        left = deadline.left()
    else:
        left = deadline - _now()
    if left is None or left <= 0:
        return
    time.sleep(left)


# ---------------------------------------------------------------- 显示


def fmt_dur(seconds):
    """人类可读耗时。**小于 1 ms 走微秒**，绝不显示 0.0ms。

    >>> fmt_dur(0.0004)   # '400µs'  —— 而不是 '0.0ms'
    >>> fmt_dur(0.35)     # '350ms'
    >>> fmt_dur(138.2)    # '2m18.2s'
    """
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 0:
        return "%dms" % int(s * 1000) if s > -1 else "%.1fs" % s
    if s < 0.001:
        return "%.0fµs" % (s * 1e6)
    if s < 1.0:
        return "%.0fms" % (s * 1000) if s >= 0.01 else "%.1fms" % (s * 1000)
    if s < 60:
        return "%.2fs" % s
    if s < 3600:
        m, r = divmod(s, 60)
        return "%dm%.1fs" % (int(m), r)
    h, r = divmod(s, 3600)
    m = r // 60
    return "%dh%dm" % (int(h), int(m))


def clock_report():
    """把本机五个时钟的真实参数摊开，用于排查「为什么计时不对」。"""
    out = []
    for name in ("time", "monotonic", "perf_counter", "process_time", "thread_time"):
        ci = time.get_clock_info(name)
        out.append({
            "name": name,
            "impl": ci.implementation,
            "monotonic": ci.monotonic,
            "resolution_ms": round(ci.resolution * 1000, 4),
            "used_by_fleet": name == CLOCK_NAME,
            "verdict": ("✔ 单调 + 够细" if (ci.monotonic and ci.resolution <= 1e-6)
                        else ("✘ 非单调，不许算超时" if not ci.monotonic
                              else "△ 单调但粒度 %.1f ms，不许量毫秒" % (ci.resolution * 1000))),
        })
    return out


# ---------------------------------------------------------------- 自测


def _selftest():
    fails = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # 1. Stopwatch 反复读不归零
    sw = Stopwatch()
    a = sw.elapsed()
    sleep(0.02)
    b = sw.elapsed()
    chk(b > a, "Stopwatch.elapsed 应该递增：%.6f → %.6f" % (a, b))

    # 2. Lap 清零
    d1 = sw.lap()
    chk(d1 >= 0.015, "lap 应该拿到至少 15ms，实际 %.6f" % d1)
    d2 = sw.lap()
    chk(d2 < 0.01, "lap 之后应清零，实际 %.6f" % d2)

    # 3. Deadline 剩余单调递减、不为负
    dl = Deadline(0.05)
    prev = dl.left()
    chk(prev is not None and 0 < prev <= 0.05, "初始剩余应 ≤ 0.05：%r" % prev)
    for _ in range(3):
        sleep(0.01)
        cur = dl.left()
        chk(cur <= prev + 1e-9, "剩余应递减：%.6f → %.6f" % (prev, cur))
        chk(cur >= 0, "剩余不该为负：%r" % cur)
        prev = cur

    # 4. 到期
    sleep(0.05)
    chk(dl.expired, "0.05s 预算过了 0.08s 还没到期")
    chk(dl.left() == 0.0, "到期后 left 必须是 0.0，实际 %r" % dl.left())

    # 5. 无限预算
    inf = Deadline(None)
    chk(inf.left() is None and not inf.expired and inf.check(1e9), "无限预算语义不对")
    chk(inf.slice(7) == 7, "无限预算 slice 应原样返回 cap")

    # 6. 0 预算立即到期
    z = Deadline(0)
    chk(z.expired and z.left() == 0.0, "0 预算应当场到期")

    # 7. slice 不超过剩余
    dl2 = Deadline(0.03)
    chk(dl2.slice(10) <= 0.03 + 1e-9, "slice 不该超过剩余")

    # 8. fmt_dur 不给 0.0ms
    chk(fmt_dur(0.0004).endswith("µs"), "亚毫秒应显示微秒：%r" % fmt_dur(0.0004))
    chk(fmt_dur(0.0000001) != "0.0ms", "极小值也不能显示 0.0ms")
    chk(fmt_dur(0.35) == "350ms", "0.35s 应显示 350ms：%r" % fmt_dur(0.35))
    chk(fmt_dur(138.2).startswith("2m"), "138.2s 应是 2m 级别：%r" % fmt_dur(138.2))
    chk(fmt_dur(None) == "—", "None 应显示破折号")

    # 9. timed 记账
    sink = {}
    with timed(sink, "睡觉"):
        sleep(0.01)
    chk("睡觉" in sink and sink["睡觉"] >= 8, "timed 没记到：%r" % sink)

    sink2 = []
    with timed(sink2, "x", min_ms=100000):
        pass
    chk(sink2 == [], "低于 min_ms 不该记：%r" % sink2)

    # 10. 精度：本层测毫秒级工作不能退化成 0 或 15.6 的整数倍
    def work():
        s = 0
        for i in range(60000):
            s += i * i
        return s

    sams = []
    for _ in range(7):
        a = now()
        work()
        sams.append((now() - a) * 1000)
    tiny = [x for x in sams if x <= 0.0]
    chk(not tiny, "perf_counter 不该量出 0：%r" % sams)
    # 全部落在 15.625 ms 的整数倍上 = 用错了时钟
    on_grid = sum(1 for x in sams if abs(x % 15.625) < 0.05)
    chk(on_grid < len(sams) - 1, "采样全落在 15.625ms 网格上，说明时钟回退成 GetTickCount64：%r" % sams)

    # 11. clock_report 自洽
    rep = clock_report()
    used = [r for r in rep if r["used_by_fleet"]]
    chk(len(used) == 1 and used[0]["name"] == "perf_counter", "声称使用的时钟不对")

    # 12. 墙钟不许混进 Deadline 的可执行代码（用 AST 查真实调用，不靠文本匹配）
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(Deadline)))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("wall", "time", "localtime"):
            base = f.value
            if isinstance(base, ast.Name) and base.id in ("time", "clock"):
                hits.append("%s.%s" % (base.id, f.attr))
    chk(not hits, "Deadline 的可执行代码里调了墙钟：%r" % hits)

    if fails:
        for f in fails:
            print("  ✘", f)
        print("clock 自测：%d 项失败" % len(fails))
        return False
    print("clock 自测：12 组断言全过（时钟 = %s）" % CLOCK_NAME)
    return True


if __name__ == "__main__":
    _selftest()
