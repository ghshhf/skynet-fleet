"""多控制面仲裁：一个意图绝不割裂（两台机器都起了 Commander 时，谁说了算）。

问题
----
「天网」的前提是**一条意图扇出所有设备，而不是两个控制面各发各的**。
真实场景很快就会出现：两台 Windows 都跑着 commander.py（用户忘了关一台、
或故意一主一备），节点只会连上其中一台，于是：
  - 用户在主控面 `broadcast 关机`，备控面毫不知情，状态立刻分叉；
  - 她在备控面被批准了某个能力，主控面不认，授权变成两张皮；
  - 最糟：两个控制面**同时**给同一节点下相反指令，节点行为不可预期。

设计（一句话）：**全编队只有一个领袖控制面能发令，其它待机；领袖死了，备用的自动接管。**
- 领袖选举 = 一份共享状态文件里的「声明」（claim）：`{id, rank, since, hb}`。
  rank 高者当领袖（`FLEET_RANK`，默认 100）；并列看谁先声明（since 早）；
  再并列看 id（字典序小）——全序，任何两个控制面对「谁是领袖」的答案一致。
- 续期：领袖每 `CLAIM_INTERVAL` 秒重写自己的 hb；超过 `HB_TTL` 没续期 = 让位。
- 待机控制面：REPL 仍可看（nodes/her/journal 都是本地只读），但**发令类命令一律拒绝**
  并明说领袖是谁——用户不会对着一台哑巴控制面猜「是不是坏了」。
- 单机常态零配置：只有一个控制面时它claim即成领袖，没有任何额外步骤。
- 退出让位：控制面正常退出时 `release()` 主动抹掉自己的声明，接管不等 TTL。

诚实边界（必须写清，否则又是假承诺）
----------------------------------
1. 仲裁是**协同**，不是安全边界。共享文件在多进程下有极小写窗口
   （读-改-写非原子）；真要被伪造声明抢领袖，靠的是文件系统权限，
   不是这个模块。抢到领袖也只能发令，动不了已装节点的凭据。
2. 判死是**时间近似**：领袖硬杀（断电/kill -9）后，备用最坏约
   `HB_TTL + CLAIM_INTERVAL` 才发现并接管。要更快就得靠节点侧双连，
   那是另一条线（node_agent 目前单连，见计划书）。
3. 领袖在位时，备用控制面上已连的节点不受影响（节点连谁是谁的事）；
   仲裁保证的是**人的意图只有一个出口**。

用法
----
  python arbiter.py --selftest                 # 离线自测
  FLEET_RANK=200 python commander.py ...       # 这台优先当领袖
  FLEET_ARBITER=/mnt/fleet/arbiter.json ...    # 自定义共享状态文件位置
"""
import json
import os
import sys
import tempfile
import time

HB_TTL = 45.0            # 声明有效期：超过没续期即视为让位
CLAIM_INTERVAL = 15.0    # 续期间隔（也是发现领袖死亡的轮询粒度）
DEFAULT_RANK = 100


class ArbiterError(Exception):
    pass


def default_path():
    """共享状态文件：显式 FLEET_ARBITER > ~/.fleet/arbiter.json。"""
    p = os.environ.get("FLEET_ARBITER", "")
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".fleet", "arbiter.json")


def elect(claims, now=None, ttl=HB_TTL):
    """纯函数：在有效声明里选出领袖。返回 claim dict 或 None（无人在位）。

    排序键：rank 降序 → since 升序 → id 升序。全序保证两控制面答案一致。
    """
    now = time.time() if now is None else now
    live = [c for c in (claims or []) if now - float(c.get("hb", 0) or 0) <= ttl]
    if not live:
        return None
    live.sort(key=lambda c: (-int(c.get("rank", 0) or 0),
                             int(c.get("since", 0) or 0),
                             str(c.get("id", ""))))
    return live[0]


def load_claims(path):
    """读声明列表；文件不存在/损坏 -> []（不崩，选举自然从零开始）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    claims = d.get("claims") if isinstance(d, dict) else None
    return [c for c in claims if isinstance(c, dict)] if claims else []


def _save(path, claims):
    """原子替换写盘（tmp + os.replace），避免半份 JSON 被对端读到。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".arbiter_", dir=os.path.dirname(os.path.abspath(path)) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "claims": claims}, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def claim(path, my_id, rank=DEFAULT_RANK, now=None):
    """续期/首次声明。返回 {"leader": bool, "leader": claim|None}。

    同一 id 重复 claim 只刷新 hb/rank，**since 保持首次声明时间**
    （否则领袖每续期一次就把「谁先来」的次序往后推，并列时会永远赢）。
    """
    now = time.time() if now is None else now
    with _file_lock(path):
        claims = load_claims(path)
        mine = next((c for c in claims if c.get("id") == my_id), None)
        if mine is None:
            mine = {"id": my_id, "since": int(now)}
            claims.append(mine)
        mine["rank"] = int(rank)
        mine["hb"] = int(now)
        # 清掉过期声明，文件不无限涨
        claims = [c for c in claims if now - float(c.get("hb", 0) or 0) <= HB_TTL]
        _save(path, claims)
        leader = elect(claims, now=now)
    return {"is_leader": bool(leader and leader.get("id") == my_id),
            "leader": leader}


def release(path, my_id):
    """主动让位（正常退出用）：抹掉自己的声明，返回是否抹掉了。"""
    with _file_lock(path):
        claims = load_claims(path)
        left = [c for c in claims if c.get("id") != my_id]
        if len(left) != len(claims):
            _save(path, left)
            return True
    return False


def status(path):
    """当前状态（REPL `arbiter` 用）：领袖 + 全部在册声明。"""
    claims = load_claims(path)
    leader = elect(claims)
    return {"leader": leader, "claims": claims}


# ---- 跨进程/跨线程写互斥（尽力而为，见 _FileLock 注释） ----


class _FileLock:
    """尽力而为的跨进程锁（O_CREATE|O_EXCL 占位文件 + 陈旧锁回收）。

    为什么不用 fcntl/flock：Windows 没有。这个锁只防**同一台机器**上的
    并发写；不同机器各写各的共享盘时窗口仍在（见模块 docstring 诚实边界）。
    """

    def __init__(self, path):
        self.path = path + ".lock"
        self.fd = None

    def __enter__(self):
        import errno
        deadline = time.time() + 5.0
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # 陈旧锁（持锁进程早死）：超过 10s 的锁文件直接抢
                try:
                    if time.time() - os.path.getmtime(self.path) > 10.0:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
            if time.time() > deadline:
                # 抢不到锁也别卡死发令路：退回无锁写（极小窗口，见诚实边界）
                return self
            time.sleep(0.05)

    def __exit__(self, *a):
        if self.fd is not None:
            try:
                os.close(self.fd)
                os.remove(self.path)
            except OSError:
                pass
            self.fd = None


def _file_lock(path):
    """每次调用返回一把锁（配合 with 使用）。同进程多线程也走它——
    O_EXCL 占位文件对线程同样互斥，claim 又是毫秒级操作，不会误触陈旧回收。"""
    return _FileLock(path)


# ---------------------------------------------------------------- 自测

def _selftest():
    tdir = tempfile.mkdtemp(prefix="fleet_arb_st_")
    path = os.path.join(tdir, "arbiter.json")

    # ① 无人声明 -> 无领袖
    assert elect([]) is None
    assert elect(load_claims(path)) is None

    # ② 单个声明即成领袖
    r = claim(path, "cmd-A", rank=100)
    assert r["is_leader"] and r["leader"]["id"] == "cmd-A", r

    # ③ rank 高者赢：B 声明 rank 200 -> B 领袖、A 待机
    r = claim(path, "cmd-B", rank=200)
    assert r["is_leader"] and r["leader"]["id"] == "cmd-B", r
    assert elect(load_claims(path))["id"] == "cmd-B", "rank 高者应当选"
    rA = claim(path, "cmd-A", rank=100)
    assert not rA["is_leader"], "A 应待机"

    # ④ 并列 rank：先声明者赢（since 保护：重复 claim 不刷新 since）
    claim(path, "cmd-C", rank=200)
    assert elect(load_claims(path))["id"] == "cmd-B", "同 rank 先声明者应留任"
    claim(path, "cmd-B", rank=200)   # B 续期不改 since
    assert elect(load_claims(path))["id"] == "cmd-B", "续期不得改变先后次序"

    # ⑤ 领袖过期（心跳停）-> 备用自动接管（把 B 的心跳往前推过期，不必真等 45s）
    claims = load_claims(path)
    aged = [dict(c, hb=float(c["hb"]) - HB_TTL - 5) if c["id"] == "cmd-B" else c
            for c in claims]
    assert elect(aged)["id"] == "cmd-C", "B 过期后同 rank 的 C 应接管"
    assert elect(aged)["id"] != "cmd-B", "过期领袖不得留任"

    # ⑥ 释放让位：B 退出后 C 立即成为领袖（不等 TTL）
    assert release(path, "cmd-B") is True
    assert release(path, "cmd-B") is False, "重复释放应报无操作"
    assert elect(load_claims(path))["id"] == "cmd-C"

    # ⑦ 文件损坏不当崩溃，从零开始
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert load_claims(path) == []
    assert elect(load_claims(path)) is None

    # ⑧ 原子写：不留临时文件残渣
    leftovers = [p for p in os.listdir(tdir) if p.startswith(".arbiter_")]
    assert not leftovers, leftovers

    print("[selftest] 无声明->无领袖 OK")
    print("[selftest] 单声明即成领袖（零配置） OK")
    print("[selftest] rank 高者当选 OK")
    print("[selftest] 并列 rank 先声明者留任（续期不改 since） OK")
    print("[selftest] 领袖过期备用接管（时间注入） OK")
    print("[selftest] 主动让位不等 TTL OK")
    print("[selftest] 坏档不崩、选举从零开始 OK")
    print("[selftest] 原子写无残渣 OK")
    print("[selftest] PASS")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    print(__doc__)


if __name__ == "__main__":
    main()
