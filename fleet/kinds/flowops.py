"""能力流程编排：把能力调用串成带依赖、可续跑的流程。

拓扑排序、增量判定与执行由 doit 承担；本模块只管流程定义、结果落盘与状态
读取。每步结果独立成文件，已成功的步骤再次运行自动跳过，因此失败后重跑
不会重复执行有副作用的前置步骤。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import time

from ..core import BaseDevice, DeviceResult as R

_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REF = re.compile(r"^\$([A-Za-z0-9_-]+)\.(data|err)(?:\.(.+))?$")

CAPS = ("flow.save", "flow.list", "flow.get", "flow.validate",
        "flow.plan", "flow.run", "flow.status")


def _home(root=None):
    return root or os.environ.get("FLEET_FLOW_HOME") \
        or os.path.join(os.path.expanduser("~"), ".fleet")


def _flows(root=None):
    return os.path.join(_home(root), "flows")


def _runs(root=None):
    return os.path.join(_home(root), "flow_runs")


def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _to_dict(res):
    """DeviceResult / dict / 任意对象 → 统一的可落盘形状。"""
    if hasattr(res, "as_dict"):
        return res.as_dict()
    if isinstance(res, dict):
        return dict(res)
    return {"ok": bool(res), "data": {"value": str(res)}}


def _safe(name, what):
    if not isinstance(name, str) or not _SAFE.match(name):
        raise ValueError("%s 只能用字母数字下划线连字符（1–64 位）：%r" % (what, name))
    return name


def _resolve(args, deps, run_dir):
    """把 "$id.data.a.b" 换成上游结果里的值；只允许引用显式声明的依赖。"""
    if isinstance(args, dict):
        return {k: _resolve(v, deps, run_dir) for k, v in args.items()}
    if isinstance(args, (list, tuple)):
        return [_resolve(v, deps, run_dir) for v in args]
    if not isinstance(args, str) or not args.startswith("$"):
        return args
    m = _REF.match(args)
    if not m:
        raise ValueError("引用格式不合法：%r（应形如 $step.data.path）" % args)
    sid, field, path = m.group(1), m.group(2), m.group(3)
    if sid not in deps:
        raise ValueError("引用了 %s，但它不在 deps 里：%r" % (sid, args))
    up = _read_json(os.path.join(run_dir, sid + ".json")) or {}
    val = up.get(field)
    if path:
        for part in path.split("."):
            if not isinstance(val, dict) or part not in val:
                raise ValueError("上游 %s.%s 里没有 %s" % (sid, field, path))
            val = val[part]
    return val


def _check_refs(args, deps):
    """只查引用的格式与依赖归属，不在校验阶段读取上游结果。"""
    if isinstance(args, dict):
        for v in args.values():
            _check_refs(v, deps)
    elif isinstance(args, (list, tuple)):
        for v in args:
            _check_refs(v, deps)
    elif isinstance(args, str) and args.startswith("$"):
        m = _REF.match(args)
        if not m:
            raise ValueError("引用格式不合法：%r（应形如 $step.data.path）" % args)
        if m.group(1) not in deps:
            raise ValueError("引用了 %s，但它不在 deps 里：%r" % (m.group(1), args))


def _acyclic(steps):
    """静态环检查：把坏流程挡在调度器之外，避免运行时卡死。"""
    by_id = {s["id"]: s for s in steps}
    state = {}

    def walk(sid, trail):
        if state.get(sid) == 1:
            return trail[trail.index(sid):] + [sid]
        if state.get(sid) == 2:
            return None
        state[sid] = 1
        for dep in by_id[sid].get("deps") or []:
            if dep in by_id:
                cyc = walk(dep, trail + [dep])
                if cyc:
                    return cyc
        state[sid] = 2
        return None

    for s in steps:
        cyc = walk(s["id"], [s["id"]])
        if cyc:
            return cyc
    return None


def _check(steps, catalog_check=True):
    """流程静态校验。拓扑执行交给 doit，这里只挡住结构性错误。"""
    problems = []
    if not isinstance(steps, list) or not steps:
        return ["steps 必须是非空数组"]
    ids = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            problems.append("第 %d 步不是对象" % (i + 1))
            continue
        sid = s.get("id") or ""
        if not _SAFE.match(str(sid)):
            problems.append("第 %d 步 id 不合法：%r" % (i + 1, sid))
        elif sid in ids:
            problems.append("id 重复：%s" % sid)
        else:
            ids.append(sid)
        if not s.get("cap"):
            problems.append("第 %d 步缺 cap" % (i + 1))
        deps = s.get("deps") or []
        if not isinstance(deps, (list, tuple)):
            problems.append("%s 的 deps 必须是数组" % sid)
            deps = []
        if sid and sid in deps:
            problems.append("%s 不能依赖自己" % sid)
        try:
            _check_refs(s.get("args") or {}, set(deps))
        except ValueError as exc:
            problems.append("%s：%s" % (sid, exc))
    for s in steps:
        if not isinstance(s, dict):
            continue
        for dep in (s.get("deps") or []):
            if dep not in ids:
                problems.append("%s 依赖了不存在的步骤 %s" % (s.get("id"), dep))
    if not problems:
        cyc = _acyclic(steps)
        if cyc:
            problems.append("依赖成环：%s" % " → ".join(cyc))
    if not problems and catalog_check:
        try:
            from ..catalog import CAPABILITIES
            known = {c.name for c in CAPABILITIES}
            for s in steps:
                if s.get("cap") not in known:
                    problems.append("能力表里没有 %s" % s["cap"])
        except Exception:
            pass
    return problems


class FlowOps(BaseDevice):
    kind = "flowops"
    via = "local"

    def __init__(self, name="flow:ops", executor=None, home=None, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "orchestration")
        self._executor = executor
        self._root = home
        self._registry = None

    def probe(self):
        if importlib.util.find_spec("doit") is None:
            return False, "没装 doit（pip install doit）；调度层不自己实现"
        return True, ""

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        a = args or {}
        fn = {
            "flow.save": self._save, "flow.list": self._list,
            "flow.get": self._get, "flow.validate": self._validate,
            "flow.plan": self._plan, "flow.run": self._run,
            "flow.status": self._status,
        }.get(cap)
        if fn is None:
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        ok, why = self.probe()
        if not ok:
            return R.unavailable(cap, why, device=self.name)
        try:
            data = fn(a)
        except ValueError as exc:
            return R.error(cap, str(exc), device=self.name)
        except Exception as exc:  # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc), device=self.name)
        return R.success(cap, device=self.name, data=data,
                         side_effect=(cap in ("flow.save", "flow.run")))

    # ------------------------------------------------------------ 定义存取
    def _flow_path(self, name):
        return os.path.join(_flows(self._root), _safe(name, "流程名") + ".json")

    def _save(self, a):
        name = _safe(a.get("name") or "", "流程名")
        steps = a.get("steps")
        problems = _check(steps, catalog_check=bool(a.get("check_catalog", True)))
        if problems:
            raise ValueError("流程校验没过：" + "；".join(problems))
        flow = {"name": name, "desc": str(a.get("desc") or ""),
                "fail_fast": bool(a.get("fail_fast", True)), "steps": steps}
        _atomic_json(self._flow_path(name), flow)
        return {"name": name, "steps": len(steps), "path": self._flow_path(name)}

    def _list(self, a):
        d = _flows(self._root)
        names = sorted(f[:-5] for f in os.listdir(d)
                       if f.endswith(".json")) if os.path.isdir(d) else []
        return {"flows": names, "count": len(names)}

    def _get(self, a):
        name = _safe(a.get("name") or "", "流程名")
        flow = _read_json(self._flow_path(name))
        if not flow:
            raise ValueError("没有名为 %s 的流程" % name)
        return {"name": name, "flow": flow}

    def _load(self, a):
        if a.get("steps") is not None:
            steps = a["steps"]
            name = _safe(a.get("name") or "inline", "流程名")
            return {"name": name, "steps": steps,
                    "fail_fast": bool(a.get("fail_fast", True))}
        return self._get(a)["flow"]

    # ------------------------------------------------------------ 校验 / 计划
    def _validate(self, a):
        flow = self._load(a)
        problems = _check(flow["steps"],
                          catalog_check=bool(a.get("check_catalog", True)))
        return {"name": flow["name"], "ok": not problems,
                "problems": problems, "steps": len(flow["steps"])}

    def _plan(self, a):
        flow = self._load(a)
        problems = _check(flow["steps"],
                          catalog_check=bool(a.get("check_catalog", True)))
        if problems:
            raise ValueError("流程校验没过：" + "；".join(problems))
        run_id = _safe(a.get("run_id") or flow["name"], "run_id")
        run_dir = os.path.join(_runs(self._root), run_id)
        rows = [{"id": s["id"], "cap": s["cap"], "deps": list(s.get("deps") or []),
                 "result": os.path.join(run_dir, s["id"] + ".json"),
                 "done": os.path.isfile(os.path.join(run_dir, s["id"] + ".json"))}
                for s in flow["steps"]]
        return {"name": flow["name"], "run_id": run_id, "run_dir": run_dir,
                "steps": rows, "note": "执行顺序由 doit 按依赖决定，此处不预排"}

    # ------------------------------------------------------------ 执行
    def _exec(self, cap, args):
        if self._executor is not None:
            return self._executor(cap, args)
        if self._registry is None:
            from ..catalog import build
            self._registry = build()
        return self._registry.call(cap, args=args)

    def _tasks_for(self, steps, run_dir, fail_fast):
        from doit.task import dict_to_task

        tasks = []
        for s in steps:
            sid, deps = s["id"], list(s.get("deps") or [])
            target = os.path.join(run_dir, sid + ".json")

            def done(path=target):
                r = _read_json(path)
                return bool(r and r.get("ok") is True)

            def action(sid=sid, deps=deps, target=target, s=s):
                t0 = time.time()
                rec = {"id": sid, "cap": s["cap"], "ok": False, "ms": 0.0}
                try:
                    if fail_fast:
                        for dep in deps:
                            up = _read_json(os.path.join(run_dir, dep + ".json")) or {}
                            if up.get("ok") is not True:
                                rec.update({"skipped": True, "err": "上游 %s 未成功" % dep})
                                _atomic_json(target, rec)
                                return None
                    args = _resolve(s.get("args") or {}, set(deps), run_dir)
                    out = _to_dict(self._exec(s["cap"], args))
                    rec.update({"ok": bool(out.get("ok")),
                                "err": out.get("err") or "",
                                "data": out.get("data") or {},
                                "code": out.get("code") or "",
                                "device": out.get("device") or ""})
                except Exception as exc:  # noqa: BLE001
                    rec.update({"ok": False, "err": "%s: %s" % (type(exc).__name__, exc)})
                rec["ms"] = round((time.time() - t0) * 1000, 1)
                _atomic_json(target, rec)
                return None

            tasks.append(dict_to_task({
                "name": sid, "actions": [action], "targets": [target],
                "file_dep": [os.path.join(run_dir, d + ".json") for d in deps],
                "uptodate": [done],
            }))
        return tasks

    def _run(self, a):
        from doit.cmd_base import TaskLoader2
        from doit.doit_cmd import DoitMain

        flow = self._load(a)
        problems = _check(flow["steps"],
                          catalog_check=bool(a.get("check_catalog", True)))
        if problems:
            raise ValueError("流程校验没过：" + "；".join(problems))
        run_id = _safe(a.get("run_id") or flow["name"], "run_id")
        run_dir = os.path.join(_runs(self._root), run_id)
        os.makedirs(run_dir, exist_ok=True)

        # 流程改了却沿用旧结果会静默跑偏：指纹不一致就要求换 run_id 或显式重置。
        digest = hashlib.sha256(json.dumps(flow, sort_keys=True, ensure_ascii=False,
                                           default=str).encode("utf-8")).hexdigest()[:16]
        stamp = os.path.join(run_dir, "_flow.json")
        old = _read_json(stamp)
        if old and old.get("digest") != digest and not a.get("reset"):
            raise ValueError("run_id=%s 的流程定义变了；换 run_id 或传 reset=true" % run_id)
        if a.get("reset"):
            for f in os.listdir(run_dir):
                if f.endswith(".json") and f != "_flow.json":
                    os.remove(os.path.join(run_dir, f))
        _atomic_json(stamp, {"digest": digest, "name": flow["name"]})

        fail_fast = bool(a.get("fail_fast", flow.get("fail_fast", True)))
        tasks = self._tasks_for(flow["steps"], run_dir, fail_fast)

        class Loader(TaskLoader2):
            def setup(self, opt):
                pass

            def load_doit_config(self):
                return {"verbosity": 0, "dep_file": os.path.join(run_dir, "_doit_db")}

            def load_tasks(self, cmd, pos_args):
                return tasks

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = DoitMain(Loader()).run(["run", "--verbosity", "0"])
        return self._summarize(flow, run_id, run_dir, rc, buf.getvalue())

    def _summarize(self, flow, run_id, run_dir, rc=0, log=""):
        rows = []
        for s in flow["steps"]:
            rec = _read_json(os.path.join(run_dir, s["id"] + ".json")) or {}
            rows.append({"id": s["id"], "cap": s["cap"],
                         "ok": bool(rec.get("ok")), "skipped": bool(rec.get("skipped")),
                         "err": rec.get("err") or "", "ms": rec.get("ms") or 0.0})
        return {"name": flow["name"], "run_id": run_id, "run_dir": run_dir,
                "total": len(rows), "ok": sum(1 for x in rows if x["ok"]),
                "skipped": sum(1 for x in rows if x["skipped"]),
                "failed": sum(1 for x in rows if not x["ok"] and not x["skipped"]),
                "rc": rc, "steps": rows, "log": log[-400:]}

    def _status(self, a):
        run_id = _safe(a.get("run_id") or "", "run_id")
        run_dir = os.path.join(_runs(self._root), run_id)
        stamp = _read_json(os.path.join(run_dir, "_flow.json"))
        if not stamp:
            raise ValueError("没有这次运行：%s" % run_id)
        steps = sorted(f[:-5] for f in os.listdir(run_dir)
                       if f.endswith(".json") and not f.startswith("_"))
        rows = []
        for sid in steps:
            rec = _read_json(os.path.join(run_dir, sid + ".json")) or {}
            rows.append({"id": sid, "cap": rec.get("cap") or "",
                         "ok": bool(rec.get("ok")), "skipped": bool(rec.get("skipped")),
                         "err": rec.get("err") or "", "data": rec.get("data") or {}})
        return {"run_id": run_id, "name": stamp.get("name") or "",
                "steps": rows, "total": len(rows),
                "ok": sum(1 for x in rows if x["ok"])}


def _selftest():
    import shutil
    import tempfile

    fails, n = [], 0

    def ck(cond, label):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)

    root = tempfile.mkdtemp(prefix="flow_st_")
    try:
        d = FlowOps(home=root, executor=lambda cap, args: None)
        ok, why = d.probe()
        ck(ok, "probe：doit 已装（%s）" % why)
        ck(len(d.caps()) == 7, "七项能力")
        ck(not d.invoke("flow.nope", {}).ok, "未知能力要拒绝")

        def ex(cap, args):
            if cap == "x.fail":
                return R.error(cap, "故意失败", device="fake")
            return R.success(cap, device="fake", data={"value": 7, "echo": args})

        d = FlowOps(home=root, executor=ex)
        simple = [{"id": "s1", "cap": "x.ok", "args": {}},
                  {"id": "s2", "cap": "x.ok", "args": {"got": "$s1.data.value"},
                   "deps": ["s1"]},
                  {"id": "s3", "cap": "x.ok", "args": {"got": "$s2.data.value"},
                   "deps": ["s2"]}]
        r = d.invoke("flow.save", {"name": "demo", "steps": simple,
                                   "check_catalog": False})
        ck(r.ok and r.data["steps"] == 3, "save：存下三条")
        ck(os.path.isfile(os.path.join(root, "flows", "demo.json")), "save：文件真落盘")
        ck(d.invoke("flow.list", {}).data["flows"] == ["demo"], "list：列出流程")
        ck(d.invoke("flow.get", {"name": "demo"}).ok, "get：读回流程")
        ck(not d.invoke("flow.get", {"name": "none"}).ok, "get：不存在要报错")
        ck(d.invoke("flow.validate", {"steps": simple, "check_catalog": False}).data["ok"],
           "validate：合法流程通过")
        pl = d.invoke("flow.plan", {"steps": simple, "check_catalog": False})
        ck(pl.ok and len(pl.data.get("steps") or []) == 3, "plan：出三步计划")

        for bad, label in [
            ([], "空流程"),
            ([{"id": "a", "cap": "x"}, {"id": "a", "cap": "x"}], "id 重复"),
            ([{"id": "a", "cap": "x", "deps": ["zz"]}], "依赖不存在"),
            ([{"id": "a", "cap": "x", "deps": ["a"]}], "自依赖"),
            ([{"id": "a", "cap": "x", "args": {"v": "$nope.data.x"},
               "deps": []}], "引用非依赖步"),
            ([{"id": "a", "cap": "x", "deps": ["b"]},
              {"id": "b", "cap": "x", "deps": ["a"]}], "成环"),
            ([{"id": "a", "cap": ""}], "缺 cap"),
        ]:
            ck(not d.invoke("flow.validate", {"steps": bad,
                                              "check_catalog": False}).data["ok"],
               "validate 拒绝：" + label)

        ck(not d.invoke("flow.save", {"name": "../evil", "steps": simple,
                                      "check_catalog": False}).ok,
           "save：拒绝路径穿越")
        ck(not d.invoke("flow.run", {"run_id": "../evil", "steps": simple,
                                     "check_catalog": False}).ok,
           "run：拒绝非法 run_id")

        calls = {"n": 0}

        def counting(cap, args):
            calls["n"] += 1
            return R.success(cap, device="fake", data={"value": 7, "echo": args})

        d2 = FlowOps(home=root, executor=counting)
        r1 = d2.invoke("flow.run", {"run_id": "r1", "steps": simple,
                                    "check_catalog": False})
        ck(r1.ok and r1.data["ok"] == 3, "run：三步全成")
        ck(calls["n"] == 3, "run：执行器被调用三次")
        s1 = _read_json(os.path.join(root, "flow_runs", "r1", "s2.json")) or {}
        ck((s1.get("data") or {}).get("echo", {}).get("got") == 7,
           "run：上一步的值真的传到下一步")
        r2 = d2.invoke("flow.run", {"run_id": "r1", "steps": simple,
                                    "check_catalog": False})
        ck(r2.ok and r2.data["ok"] == 3, "续跑：结果仍全成")
        ck(calls["n"] == 3, "续跑：已成功的步骤一次都没重跑")
        st = d2.invoke("flow.status", {"run_id": "r1"})
        ck(st.ok and st.data["total"] == 3 and st.data["ok"] == 3, "status：读到三步全成")
        ck(not d2.invoke("flow.status", {"run_id": "no-such"}).ok, "status：不存在要报错")

        changed = [{"id": "s1", "cap": "x.changed", "args": {}}]
        ck(not d2.invoke("flow.run", {"run_id": "r1", "steps": changed,
                                      "check_catalog": False}).ok,
           "指纹：流程变了要拦住，不静默沿用旧结果")
        ck(d2.invoke("flow.run", {"run_id": "r1", "steps": changed,
                                  "check_catalog": False, "reset": True}).ok,
           "reset：显式重置后可以重跑")

        failing = [{"id": "a", "cap": "x.fail", "args": {}},
                   {"id": "b", "cap": "x.ok", "args": {}, "deps": ["a"]}]
        rf = d.invoke("flow.run", {"run_id": "rf", "steps": failing,
                                   "check_catalog": False})
        ck(rf.ok and rf.data["failed"] == 1, "失败：如实报一步失败")
        ck(rf.data["steps"][1]["skipped"] is True, "fail_fast：下游标跳过不执行")
        ck((_read_json(os.path.join(root, "flow_runs", "rf", "a.json")) or {}).get("ok")
           is False, "失败：失败本身也落盘")

        rn = d.invoke("flow.run", {"run_id": "rn", "steps": failing,
                                   "check_catalog": False, "fail_fast": False})
        ck(rn.data["steps"][1]["skipped"] is False, "fail_fast=false：下游照常执行")

        ck(_check(simple, catalog_check=False) == [], "_check：合法流程零问题")
        ck(_acyclic(simple) is None, "_acyclic：无环返回 None")
        ck(_acyclic([{"id": "a", "deps": ["b"]}, {"id": "b", "deps": ["a"]}]) is not None,
           "_acyclic：有环能报出来")
        ck(_to_dict({"ok": True})["ok"] is True, "_to_dict：dict 原样")
        ck(_to_dict(R.success("x", device="d"))["ok"] is True, "_to_dict：DeviceResult")
        ck(_resolve({"a": ["$s1.data.value"]}, {"s1"},
                    os.path.join(root, "flow_runs", "r1")) == {"a": [7]},
           "_resolve：嵌套引用")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("flowops 自测 %d/%d 通过" % (n - len(fails), n))
    for f in fails:
        print("  FAIL", f)
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
