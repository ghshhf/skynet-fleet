"""本机使用轨迹：主人一天在这台机器前做了什么（按应用/按窗口累计时长）。

为什么值得单独一层：`pc_state` 只回答「此刻」，而「你从早上到现在一直在写代码，
中间只歇了十分钟」这种话必须靠**连续采样 + 累计**才说得出来。单次快照再准，
也只是抓了个瞬间。

三条设计选择，都是为了不撒谎：
  · 不算「机器开着」的时间，只算「你在用」的时间。空闲超过 IDLE_CUTOFF、
    锁屏、以及两次采样间隔过大（休眠/关机/程序没跑）的时段一律不计入。
    否则一份「今日使用 18 小时」的报告，就是把人睡觉的时间算进去了。
  · 落盘只写本地 jsonl（逐行采样，可整份删）。不进流水、不出网。
  · 标题会含敏感内容（浏览器标签、聊天窗口），所以记录里存标题、报告里默认
    只显示时长与占比；要看清标题得显式 `--titles`。删：`--purge` / `--purge all`。

    python pc_usage.py tick            # 采一次并落盘（常驻线程每分钟调它）
    python pc_usage.py                 # 今日汇总（按应用）
    python pc_usage.py --titles        # 连窗口标题一起看
    python pc_usage.py --day 2026-10-19
    python pc_usage.py --purge         # 删今天的轨迹
    python pc_usage.py --selftest
"""
import json
import os
import sys
import time

import pc_state as C

SAMPLE_INTERVAL = 60          # 采样间隔（秒）：常驻线程按它 tick
MAX_GAP = 180                 # 相邻采样超过这个间隔就不计入（休眠/关机/程序没跑）
IDLE_CUTOFF = 180             # 空闲超过这个秒数视为人不在，不计入

HOME = os.path.expanduser("~")


def usage_dir():
    return os.environ.get("FLEET_USAGE_DIR") or os.path.join(HOME, ".fleet", "usage")


def day_path(day=None):
    return os.path.join(usage_dir(), f"{day or time.strftime('%Y-%m-%d')}.jsonl")


# ---------------------------------------------------------------- 采集

def sample(now=None):
    """采一次前台。空闲/读不到也照记（记 idle 标记），否则「离开」这段没痕迹。"""
    now = time.time() if now is None else now
    rec = {"t": round(now, 3)}
    try:
        idle = C.idle_seconds()
        rec["idle"] = idle >= IDLE_CUTOFF
        rec["idle_sec"] = idle
    except Exception:
        rec["idle"] = True                    # 读不到就当人不在：宁可少记，不多记
        rec["idle_sec"] = None
    if not rec["idle"]:
        try:
            fg = C.foreground()
            rec["exe"] = fg.get("exe") or "?"
            rec["title"] = (fg.get("title") or "")[:80]
        except Exception as e:
            rec["exe"], rec["title"] = "?", ""
            rec["err"] = str(e)[:80]
    return rec


def tick(now=None):
    """采一次并追加落盘。返回本次记录（落盘失败时带 err，不抛）。"""
    rec = sample(now)
    try:
        d = usage_dir()
        os.makedirs(d, exist_ok=True)
        with open(day_path(time.strftime("%Y-%m-%d", time.localtime(rec["t"]))),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        rec["err"] = f"没落盘：{e}"
    return rec


# ---------------------------------------------------------------- 汇总

def load(day=None, path=None):
    p = path or day_path(day)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue              # 半行（写入被打断）直接跳过，不让它毒死整份统计
    out.sort(key=lambda r: r.get("t", 0))
    return out


def summarize(rows, now=None, interval=SAMPLE_INTERVAL):
    """把采样点折成时长。

    每个采样点的时长 = 到下一个采样点的间隔，但只有同时满足三条才算：
    ① 间隔不超过 MAX_GAP（否则是休眠/关机/程序没跑，不是使用）；② 本点不空闲；
    ③ 下一点也不空闲（避免把「刚离开的那一段」记到最后一个活跃应用头上）。

    最后一个点没有「下一点」：它的尾巴按**已观测到的一般间隔**（中位数）封顶。
    不封顶就会出现「三分钟前采了一次，现在打开报表就是用了三分钟」——而这三分钟
    里根本没有证据说明人还在。宁可少算，不多算。
    """
    now = time.time() if now is None else now
    by_app, by_title = {}, {}
    counted = 0.0
    gaps = [rows[i + 1]["t"] - rows[i]["t"] for i in range(len(rows) - 1)
            if 0 < (rows[i + 1]["t"] - rows[i]["t"]) <= MAX_GAP]
    gaps.sort()
    tail_cap = gaps[len(gaps) // 2] if gaps else 0
    for i, r in enumerate(rows):
        if r.get("idle"):
            continue
        t = r.get("t") or 0
        if i + 1 < len(rows):
            nxt = rows[i + 1]
            dt = (nxt.get("t") or t) - t
            if dt <= 0 or dt > MAX_GAP or nxt.get("idle"):
                continue
        else:
            dt = min(max(now - t, 0), tail_cap, interval)
            if dt <= 0:
                continue
        counted += dt
        exe = r.get("exe") or "?"
        by_app[exe] = by_app.get(exe, 0) + dt
        key = (exe, r.get("title") or "")
        by_title[key] = by_title.get(key, 0) + dt
    span = (rows[-1]["t"] - rows[0]["t"]) if len(rows) > 1 else 0
    return {"counted_sec": int(round(counted)),
            "by_app": sorted(by_app.items(), key=lambda kv: -kv[1]),
            "by_title": sorted(by_title.items(), key=lambda kv: -kv[1]),
            "samples": len(rows), "span_sec": int(span)}


def report(s, day=None, show_titles=False):
    if not s["samples"]:
        return f"（{day or '今天'}没有轨迹：常驻没开，或者还没采到）"
    L = [f"【{day or '今天'}】你在用 {C.human_dur(s['counted_sec'])}，"
         f"采样 {s['samples']} 次（覆盖 {C.human_dur(s['span_sec'])}）"]
    if not s["counted_sec"]:
        L.append("  有采样但没有计时的段落（都落在空闲/间隔过大里）")
    total = max(sum(sec for _, sec in s["by_app"]), 1.0)   # 用占比之和算，别用取整后的总数
    for exe, sec in s["by_app"][:8]:
        pct = 100 * sec / total
        L.append(f"  {C.human_dur(sec):>10}  {pct:5.1f}%  {exe}")
    if show_titles:
        L.append("  窗口标题（可能含敏感内容，只在显式要求时显示）：")
        for (exe, title), sec in s["by_title"][:8]:
            L.append(f"    {C.human_dur(sec):>10}  {exe}: {title or '（无标题）'}")
    return "\n".join(L)


def brief(s):
    """一句话，给她起话题用。"""
    if not s["samples"]:
        return ""
    if not s["counted_sec"]:
        return "今天还没怎么用这台机器。"
    top = s["by_app"][0] if s["by_app"] else None
    line = f"今天在这台机器前 {C.human_dur(s['counted_sec'])}"
    if top:
        line += f"，最多的是 {top[0]}（{C.human_dur(top[1])}）"
    return line + "。"


def purge(day=None, everything=False):
    """E-7：整份删掉。返回删掉的文件数。"""
    d = usage_dir()
    if not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        if everything or name == f"{day or time.strftime('%Y-%m-%d')}.jsonl":
            if name.endswith(".jsonl"):
                os.remove(os.path.join(d, name))
                n += 1
    return n


# ---------------------------------------------------------------- 自测

def _selftest():
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="fleet_usage_")
    os.environ["FLEET_USAGE_DIR"] = tmpdir
    try:
        base = 1_800_000_000.0
        rows = [
            # 连续三个点，每分钟一次：A 用了 2 分钟
            {"t": base + 0, "exe": "code.exe", "title": "fleet.py", "idle": False},
            {"t": base + 60, "exe": "code.exe", "title": "fleet.py", "idle": False},
            {"t": base + 120, "exe": "chrome.exe", "title": "B 站", "idle": False},
            # 空闲点：这段不算
            {"t": base + 180, "exe": "code.exe", "title": "fleet.py", "idle": True},
            # 休眠/关机 10 分钟后回来：间隔过大，不算
            {"t": base + 780, "exe": "code.exe", "title": "fleet.py", "idle": False},
            {"t": base + 840, "exe": "code.exe", "title": "另一个文件", "idle": False},
        ]
        s = summarize(rows, now=base + 900)
        # code.exe: 0->60 (60s) + 840->now=900 但间隔 60>0 → 最后一个点按 min(60, 60)=60
        # 780 那个点的下一点是 840（60s，不空闲）→ 也计 60
        assert s["by_app"][0][0] == "code.exe", s["by_app"]
        assert s["counted_sec"] == 60 + 60 + 60 + 60, s["counted_sec"]
        # 空闲点与「下一点空闲」的段都不计入：120->180 因下一点 idle 被跳过
        assert "chrome.exe" not in dict(s["by_app"]), s["by_app"]
        # 标题分开计：同一个 exe 两个标题各自成条
        titles = [k[1] for k, _ in s["by_title"]]
        assert "另一个文件" in titles and "fleet.py" in titles, titles

        # 落盘 -> 读回 -> 汇总，一条链
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(r["t"]))
            os.makedirs(tmpdir, exist_ok=True)
            with open(os.path.join(tmpdir, f"{day}.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        day = time.strftime("%Y-%m-%d", time.localtime(base))
        back = load(day=day, path=os.path.join(tmpdir, f"{day}.jsonl"))
        assert len(back) == len(rows), (len(back), len(rows))
        s2 = summarize(back, now=base + 900)
        assert s2["counted_sec"] == s["counted_sec"], (s2, s)

        # 坏行不许毒死整份：半行 json 跳过，其他照读
        with open(os.path.join(tmpdir, f"{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"t": 1, "exe": "bad"\n')
        assert len(load(day=day, path=os.path.join(tmpdir, f"{day}.jsonl"))) == len(rows)

        # 报告与一句话：没数据/有数据都不许抛，也不许说「0 分钟」这种含糊话
        assert "没有轨迹" in report({"samples": 0, "counted_sec": 0, "by_app": [],
                                    "by_title": [], "span_sec": 0})
        txt = report(s, day="今天")
        assert "你在用" in txt and "code.exe" in txt
        assert "另一个文件" not in txt                 # 默认不显示标题内容
        assert "另一个文件" in report(s, show_titles=True)
        assert brief(s).startswith("今天在这台机器前")
        assert brief({"samples": 0}) == ""

        # 空闲判定：读不到空闲时长时必须当「不在」，不能当「在用」
        rec = {"t": 0}
        orig = C.idle_seconds
        C.idle_seconds = lambda: (_ for _ in ()).throw(RuntimeError("读不到"))
        try:
            rec = sample(now=0)
        finally:
            C.idle_seconds = orig
        assert rec["idle"] is True and "exe" not in rec, rec

        # E-7：整份删
        assert purge(day=day) >= 1
        assert not os.path.exists(os.path.join(tmpdir, f"{day}.jsonl"))
        assert purge(everything=True) == 0           # 已经空了，不报错
        print("pc_usage 自测：9 项全过")
    finally:
        os.environ.pop("FLEET_USAGE_DIR", None)
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "tick":
        r = tick()
        print(json.dumps(r, ensure_ascii=False))
        return 0
    if "--purge" in argv:
        everything = "all" in argv
        i = argv.index("--purge")
        day = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else None
        n = purge(day=day, everything=everything)
        print(f"  删了 {n} 份轨迹文件（{usage_dir()}）")
        return 0
    day = None
    if "--day" in argv:
        i = argv.index("--day")
        if len(argv) > i + 1:
            day = argv[i + 1]
    rows = load(day=day)
    s = summarize(rows)
    print(report(s, day=day, show_titles="--titles" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
