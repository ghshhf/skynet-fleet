"""联网作业执行器：把两个爬虫包成「能起、能看、能停、能收」的作业。

联网层由两个本地项目提供，本文件是它们与设备层之间唯一的桥：

  netforce（自建爬虫，天网的**搜索层**）—— 资源站发现 / 游戏 wiki 语料 / 站群扩散
  ai_frontier_crawl（train_15b/tools）—— arXiv + HF + OpenAlex 中英论文

为什么一律走子进程，不 import 进来当函数调用：

  1. 它们都是**长任务**。netforce 的发现模式一跑就是到次日 6 点，import 进来
     等于把无限循环塞进我们进程，界面直接卡死，且没法中途叫停。
  2. 它们**独占 stdout**。内部满屏 print 进度，import 进来会冲进我们的界面；
     子进程的输出则是我们主动去读的一行行文本，读几行、怎么呈现由我们定。
  3. 它们**随时可能崩**。子进程崩了只是这一条作业失败，主界面纹丝不动。

代价是要靠解析 stdout 拿进度——所以它俩的输出格式被当成契约来解析
（`_progress()` 认 `(4/5)` 这种），解析不出来就退化成"只知道还活着"。

不改动对方仓库一个字：路径全部走环境变量与候选推断。

  NETFORCE_HOME   netforce 仓库位置（环境变量优先；不设则按候选目录自动找）
  PAPERS_HOME     论文爬虫所在的 train_15b 位置
  NETFORCE_DATA   两个爬虫的落盘目录（覆盖 netforce 默认的 G:/_train_corpus_raw）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PY = sys.executable

# netforce 的三模式在命令行上怎么叫；None 表示"没装这个活"
NETFORCE_SCRIPT = "resource_finder.py"
PAPERS_SCRIPT = os.path.join("tools", "ai_frontier_crawl.py")

# ---------------------------------------------------------------------------
# 源仓库定位：候选制 + marker 判定，**不写死盘符**。
#
# 🔴 历史教训（2026-10-24）：这三行原本是 `D:\code\netforce` 三个硬编码常量。
#    那不是 bug——常量是在**另一台电脑**上写的，盘符对那台机器成立。但
#    换机 = 换盘符 = 每换一次电脑就要手动指一次路径；指错或忘了指，
#    12 个 web.* 能力静默降级成"没装"，而 README 仍写着「已接成远程设备」，
#    代码看起来一切正常（实测 available() 返回 False 才发现）。
#
# ⇒ 改成「推出来的候选」：本仓与 netforce 摆在**同一个父目录**下就自动命中
#    （当前布局 E:\xmanbian\{skynet-fleet,netforce}），换盘、换机、换 clone
#    位置都不用改代码。环境变量仍是最优先——给人留一个显式逃生口。
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent   # fleet/ 的上一级 = 仓库根

#: 判定「这就是那个仓库」的 marker。单文件不可靠（同名脚本到处都是），
#: 要求关键文件**同时在**——netforce 的判据是入口脚本 + skynet 子包。
NETFORCE_MARKERS = ("resource_finder.py", os.path.join("skynet", "__init__.py"))
PAPERS_MARKERS = ("tools/ai_frontier_crawl.py",)

#: 候选目录。**不含私人盘符**；历史路径只作兼容，找不到自然跳过。
DEFAULT_NETFORCE_DIRS = [
    str(_REPO_ROOT.parent / "netforce"),            # ① 与本仓并列（当前布局）
    str(_REPO_ROOT / "vendor" / "netforce"),        # ② 想内嵌放置时的落点
    str(_REPO_ROOT.parent / "code" / "netforce"),   # ③ 父目录下再套一层
    r"D:\code\netforce",                            # ④ 历史布局（老机器）
    str(Path.home() / "code" / "netforce"),         # ⑤ 用户目录习惯
]
DEFAULT_PAPERS_DIRS = [
    str(_REPO_ROOT.parent / "train_15b"),
    r"D:\work\_local_ai\train_15b",
]

#: 数据落盘目录。netforce 侧 `CORPUS_DIR.mkdir(parents=True, exist_ok=True)`
#: 会自建，所以这里只给路径、不预先创建。同样从 D:\work 挪到与本仓并列。
NETFORCE_DATA = Path(os.environ.get(
    "NETFORCE_DATA", str(_REPO_ROOT.parent / "_netforce_data")))


def _has_markers(base: str, markers) -> bool:
    return all(os.path.isfile(os.path.join(base, m)) for m in markers)


def discover_home(env_var: str, dirs, markers) -> str:
    """定位一个外部源仓库。找不到返回 ""（调用方如实说没装，不静默降级）。

    顺序：
      ① 环境变量**设了** ⇒ 就认它。指错了也认——显式指定优先于自动推断，
         悄悄换一个源比报错更坏。
      ② 没设 ⇒ 按候选目录逐个验 marker，第一个命中的算数。
    """
    p = os.environ.get(env_var, "").strip()
    if p:
        return p
    for d in dirs:
        if d and _has_markers(d, markers):
            return d
    return ""


NETFORCE_HOME = discover_home("NETFORCE_HOME", DEFAULT_NETFORCE_DIRS, NETFORCE_MARKERS)
PAPERS_HOME = discover_home("PAPERS_HOME", DEFAULT_PAPERS_DIRS, PAPERS_MARKERS)

# 时间戳 / 进度 的样式。netforce 每行形如：
#   [02:22:15] (4/5) ✓ 18trip「18TRIPWIKI」51KB 目录页 15 个（角色6/剧情8/档案1）
RE_TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*")
RE_PROG = re.compile(r"\((\d+)\s*/\s*(\d+)\)")
RE_OK = re.compile(r"✓|√")
RE_BAD = re.compile(r"已合并|不存在|跳回主站|风控|失败|超时")

# 只留"真有内容"的行，启动时那句"要在 X 分钟后结束"之类不算进度
RE_NOISE = re.compile(r"本轮结束|站群盘点开始|发现器启动", re.I)


def netforce_cli() -> str:
    """netforce 的入口脚本绝对路径；不存在返回空串。"""
    if not NETFORCE_HOME:
        return ""
    p = Path(NETFORCE_HOME) / NETFORCE_SCRIPT
    return str(p) if p.is_file() else ""


def papers_cli() -> str:
    """论文爬虫的入口脚本绝对路径；不存在返回空串。"""
    if not PAPERS_HOME:
        return ""
    p = Path(PAPERS_HOME) / PAPERS_SCRIPT
    return str(p) if p.is_file() else ""


def available() -> dict:
    """联网层的两个源在不在。缺哪个，哪个能力自动变不可用。"""
    return {
        "netforce": bool(netforce_cli()),
        "papers": bool(papers_cli()),
        "netforce_home": NETFORCE_HOME,
        "papers_home": PAPERS_HOME,
        "data": str(NETFORCE_DATA),
    }


class Job:
    """一个联网作业：子进程干活，我们只读它的输出行。

    用法：
        job = Job(...).start()
        for line in job.lines():     # 边跑边出，界面拿它画进度
            ...
        job.stop()                   # 随时叫停
    """

    def __init__(self, cmd, cwd, tag="", env=None, minutes=0):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.tag = tag
        self.minutes = minutes          # 限时：到点自己停（0 = 不限）
        self.env = env
        self.proc = None
        self._deadline = 0.0
        self._stopped = False
        self.total = 0
        self.done = 0
        self.hits = 0                   # 命中数（活站 / 收获条目）
        self.lines_seen = 0

    # ---------------- 起 ----------------

    def start(self):
        e = dict(os.environ)
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONUNBUFFERED"] = "1"       # 不设这个，输出会被憋在缓冲区里看着像卡死
        e["NETFORCE_DATA"] = str(NETFORCE_DATA)
        if self.env:
            e.update(self.env)
        kw = dict(stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  cwd=self.cwd, env=e,
                  text=True, encoding="utf-8", errors="replace", bufsize=1)
        if os.name == "nt":
            # 没有这行，Windows 上每起一个子进程就闪一个黑框
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(self.cmd, **kw)
        if self.minutes:
            self._deadline = time.time() + self.minutes * 60
        return self

    # ---------------- 读 ----------------

    def lines(self):
        """逐行吐出子进程输出；到点/被停/自然结束都停。"""
        if self.proc is None:
            return
        for raw in self.proc.stdout:
            line = (raw or "").rstrip("\r\n")
            if not line.strip():
                continue
            self.lines_seen += 1
            self._absorb(line)
            yield line
            if self._stopped:
                break
            if self._deadline and time.time() >= self._deadline:
                self.stop()
                break
        self.stop()

    def _absorb(self, line):
        """从一行里认领进度与命中——解析不出来就当没看见，不硬猜。"""
        m = RE_PROG.search(line)
        if m:
            self.done, self.total = int(m.group(1)), int(m.group(2))
        if RE_OK.search(line) and not RE_NOISE.search(line):
            self.hits += 1

    @property
    def progress(self) -> float:
        """0~1；total 未知时退化成 0（界面上表现为"转圈"而不是假进度）。"""
        if self.total <= 0:
            return 0.0
        return min(1.0, self.done / float(self.total))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ---------------- 停 ----------------

    def stop(self):
        self._stopped = True
        p = self.proc
        if p is None or p.poll() is not None:
            return self
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        return self


# ---------------------------------------------------------------- 产出读取

def read_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def netforce_out() -> dict:
    """netforce 资源发现的产出（`resources.json` 在它自己仓库里）。"""
    if not NETFORCE_HOME:
        return {"total": 0, "sites": []}
    raw = read_json(os.path.join(NETFORCE_HOME, "resources.json"), [])
    if isinstance(raw, dict):
        return {"total": raw.get("total", 0), "sites": raw.get("sites", [])[:8]}
    return {"total": len(raw), "sites": raw[-8:]}


def diffuse_out() -> dict:
    """站群扩散的产出：`anime_sites.json`。

    注意它的 `sites` 是**字典**不是列表：{子站代号: {status,size,name,pages}}。
    按列表去切片会得到 `TypeError: unhashable type: slice` 一路抛到调用方，
    而堆栈指向的是切片那行——看不出真凶是「对方把站清单存成了字典」。
    """
    d = read_json(NETFORCE_DATA / "anime_sites.json", {})
    if not d:
        return {"total": 0, "alive": 0, "at": "", "sites": []}
    raw = d.get("sites") or {}
    rows = []
    if isinstance(raw, dict):
        for code, v in list(raw.items())[:10]:
            v = v or {}
            rows.append({"code": code, "name": v.get("name", ""),
                         "status": v.get("status", ""),
                         "size": v.get("size", 0),
                         "pages": len(v.get("pages") or {})})
    else:
        rows = [{"code": str(x)} for x in list(raw)[:10]]
    return {
        "total": d.get("total", 0),
        "alive": d.get("alive", 0),
        "at": d.get("generated_at", ""),
        "sites": rows,
    }


def corpus_out(tag="blhx") -> dict:
    """角色语料的产出：`voice_<tag>.jsonl`。"""
    f = NETFORCE_DATA / f"voice_{tag}.jsonl"
    if not f.is_file():
        return {"tag": tag, "lines": 0, "samples": []}
    rows = []
    with open(f, encoding="utf-8") as fh:
        for i, ln in enumerate(fh):
            if i >= 6:
                break
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    n = sum(1 for _ in open(f, encoding="utf-8"))
    return {"tag": tag, "lines": n, "samples": rows}


def papers_dir() -> Path:
    return Path(PAPERS_HOME) / "data" / "frontier"


def papers_out(day="") -> dict:
    """论文产出：默认今天的 `YYYY-MM-DD.jsonl`，没有就退回最新那一天。"""
    d = papers_dir()
    if not d.is_dir():
        return {"day": "", "lines": 0, "items": []}
    target = d / f"{day or datetime.now().strftime('%Y-%m-%d')}.jsonl"
    if not target.is_file():
        cand = sorted(d.glob("*.jsonl"), reverse=True)
        if not cand:
            return {"day": "", "lines": 0, "items": []}
        target = cand[0]
    items = []
    n = 0
    with open(target, encoding="utf-8") as fh:
        for ln in fh:
            n += 1
            if len(items) < 8:
                try:
                    items.append(json.loads(ln))
                except Exception:
                    pass
    return {"day": target.stem, "lines": n, "items": items}


def net_status() -> dict:
    """联网层现状：一眼看清两个源在不在、各自攒了多少。"""
    av = available()
    papers = papers_out()
    diff = diffuse_out()
    disc = netforce_out()
    return {
        "netforce": av["netforce"],
        "papers": av["papers"],
        "data": av["data"],
        "papers_day": papers["day"],
        "papers_n": papers["lines"],
        "sites_total": disc["total"],
        "anime_total": diff["total"],
        "anime_alive": diff["alive"],
    }
