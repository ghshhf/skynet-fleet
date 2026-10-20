"""离线队列持久化：Commander 重启不丢排队命令。

为什么需要：Phase 4 的承诺是「上下线对你透明」——节点离线时命令进队列，
上线自动补。但队列若只在内存，控制面一重启就全丢，这个承诺就是假的。

存储：单个 JSON 文件（FLEET_QUEUE 覆盖路径），写盘用临时文件 + os.replace 原子替换，
避免半写坏档。纯 stdlib。
"""
import json
import os

DEFAULT_PATH = os.environ.get("FLEET_QUEUE", "fleet_queue.json")


def save(queue, path=None):
    """把 {node: [ {id, cmd}, ... ]} 落盘（原子替换）。"""
    p = path or DEFAULT_PATH
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def load(path=None):
    """读回队列。文件不存在/损坏都返回空 dict（宁可丢队列，不可让控制面起不来）。"""
    p = path or DEFAULT_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError):
        return {}


def pending_count(queue):
    return sum(len(v) for v in queue.values())


def _selftest():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    try:
        q = {"PC-Ghost": [{"id": "q1", "cmd": "echo hi"}],
             "PC-Off": [{"id": "q2", "cmd": "hostname"}, {"id": "q3", "cmd": "whoami"}]}
        save(q, path)
        back = load(path)
        assert back == q, back
        assert pending_count(back) == 3, pending_count(back)

        # 模拟「重启后」：从磁盘重建，条目与顺序都不变
        assert load(path)["PC-Off"][1]["cmd"] == "whoami"

        # 空队列/缺文件都不炸
        save({}, path)
        assert load(path) == {}
        os.remove(path)
        assert load(path) == {}, "缺文件应返回空"

        # 坏档不被静默忽略成崩溃
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load(path) == {}, "坏档应返回空"

        print("[selftest] 入队->落盘->读回一致 OK")
        print("[selftest] 重启后条目与顺序保持 OK")
        print("[selftest] 空队列/缺文件/坏档 均不崩 OK")
        print("[selftest] PASS")
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
