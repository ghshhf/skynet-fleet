"""容量闸：管「同时能跑几个角色」和「能往下派几层」——**两个独立的闸**。

为什么抄这个（学 DeepSeek Harness 的第 1 条机制）：

  Harness 把「深度」和「容量」拆成两个互不相干的闸：
    maxDepth            管**树能长多高**（深度 0 = 禁止委派）
    maxActiveSubagents  管**同时铺多宽**

  本仓照抄这两闸的**语义**，但把默认容量定成 **4** 而不是 Harness 的 8——
  理由是本机的真实约束：16GB 内存、9 个并发子智能体累计 19MB 上下文、
  多个角色打**同一个免费端点**（127.0.0.1:8787）互相抢额度。照抄 8 会翻车。

四条容易被想当然搞错的语义（Harness 官方明确写了）：

  1. **根父级不占名额**。容量数的是「被激活的子角色」，编排者自己不算。
  2. **本地与外部后代共享同一个容量池**。本仓的「本地角色」和「远程角色」
     不是两拨人，是同一拨。
  3. **创建前预留，完整释放后才归还**。等待中的 / 有待处理收件箱的 /
     正在停止的 activation **仍然占着名额**——它还没真正结束。
  4. **容量耗尽时不排队**，直接失败。官方理由很硬：排队会让「等待后代的父代理」
     去等「自己占着的名额」，互相死等。

本仓额外加一条（对应 Harness「调高上限立即生效、调低只拒绝新接纳」）：
  调低容量时，**已在跑的不杀**，只拒绝新的预留。已占的名额超过新上限也不报错。
"""
import threading


class CapacityError(Exception):
    """容量或深度闸拒绝了一次创建。message 必须写清「为什么 + 差多少」。"""

    def __init__(self, message, kind="capacity", need=0):
        super().__init__(message)
        self.kind = kind        # "capacity" | "depth"
        self.need = need        # 容量满时还差几个名额


class Lease:
    """一次预留的凭据。**必须**在角色真正结束后 release，名额才归还。"""

    def __init__(self, gate, slot_id, role, depth, provider=""):
        self._gate = gate
        self.slot_id = slot_id
        self.role = role
        self.depth = depth
        self.provider = provider
        self.state = "reserved"   # reserved -> running -> stopping -> released
        self._released = False

    def start(self):
        """标记为「已在跑」。跑起来但还没结束，仍占名额。"""
        if self.state == "reserved":
            self.state = "running"
        return self

    def stop(self):
        """标记为「正在停止」。**仍占名额**——它还没真正结束。"""
        if self.state in ("reserved", "running"):
            self.state = "stopping"
        return self

    def release(self):
        """真正结束，归还名额。可重复调用（第二次是空操作）。"""
        if not self._released:
            self._released = True
            self.state = "released"
            self._gate._free(self)
        return self

    @property
    def released(self):
        return self._released

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class CapacityGate:
    """共享容量池 + 深度锁。

    用法：
        gate = CapacityGate(capacity=4, max_depth=1)
        lease = gate.reserve("researcher", depth=1, provider="local-gateway")
        try:
            ...跑...
        finally:
            lease.release()      # 名额在这里才归还
    """

    def __init__(self, capacity=4, max_depth=1):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"capacity 必须是 >=1 的整数，实际是 {capacity!r}")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise ValueError(f"max_depth 必须是 >=0 的整数，实际是 {max_depth!r}")
        self.capacity = capacity
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self._slots = {}          # slot_id -> Lease
        self._seq = 0

    # ---------- 容量 ----------

    @property
    def in_use(self):
        """当前占用的名额数。"""
        with self._lock:
            return len(self._slots)

    @property
    def free(self):
        with self._lock:
            return max(0, self.capacity - len(self._slots))

    def active(self):
        """当前占着名额的角色清单（按 slot_id 排序，输出稳定）。"""
        with self._lock:
            return [
                {"slot_id": k, "role": v.role, "depth": v.depth,
                 "provider": v.provider, "state": v.state}
                for k, v in sorted(self._slots.items())
            ]

    def reserve(self, role, depth=1, provider="", slot_id=None):
        """预留一个名额。**满了立刻失败，绝不排队。**

        depth 语义：根编排器是 0，它直接创建的子角色是 1。
        """
        role = role or "unnamed"
        with self._lock:
            # 深度闸先判——超深度是「逻辑错误」，比容量满更该先说
            if depth > self.max_depth:
                raise CapacityError(
                    f"深度超限：{role} 要在第 {depth} 层被创建，"
                    f"本闸深度上限是 {self.max_depth}（深度 0 = 根编排者，不占名额）",
                    kind="depth")
            if depth < 0:
                raise CapacityError(f"深度不能是负数：{depth}", kind="depth")

            if len(self._slots) >= self.capacity:
                need = 1
                raise CapacityError(
                    f"容量已满：{role} 没拿到名额（已占 {len(self._slots)}/{self.capacity}，"
                    f"还差 {need} 个）。不排队——排队会让等待的父代理去等自己占着的名额",
                    kind="capacity", need=need)

            self._seq += 1
            sid = slot_id or f"{self._seq:03d}-{role}"
            lease = Lease(self, sid, role, depth, provider)
            self._slots[sid] = lease
            return lease

    def _free(self, lease):
        with self._lock:
            if self._slots.get(lease.slot_id) is lease:
                del self._slots[lease.slot_id]

    # ---------- 容量调整 ----------

    def set_capacity(self, n):
        """调容量。**调高立即生效；调低只影响新的预留，已在跑的不动。**"""
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"capacity 必须是 >=1 的整数，实际是 {n!r}")
        with self._lock:
            self.capacity = n
        return self.capacity

    def set_max_depth(self, n):
        """调深度上限。同样是「调高立即生效、调低不杀已在跑的」。"""
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"max_depth 必须是 >=0 的整数，实际是 {n!r}")
        with self._lock:
            self.max_depth = n
        return self.max_depth

    def snapshot(self):
        with self._lock:
            return {"capacity": self.capacity, "in_use": len(self._slots),
                    "free": max(0, self.capacity - len(self._slots)),
                    "max_depth": self.max_depth,
                    "active": self.active()}


def _selftest():
    # 1. 根不占名额；第一个子级能拿到
    g = CapacityGate(capacity=2, max_depth=1)
    assert g.in_use == 0
    l1 = g.reserve("researcher", depth=1)
    assert g.in_use == 1 and g.free == 1

    # 2. 深度超限要报「这是第几层」
    try:
        g.reserve("too-deep", depth=2)
        raise AssertionError("深度超限没报错")
    except CapacityError as e:
        assert e.kind == "depth" and "第 2 层" in str(e), str(e)

    # 3. 满则 fail-fast，不排队
    l2 = g.reserve("critic", depth=1)
    try:
        g.reserve("editor", depth=1)
        raise AssertionError("容量满了没报错")
    except CapacityError as e:
        assert e.kind == "capacity" and e.need == 1, str(e)
    assert g.in_use == 2

    # 4. 释放一个才归还名额（预留即占，不因「拿到 lease」就多占）
    l1.release()
    assert g.in_use == 1
    l3 = g.reserve("editor", depth=1)
    assert g.in_use == 2

    # 5. 重复释放不重复归还（不会把池子放飞）
    l2.release()
    l2.release()
    assert g.in_use == 1, g.in_use

    # 6. 调低容量不杀已跑的，只拒绝新的
    g.set_capacity(1)
    assert g.in_use == 1
    try:
        g.reserve("new", depth=1)
        raise AssertionError("调低容量后不该再接纳新的")
    except CapacityError as e:
        assert e.kind == "capacity", str(e)
    l3.release()
    g.set_capacity(4)
    l4 = g.reserve("new", depth=1)
    assert g.in_use == 1

    # 7. 深度 0 = 根编排者，不该由子角色创建
    g2 = CapacityGate(capacity=1, max_depth=0)
    try:
        g2.reserve("child", depth=1)
        raise AssertionError("max_depth=0 时不该准许派生")
    except CapacityError as e:
        assert e.kind == "depth", str(e)

    # 8. lease 状态机：stopping 仍占名额
    g3 = CapacityGate(capacity=1, max_depth=1)
    l = g3.reserve("r", depth=1)
    l.start()
    assert l.state == "running" and g3.in_use == 1
    l.stop()
    assert l.state == "stopping" and g3.in_use == 1, "正在停止仍应占名额"
    l.release()
    assert g3.in_use == 0

    print("capacity selftest OK")
    return True


if __name__ == "__main__":
    _selftest()
