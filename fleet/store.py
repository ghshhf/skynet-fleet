"""天网 · 状态落盘层（唯一正确写法；调用点不要再自己拼 tmp + replace）

为什么有这个文件
----------------
2026-11-03 盘点全仓：**23 处**各自实现「`tmp = path + ".tmp"` → 写 → `os.replace`」。
写法看着都对，实际有两个真缺陷，而且坏得很安静：

  1) **共享临时名**。两个写者同时写同一个状态文件时，后开的 `open(tmp, "w")` 会把
     先写的半截**截断**，两边交错写出的内容既不是 A 也不是 B，却仍可能是一段合法
     JSON —— 实测（`evals/t74_durable.py` 第一组）：交错写出的文件 `who="B"`、
     条数却是 A 的一半，解析不报错，人也不会发现。

  2) **没有 fsync**。`os.replace` 只保证目录项替换，数据还在页缓存里。断电/蓝屏后
     可能得到零长度或半截文件，而且替换掉的是原本好使的那一份。

正确的原子写是五步，缺一不可：
    唯一临时名（mkstemp）→ 写入 → flush → fsync(文件) → os.replace → fsync(目录)

这套纪律的来源是 CodeAssist（tyron12233/CodeAssist，GPL-3.0，Android 上的 IDE）公开
写明的设计目标：
    "The process can be killed at any time, so persistence is durable
     (atomic write-and-rename) and the model loads lazily."
**只取行为与纪律，不取代码**——本文件是零依赖的 Python 重写。

分级持久性（fsync 不是越多越好）
------------------------------
- `fsync=True`（默认）：低频、关键状态（配置、索引、分类结果、库清单）。
- `fsync=False`：高频刷盘（爬虫每 20 条/每 5 秒一次的落盘）。唯一临时名照样用，
  防交错；但不 fsync —— 真要断电，最多丢几秒的数据，下一次 flush 会重写。
  用 true 换那份确定性，值得；用 true 换吞吐，不值得。

用法
----
    from fleet import store
    store.atomic_write_json("data/x.json", obj)                  # 关键状态
    store.atomic_write_json("data/hot.json", obj, fsync=False)    # 高频刷盘
    store.atomic_write_text("logs/a.txt", "…")
    obj = store.read_json("data/x.json", default={})              # 坏文件给结构化错误
    if store.write_json_if_changed(p, obj): ...                   # 没变就不碰盘（不刷 mtime）

CLI：
    python -m fleet.store --selftest
"""
import errno
import json
import os
import tempfile
import time

__all__ = [
    "StoreError", "atomic_write_bytes", "atomic_write_text", "atomic_write_json",
    "write_json_if_changed", "read_json", "read_text", "exists",
]

_TMP_PREFIX = ".fleet-tmp-"
_MISSING = object()


class StoreError(RuntimeError):
    """落盘/读取失败。带路径，别让调用方去猜是哪个文件。"""


# ------------------------------------------------------------------ 内部

def _fsync_dir(d):
    """POSIX：目录项替换本身也要落盘，否则 rename 可能断电后丢失。

    Windows 打不开目录句柄做 fsync，**如实跳过**——不假装做了（NTFS 的目录项
    日志另有保证，但那是文件系统的事，不是我们做了）。
    """
    if os.name == "nt":
        return False
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _prepare(path):
    p = os.path.abspath(path)
    d = os.path.dirname(p) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise StoreError("建目录失败 %s: %s" % (d, e)) from e
    return p, d


def _replace_with_retry(tmp, p, tries=60):
    """`os.replace` 换成重试版。

    Windows 上 MoveFileEx 在目标文件正被别的进程/线程打开时会瞬时失败
    （WinError 5 拒绝访问 / 32 被占用）——这不是写坏了，是抢用。等到别人松手
    就能成，所以短退避重试；真正持续占用（杀不掉的文件锁）才最终失败。
    第一次就成的情况开销只有一次 stat，可以忽略。
    """
    delay = 0.002
    last = None
    for i in range(tries):
        try:
            os.replace(tmp, p)
            return
        except OSError as e:
            last = e
            if os.name != "nt" and e.errno not in (errno.EBUSY, errno.EACCES, errno.EPERM):
                raise
            if i == tries - 1:
                break
            time.sleep(delay)
            delay = min(delay * 1.5, 0.05)
    raise last


def _tmp_in(d):
    """唯一临时名。**绝不**用 path + ".tmp"——那正是并发交错的根源。"""
    fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=".part", dir=d)
    return fd, tmp


# ------------------------------------------------------------------ 写

def atomic_write_bytes(path, data, perm=None, fsync=True):
    """原子写字节。成功返回最终路径；失败抛 StoreError 且目标文件保持原样。"""
    p, d = _prepare(path)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise StoreError("atomic_write_bytes 只收 bytes，收到 %s" % type(data).__name__)
    fd, tmp = _tmp_in(d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(data))
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if perm is not None:
            try:
                os.chmod(tmp, perm)
            except OSError:
                pass
        _replace_with_retry(tmp, p)
    except BaseException as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(e, StoreError):
            raise
        raise StoreError("写 %s 失败: %s" % (p, e)) from e
    if fsync:
        _fsync_dir(d)
    return p


def atomic_write_text(path, text, perm=None, fsync=True, newline="\n"):
    """原子写文本（UTF-8，换行统一成 newline）。"""
    if not isinstance(text, str):
        raise StoreError("atomic_write_text 只收 str，收到 %s" % type(text).__name__)
    if newline and newline != "\n":
        text = text.replace("\r\n", "\n").replace("\n", newline)
    return atomic_write_bytes(path, text.encode("utf-8"), perm=perm, fsync=fsync)


def dumps_json(obj, indent=2, ensure_ascii=False, sort_keys=False, allow_nan=False):
    """统一的 JSON 序列化口径：中文不转义、NaN 拒写（NaN 不是合法 JSON）。"""
    try:
        return json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent,
                          sort_keys=sort_keys, allow_nan=allow_nan)
    except (TypeError, ValueError) as e:
        raise StoreError("对象无法序列化成 JSON: %s" % e) from e


def atomic_write_json(path, obj, indent=2, ensure_ascii=False, sort_keys=False,
                      perm=None, fsync=True, trailing_newline=False):
    """原子写 JSON。返回最终路径。"""
    s = dumps_json(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys)
    if trailing_newline:
        s += "\n"
    return atomic_write_text(path, s, perm=perm, fsync=fsync)


def write_json_if_changed(path, obj, **kw):
    """内容没变就不碰盘：返回 True = 真写了，False = 内容一致跳过。

    好处不只是省 IO：不刷 mtime，文件监视/增量构建不会被假变更唤醒。
    """
    s = dumps_json(obj, indent=kw.get("indent", 2),
                   ensure_ascii=kw.get("ensure_ascii", False),
                   sort_keys=kw.get("sort_keys", False))
    if kw.get("trailing_newline"):
        s += "\n"
    old = read_text(path, default=None)
    if old == s:
        return False
    atomic_write_text(path, s, perm=kw.get("perm"), fsync=kw.get("fsync", True))
    return True


# ------------------------------------------------------------------ 读

def exists(path):
    return os.path.isfile(path)


def read_text(path, default=_MISSING, encoding="utf-8"):
    try:
        with open(path, "r", encoding=encoding, errors="strict") as f:
            return f.read()
    except FileNotFoundError:
        if default is not _MISSING:
            return default
        raise StoreError("文件不存在: %s" % path)
    except UnicodeDecodeError as e:
        raise StoreError("%s 不是合法 UTF-8: %s" % (path, e)) from e
    except OSError as e:
        raise StoreError("读 %s 失败: %s" % (path, e)) from e


def read_json(path, default=_MISSING, encoding="utf-8"):
    """读 JSON。不存在给 default（没给就抛）；坏文件抛 StoreError 并指出行列。"""
    try:
        raw = read_text(path, default=_MISSING, encoding=encoding)
    except StoreError:
        if default is not _MISSING:
            return default
        raise
    if raw == "":
        if default is not _MISSING:
            return default
        raise StoreError("%s 是空文件（可能上次写盘被打断）" % path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if default is not _MISSING:
            return default
        raise StoreError(
            "%s 不是合法 JSON：第 %d 行第 %d 列 %s（长度 %d 字节）"
            % (path, e.lineno, e.colno, e.msg, len(raw))
        ) from e


# ------------------------------------------------------------------ 自测

def _selftest():
    import shutil
    import threading

    ok = fail = 0

    def chk(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  x %s" % msg)

    d = tempfile.mkdtemp(prefix="store-selftest-")
    try:
        p = os.path.join(d, "a", "b", "x.json")     # 目录不存在
        atomic_write_json(p, {"k": "中文", "n": [1, 2, 3]})
        chk(read_json(p)["k"] == "中文", "中文往返不一致")
        chk(not [n for n in os.listdir(os.path.dirname(p)) if _TMP_PREFIX in n],
            "临时文件有残留")

        chk(write_json_if_changed(p, {"k": "中文", "n": [1, 2, 3]}) is False, "内容没变却写了")
        chk(write_json_if_changed(p, {"k": "中文", "n": [1, 2, 4]}) is True, "内容变了却跳过")

        # 并发：唯一临时名 ⇒ 最终必是某一个完整 payload
        payloads = [{"who": i, "data": list(range(200))} for i in range(6)]
        ts = [threading.Thread(target=atomic_write_json, args=(p, x)) for x in payloads]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        got = read_json(p)
        chk(got in payloads, "并发写后不是任何一个完整 payload（说明两次写被拼在了一起）")
        chk(not [n for n in os.listdir(os.path.dirname(p)) if _TMP_PREFIX in n],
            "并发后临时文件有残留")

        # 坏文件：结构化错误，不吞
        atomic_write_text(p, "{ 坏的")
        try:
            read_json(p)
            chk(False, "坏 JSON 应该抛 StoreError")
        except StoreError as e:
            chk("第 1 行" in str(e), "坏 JSON 的报错没带行列：%s" % e)
        chk(read_json(p, default={"d": 1}) == {"d": 1}, "有 default 时坏文件应回落")

        print("*** store selftest: %d ok / %d fail ***" % (ok, fail))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return fail == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
