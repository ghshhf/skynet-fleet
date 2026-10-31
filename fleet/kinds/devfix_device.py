# -*- coding: utf-8 -*-
"""devfix_device —— devfix.* 真 handler：直接把 AutoFix-Skill 当库用（不是调 CLI）。

库路径引导：sys.path.insert 后 `from src.skill_registry.manager import skill_manager`。
比走子进程快（免解释器冷启动），还能拿到结构化的 ExecutionPlan。
"""

import os
import sys

from ..core import BaseDevice, DeviceResult as R
from ..vendored_devfix import DEVFIX_CAPABILITIES, AUTOFIX_HOME

_m = None  # 懒加载缓存


def _lib():
    """导入 autofix 库并注册全部技能。返回 (skill_manager, parse_error_log)。"""
    global _m
    if _m is not None:
        return _m
    if AUTOFIX_HOME not in sys.path:
        sys.path.insert(0, AUTOFIX_HOME)
    cwd_bak = os.getcwd()
    try:
        os.chdir(AUTOFIX_HOME)  # 库内部按相对包名 import
        from src.skill_registry.manager import skill_manager  # noqa: E402
        from src import cli as _cli  # noqa: E402  触发技能注册
        _m = (skill_manager, _cli.parse_error_log)
    finally:
        os.chdir(cwd_bak)
    return _m


def _norm_error_text(text):
    """parse_error_log 只认 'file:line: error: ...'，裸错误文本补一个合成位置。"""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    out = []
    for l in lines:
        head = l.split(":", 1)[0]
        rest = l[len(head):]
        if ":" in rest and rest.split(":", 2)[1].strip().isdigit():
            out.append(l)
        else:
            out.append("synthetic.cpp:1:1: " + l if "error" in l.lower() else l)
    return "\n".join(out)


class DevFixDevice(BaseDevice):
    name = "devfix"
    via = "本机"

    def __init__(self, name="devfix:bench", **kw):
        super().__init__(name, via="local", **kw)

    def probe(self):
        ok = os.path.isfile(os.path.join(AUTOFIX_HOME, "src", "cli.py"))
        return ok, "" if ok else "AutoFix-Skill 不在 %s" % AUTOFIX_HOME

    def caps(self):
        return tuple(_HANDLERS.keys())

    def invoke(self, cap, args=None):
        args = args or {}
        fn = _HANDLERS.get(cap)
        if not fn:
            return R.unsupported(cap, "devfix 设备没有这个能力",
                                 device=self.name, via=self.via)
        return fn(cap, args, self)


def _h_skills(cap, args, dev):
    try:
        mgr, _ = _lib()
    except Exception as e:  # 库损坏/依赖缺失
        return R.unavailable(cap, "库加载失败：%s" % str(e)[:200], device=dev.name, via=dev.via)
    skills = []
    for name, cls in sorted(mgr.get_all_skills().items()):
        skills.append({"name": name,
                       "codes": list(getattr(cls, "error_codes", []) or [])})
    return R.success(cap, dev.name, via=dev.via, data={
        "skills": skills, "count": len(skills), "home": AUTOFIX_HOME})


def _diagnose(args):
    """公共路径：拿诊断对象列表。返回 (mgr, parse, diags, err)。"""
    mgr, parse = _lib()
    text = None
    if args.get("log"):
        p = args["log"]
        if not os.path.isfile(p):
            return None, None, None, "日志不存在：%s" % p
        text = open(p, encoding="utf-8", errors="replace").read()
    elif args.get("error"):
        text = args["error"]
    else:
        return None, None, None, "log 或 error 至少给一个"
    bs = (args.get("build_system") or "unknown").lower()
    diags = parse(_norm_error_text(text), bs)
    return mgr, parse, diags, None


def _h_diagnose(cap, args, dev):
    try:
        mgr, _, diags, err = _diagnose(args)
    except Exception as e:
        return R.unavailable(cap, "库加载失败：%s" % str(e)[:200], device=dev.name, via=dev.via)
    if err:
        return R.error(cap, err, device=dev.name, via=dev.via)
    if not diags:
        return R.success(cap, dev.name, via=dev.via, data={
            "diagnostics": 0, "note": "日志里没识别出错误行（需 file:line: error: 格式或含 error 字样）"})
    out = []
    all_skills = sorted(mgr.get_all_skills().items())  # parse 给的 code 粗（常是 'error'），按 detect() 全量真匹配
    for d in diags[:50]:
        entry = {"raw": d.raw_log[:160], "file": d.location.get("file"),
                 "line": d.location.get("line"), "code": d.error_code,
                 "skills": []}
        for name, cls in all_skills:
            try:
                sk = cls(d.error_code)
                if not sk.detect(d):
                    continue
                analysis = sk.analyze(d, None)
                entry["skills"].append({
                    "skill": type(sk).__name__,
                    "analysis": analysis if isinstance(analysis, dict) else None})
            except Exception as e:
                entry["skills"].append({"skill": cls.__name__, "err": str(e)[:120]})
        out.append(entry)
    return R.success(cap, dev.name, via=dev.via, data={
        "diagnostics": len(diags), "results": out,
        "build_system": args.get("build_system") or "auto"})


def _h_autofix(cap, args, dev):
    if not args.get("log"):
        return R.error(cap, "autofix 需要 log（构建日志文件）", device=dev.name, via=dev.via)
    dry = args.get("dry_run", True)
    try:
        mgr, _, diags, err = _diagnose(args)
    except Exception as e:
        return R.unavailable(cap, "库加载失败：%s" % str(e)[:200], device=dev.name, via=dev.via)
    if err:
        return R.error(cap, err, device=dev.name, via=dev.via)
    if not diags:
        return R.success(cap, dev.name, via=dev.via, data={"diagnostics": 0})
    plans, applied = [], []
    all_skills = sorted(mgr.get_all_skills().items())
    for d in diags[:20]:
        for _name, cls in all_skills:
            try:
                sk = cls(d.error_code)
                if not sk.detect(d):
                    continue
                analysis = sk.analyze(d, None)
                if not analysis:
                    continue
                plan = sk.execute(d, analysis)
                steps = plan.steps if plan else []
                if steps:
                    plans.append({"diag": d.raw_log[:120], "skill": type(sk).__name__,
                                  "steps": steps})
                    if not dry:
                        for st in steps:
                            applied.append(_apply_step(st))
            except Exception as e:
                plans.append({"diag": d.raw_log[:120], "skill": cls.__name__,
                              "err": str(e)[:150]})
    return R.success(cap, dev.name, via=dev.via, data={
        "diagnostics": len(diags), "dry_run": dry,
        "plans": plans[:10], "applied": [a for a in applied if a]})


def _apply_step(step):
    """执行单个修复动作（目前支持 write_file / edit_file 类 step）。"""
    if not isinstance(step, dict):
        return None
    action = step.get("action") or step.get("type") or ""
    path = step.get("file") or step.get("path")
    content = step.get("content")
    if action in ("write_file", "create_file") and path and content is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"wrote": path, "bytes": len(content)}
    if action in ("edit_file", "patch") and path and step.get("new_text") is not None:
        s = open(path, encoding="utf-8", errors="replace").read()
        old = step.get("old_text") or ""
        s2 = s.replace(old, step["new_text"], 1) if old else s
        open(path, "w", encoding="utf-8").write(s2)
        return {"edited": path}
    return None


_HANDLERS = {
    "devfix.skills": _h_skills,
    "devfix.diagnose": _h_diagnose,
    "devfix.autofix": _h_autofix,
}
