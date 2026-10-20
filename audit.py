"""控制面审计：append-only JSONL，让「统一调用面」的每个动作都留痕（可观测 / 可追责）。

设计取第四原则之形——**不在 agent 里做运行时判断**，只在「动作发生的边界」写一条流水；
因此不改任何控制流、不引入分支 bug，纯粹旁路记录。

记什么：节点上/下线、执行与扇出、文件推拉、能力授权/拒绝、一次性令牌、AI 路由、
她的主动开口、算力路由……（凡经 Commander 的统一调用面动作）。

用法：
  import audit
  audit.log("her.grant", node="Her-Phone", cap="phone.screen.on")
  audit.tail(20)          # 看最近 20 条（REPL: audit [n]）
  audit.export()          # 审计文件路径（可拷走/归档）
"""
import json
import os
import threading
import time

_PATH = os.environ.get("FLEET_AUDIT", "fleet_audit.jsonl")
_LK = threading.Lock()
_MAX_TAIL = 5000


def configure(path):
    """改审计落点（自检用临时文件，避免污染真实流水）。"""
    global _PATH
    _PATH = path
    return _PATH


def path():
    return _PATH


def log(kind, **fields):
    """写一条审计流水。失败只吞掉，绝不影响控制面动作本身。"""
    rec = {"ts": round(time.time(), 3), "kind": kind}
    rec.update(fields)
    try:
        with _LK:
            with open(_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec


def tail(n=20):
    """读最近 n 条（新→旧顺序无关，这里保持时序返回）。"""
    if not os.path.exists(_PATH):
        return []
    try:
        with _LK:
            lines = open(_PATH, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def count():
    if not os.path.exists(_PATH):
        return 0
    try:
        with _LK:
            with open(_PATH, encoding="utf-8") as f:
                return sum(1 for _ in f)
    except OSError:
        return 0


def export(dest=None):
    """把审计流水拷贝一份出来归档（默认原地返回路径）。"""
    if dest:
        try:
            with _LK:
                data = open(_PATH, encoding="utf-8").read()
            open(dest, "w", encoding="utf-8").write(data)
            return dest
        except OSError:
            return None
    return _PATH


if __name__ == "__main__":
    audit_path = configure("/tmp/fleet_audit_demo.jsonl")
    log("demo", note="自测一条")
    print("审计文件:", audit_path)
    print("最近:", tail(5))
