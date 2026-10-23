"""审批中枢（W2）：把「要不要审批」这件事接在注册表的闸门上。

为什么判定权必须在注册表层：
    `Registry.call` 已经算好了「这项能力要不要审批、审批几级」
    （side_effect / approval=ask / destructive → level 1/2），并且**只在**
    需要审批时才会去调闸门 `gate(cap, dev, args, level)`。
    所以本模块**不做判定**，只做「登记 + 等待 + 送达」三件事：
    闸门被调用 ⇒ 注册表已经判定「这项要审批」⇒ 登记一条待审批记录，
    然后把这一次调用挂起，直到主人点确认 / 点拒绝 / 等到超时。

为什么不是让 UI 自己判：
    UI 换个前端、手敲一条 HTTP，都能自己决定「这个我不问主人了」。
    判定写在注册表里，UI 绕不过去。

为什么一次性（授权用掉即失效）：
    一次授权只配**当前这一条正在等的调用**。它通过一次性 Event 送达，
    送达后请求立刻入「已结束」账本：同一个 request_id 第二次回应 → 拒绝；
    换一个会话（另一个 broker）拿同一个 request_id 来回应 → 也拒绝。
    没有可以复制的「授权凭据」流出去，所以谈不上重放。

超时 = 拒绝：
    等不到人就是没人授权（fail-closed）。理由写清「审批超时未确认」，
    不许静默放行，也不许把超时说成失败以外的东西。
"""
import threading
import time
import uuid

#: 默认等待主人确认的秒数。超了就拒绝。
#:
#: 原来是 120 秒。实测（无窗口的 --link 模式下主人根本不在电脑前）问一句
#: 「帮我打开微信」要干等两分钟才等来一句「超时没确认」——这期间界面上
#: 什么都没有，看着像卡死。**没人点就是拒绝，早说比晚说好**，压到 30 秒。
#: 真需要人工确认的场景（有窗口开着）也够用：弹窗一直在，点一下即可。
DEFAULT_TIMEOUT = 30.0

#: 「已结束」账本最多留多少条（防无限增长；留一点点是为了能说清「这 id 早就结束了」）。
CLOSED_LEDGER_MAX = 256


class ApprovalError(Exception):
    """审批中枢自己拒绝了一次操作（消息就是给主人看的原因）。"""


class ApprovalRequest:
    """一条待审批的记录。

    它是**一次调用**的凭证，不是「以后都能用」的授权：
    主人点一次确认，只让这一次调用过去。
    """

    __slots__ = ("request_id", "session", "cap", "title", "device", "level",
                 "args_preview", "tag", "created_at", "deadline", "event",
                 "decision", "reason", "destructive")

    def __init__(self, request_id, session, cap, title, device, level,
                 args_preview, tag, created_at, deadline, destructive=False):
        self.request_id = request_id
        self.session = session
        self.cap = cap
        self.title = title
        self.device = device
        self.level = int(level or 1)
        self.args_preview = args_preview
        self.tag = tag
        self.created_at = created_at
        self.deadline = deadline
        self.destructive = bool(destructive)
        self.event = threading.Event()
        self.decision = ""          # "approved" / "denied" / ""
        self.reason = ""

    def as_dict(self, now=None):
        now = time.time() if now is None else now
        return {
            "request_id": self.request_id,
            "cap": self.cap,
            "title": self.title,
            "device": self.device,
            "level": self.level,
            "destructive": self.destructive,
            "args_preview": self.args_preview,
            "tag": self.tag,
            "created_at": self.created_at,
            "expires_in": max(0.0, round(self.deadline - now, 1)),
            "state": self.decision or "pending",
        }


def _preview(args, limit=400):
    """把参数缩成一行给人看（审批弹窗里要显示「要干什么」）。

    只截断、不脱敏：参数里出现凭据是设备侧的问题，而这里显示的是**本机窗口**
    里的内容（token 都不出进程）。但仍然截断——太长的一行会把弹窗撑破。
    """
    if not args:
        return ""
    try:
        s = ", ".join("%s=%s" % (k, v) for k, v in sorted(args.items()))
    except Exception:
        return "(参数无法显示)"
    return s[:limit] + ("…" if len(s) > limit else "")


class ApprovalBroker:
    """审批中枢：登记待审批、等主人回应、把回应一次性送到正在等的那次调用。

    线程安全：所有状态变更都在 `self._lk` 里；等待发生在锁外。
    并发：每次调用各有各的 Event，A 在等不影响 B 在等。
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT, session=None):
        self.timeout = float(timeout)
        # 会话标识：审批记录属于创建它的会话，别的会话拿着同一个 request_id
        # 也回不来（不认）。
        self.session = session or uuid.uuid4().hex
        self._lk = threading.Lock()
        self._pending = {}            # request_id -> ApprovalRequest
        self._closed = {}             # request_id -> 结束原因（防重放）
        self._closed_order = []

    # ---------------- 对注册表暴露的闸门 ----------------

    def gate_for(self, tag=None, session=None):
        """给 `registry.call` 用的闸门闭包。

        `tag` 是调用方的自标签（UI 用来把这条待审批认领回自己的那次调用），
        `session` 是调用方所属的客户端会话（用来拒绝跨会话重放）。
        两者**只用于回显与归属，不参与判定**。
        """
        broker = self

        def _gate(cap, dev, args, level):
            return broker._await(broker._create(cap, dev, args, level, tag, session))

        return _gate

    def _create(self, cap, dev, args, level, tag, session=None):
        now = time.time()
        cap_name = getattr(cap, "name", str(cap))
        title = getattr(cap, "title", "") or cap_name
        device = getattr(dev, "name", "") or ""
        req = ApprovalRequest(
            request_id=uuid.uuid4().hex,
            session=session or self.session,
            cap=cap_name,
            title=title,
            device=device,
            level=int(level or 1),
            args_preview=_preview(args),
            tag=tag or "",
            created_at=now,
            deadline=now + self.timeout,
            destructive=bool(getattr(cap, "destructive", False)),
        )
        with self._lk:
            self._pending[req.request_id] = req
        return req

    def _await(self, req):
        """挂起这一次调用，等主人的回应。返回 (放行?, 理由)。"""
        # 等待发生在锁外：等的时候不占锁，别的请求照常进来。
        signalled = req.event.wait(self.timeout)
        with self._lk:
            # 判定用 decision（不是 event 是否送达）：超时那一刻可能正好有人
            # 点了确认——那种情况下「有人授权过」比「超时了」更真。
            if req.decision:
                ok = (req.decision == "approved")
                reason = req.reason or ("" if ok else "主人拒绝了这次操作")
                self._close(req.request_id, req.decision)
                return ok, reason
            # 没人表态 = 没人授权 = 拒绝（fail-closed）。
            if req.request_id in self._pending:
                del self._pending[req.request_id]
            reason = req.reason or (
                "审批超时未确认（等了 %.0f 秒没有人点确认，这次就不做）" % self.timeout)
            self._close(req.request_id, "timeout")
            return False, reason

    def _close(self, request_id, how):
        """记一笔「这条已经结束了」，用于拒绝重放。"""
        if request_id in self._closed:
            self._closed[request_id] = how
            return
        self._closed[request_id] = how
        self._closed_order.append(request_id)
        while len(self._closed_order) > CLOSED_LEDGER_MAX:
            old = self._closed_order.pop(0)
            self._closed.pop(old, None)

    # ---------------- UI 侧接口 ----------------

    def pending(self):
        """当前在等主人确认的清单（给 UI 显示「哪些能力在等审批」）。"""
        now = time.time()
        with self._lk:
            rows = [r.as_dict(now) for r in self._pending.values()
                    if not r.decision and r.deadline > now]
        rows.sort(key=lambda r: r["created_at"])
        return rows

    def get(self, request_id):
        """取一条待审批记录（响应后立刻就没了——那是一次性的）。"""
        with self._lk:
            return self._pending.get(request_id)

    def respond(self, request_id, approve, reason="", session=None, double=False):
        """主人点了确认/拒绝。

        `session` 是**回应方声称自己所属的会话**。不给时按本中枢的会话算
        （等于「没拿出归属凭据的人」），所以跨会话重放一定对不上。

        返回 (是否送达, 原因)。以下情况一律**不送达**并说明原因：
          · request_id 不存在 → 「没有这条审批请求」
          · 已经结束过（用过/超时/已拒）→ 「这条审批已经结束了」（防重放）
          · 属于别的会话 → 「这条审批不属于当前会话」
          · 破坏性能力没给二次确认 → 「破坏性操作需要二次确认」（双确认）
        """
        claim = session or self.session
        with self._lk:
            req = self._pending.get(request_id)
            if req is None:
                how = self._closed.get(request_id)
                if how == "timeout":
                    return False, "这条审批已经超时结束了（审批超时未确认）"
                if how:
                    return False, "这条审批已经结束了，不能重复回应"
                return False, "没有这条审批请求（编号不认识）"
            if claim != req.session:
                return False, "这条审批不属于当前会话"
            if req.decision:
                return False, "这条审批已经结束了，不能重复回应"
            # 破坏性能力双确认：不可逆的动作不许一点就过。
            # 这一条也在**审批中枢**里判，不靠 UI 自觉——UI 少传一个字段，
            # 这里就拒绝，前端绕不过去。
            if approve and req.destructive and not double:
                return False, "破坏性操作需要二次确认（double=true）：不可逆的动作不能一点就过"
            req.decision = "approved" if approve else "denied"
            req.reason = reason or ("" if approve else "主人拒绝了这次操作")
            req.event.set()
            # 从 pending 摘掉：它已经不会再被第二次回应了。
            del self._pending[request_id]
            self._close(request_id, req.decision)
        return True, ""
