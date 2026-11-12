# -*- coding: utf-8 -*-
"""天网 · 网络观测与协议理解（抓包方向，对应 V1.1 第六节 + 阶段 2/3）

不依赖底层抓包库（npcap / Wireshark），先落地「应用层协议观测」：
任何一次 HTTP 请求，都产出结构化证据（耗时、重定向链、状态码、错误分类），
并能在失败时给出故障诊断。这是比 PCAP 更轻、立刻能跑的第一步。

后续若要真抓包，把采集器换成 npcap / tshark 即可，对上接口不变。

状态机（执行侧）：
    initiated → sent → received → success / failed / unknown_result
    - success     ：拿到响应，业务是否成功是上层的事，这里只确认「通信成了」。
    - failed      ：请求根本没到业务层（DNS/TLS/连接/协议/代理失败）。
    - unknown_result（2026-11-03 新增）：请求已发出、连接已建，但响应未确认。
                  计划书硬规矩——「没收到结果 ≠ 一定失败」，可能是结果未知，
                  此时盲目重试有副作用，必须先查状态。绝不当成 failed 处理。
"""
from . import clock
from . import netdiag  # 诊断模板与 KB（纯模块，无循环依赖）

try:
    import httpx
except ImportError:  # 无 httpx 环境：分类退化为纯文本嗅探
    httpx = None

__all__ = [
    "classify_error", "infer_terminal_state", "RequestTrace",
    "probe_url", "diagnose", "run_diagnosis",
]


# ------------------------------------------------------------------- 错误分类
# 调用方只抛异常进来，我们负责把它归到「故障类别」。证据不足一律标 unknown，
# 绝不臆造原因（这是 V1.1 第六节故障诊断器的硬规矩）。
def classify_error(exc):
    """把一次请求抛出的异常归到故障类别。返回小写类别串。"""
    # httpx 异常层级：HTTPError <- TransportError <- {ConnectError, TimeoutException,
    # RemoteProtocolError, ProxyError, ...}；TimeoutException <- {ConnectTimeout, ReadTimeout}
    # 顺序必须先细后粗。
    if httpx is not None:
        if isinstance(exc, httpx.ConnectTimeout):
            return "timeout_connect"
        if isinstance(exc, httpx.ReadTimeout):
            return "timeout_read"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.ConnectError):
            return "connection"
        if isinstance(exc, httpx.ProxyError):
            return "proxy"
        if isinstance(exc, httpx.RemoteProtocolError):
            return "protocol"
        if isinstance(exc, httpx.UnsupportedProtocol):
            return "protocol"
        if isinstance(exc, httpx.HTTPStatusError):
            return "http_status"
        if isinstance(exc, httpx.HTTPError):
            return "http_error"
    # 非 httpx 异常（例如底层 socket / 平台相关）：按文本嗅探，嗅不到就 unknown
    s = str(exc).lower()
    if "getaddrinfo" in s or "name or service not known" in s or "nodename nor servname" in s:
        return "dns"
    if "certificate" in s or "ssl" in s or "tls" in s:
        return "tls"
    if "connection refused" in s or "connection reset" in s or "network is unreachable" in s:
        return "connection"
    if "timed out" in s or "timeout" in s:
        return "timeout"
    return "unknown"


# ------------------------------------------------------------------- 终态推断
# 这是「结果未知 ≠ 失败」的代码兑现点。given 一个异常 + 它发生在哪个阶段，
# 推断终态是 failed（明确没到业务层）还是 unknown_result（可能到业务层了，只是没确认）。
def infer_terminal_state(exc, sent):
    """sent=True 表示 mark_sent 已调（连接层已发起）。

    判定规则（保守，宁漏勿错标）：
    - timeout_read：连接已建、响应迟迟不来 → 服务端可能已处理 → unknown_result
    - connection 且 sent=True 且非「连接被拒/不可达」：发出之后才断开（reset 等）
      → 请求可能已抵达服务端 → unknown_result
    - connection 但「未发出 / 被拒 / 不可达」：明确没建立，请求没出去 → failed
    - 其余（DNS/TLS/代理/协议/超时未发）→ failed
    """
    cat = classify_error(exc)
    s = str(exc).lower()
    if cat == "timeout_read":
        return "unknown_result"
    if cat == "connection":
        if not sent or "refused" in s or "unreachable" in s or "10061" in s:
            return "failed"          # 连接被拒/不可达：明确没发出去
        # httpx 把「域名解析失败 / TLS 握手失败」也包成 ConnectError；
        # 这类错误发生在请求字节发出之前，必须算 failed，不能算 unknown_result
        # （否则会给一个根本发不出去的请求判「结果未知，别重试」）。
        if any(k in s for k in ("getaddrinfo", "name or service not known",
                                "nodename nor servname", "name resolution",
                                "certificate", "ssl", "tls")):
            return "failed"
        return "unknown_result"      # 已发且中途断开：可能已抵达
    return "failed"


# 状态机：请求生命周期。unknown_result 与 failed 平级，都是「未成功」，但语义不同。
_STATE_FLOW = ("initiated", "sent", "received", "success", "failed", "unknown_result")


class RequestTrace:
    """一次请求的结构化证据。所有时间走 clock（perf_counter），不碰墙钟算时长。"""

    __slots__ = (
        "url", "method", "task_id", "state", "_t0", "_t1",
        "status", "final_url", "redirects", "error", "category",
        "bytes", "headers", "body", "body_truncated",
    )

    def __init__(self, url, method="GET", task_id=None):
        self.url = url
        self.method = method
        self.task_id = task_id
        self.state = "initiated"
        self._t0 = clock.now()
        self._t1 = None
        self.status = None
        self.final_url = None
        self.redirects = []          # [(status_code, url), ...] 取自 response.history
        self.error = None
        self.category = None
        self.bytes = None
        self.headers = None
        self.body = None
        self.body_truncated = False

    def mark_sent(self):
        if self.state == "initiated":
            self.state = "sent"

    def mark_received(self):
        if self.state in ("initiated", "sent"):
            self.state = "received"

    def finish_success(self, resp, body_limit=200000):
        self.mark_received()
        self._t1 = clock.now()
        self.status = resp.status_code
        self.final_url = str(resp.url)
        try:
            self.redirects = [(r.status_code, str(r.url)) for r in resp.history]
        except Exception:
            self.redirects = []
        self.bytes = len(resp.content) if resp.content is not None else 0
        try:
            self.headers = dict(resp.headers)
        except Exception:
            self.headers = {}
        text = resp.text if resp.text is not None else ""
        if len(text) > body_limit:
            self.body = text[:body_limit]
            self.body_truncated = True
        else:
            self.body = text
        self.state = "success"

    def finish_error(self, exc):
        self._t1 = clock.now()
        self.error = str(exc)
        self.category = classify_error(exc)
        sent = self.state in ("sent", "received")
        self.state = infer_terminal_state(exc, sent)

    def dur_ms(self):
        if self._t1 is None:
            return None
        return (self._t1 - self._t0) * 1000.0

    def to_metrics(self):
        """给调用方（爬虫 / 诊断器）的精简指标块。"""
        return {
            "url": self.url,
            "method": self.method,
            "task_id": self.task_id,
            "final_url": self.final_url,
            "status": self.status,
            "state": self.state,
            "dur_ms": round(self.dur_ms(), 3) if self.dur_ms() is not None else None,
            "redirects": self.redirects,
            "redirect_count": len(self.redirects),
            "bytes": self.bytes,
            "category": self.category,
            "error": self.error,
        }

    def to_dict(self):
        d = self.to_metrics()
        d["body_truncated"] = self.body_truncated
        return d


def probe_url(url, method="GET", headers=None, timeout=30,
              follow_redirects=True, body_limit=200000, client=None,
              task_id=None):
    """发一次真实请求并产出 RequestTrace。client 可注入（测试用），否则自动建/关。

    只做观测 + 取首段 body，不做任何解析/落盘——那是上层爬虫 handler 的事。
    """
    import httpx  # 局部导入：本模块可被「无 httpx」环境静态加载（能力注册阶段）

    tr = RequestTrace(url, method, task_id=task_id)
    tr.mark_sent()
    own = client is None
    if own:
        client = httpx.Client(timeout=timeout, follow_redirects=follow_redirects)
    try:
        resp = client.request(method, url, headers=headers or {})
        tr.finish_success(resp, body_limit=body_limit)
    except Exception as e:  # 任何失败都转成观测证据，不向上抛
        tr.finish_error(e)
    finally:
        if own and client is not None:
            try:
                client.close()
            except Exception:
                pass
    return tr


def diagnose(trace):
    """基于 trace 给诊断。返回 {category, reason, confidence, next_steps, evidence, state}。

    诊断模板来自 netdiag 知识库（可持久化、可累积）；未命中类别回落 unknown 模板。
    """
    cat = trace.category or "unknown"
    reason, conf, steps = netdiag.lookup(cat)
    # 把实测证据贴上去，让诊断可被复核，而不是一句结论。
    evidence = []
    if trace.status is not None:
        evidence.append("HTTP %d" % trace.status)
    if trace.redirects:
        evidence.append("%d 次重定向：%s" % (
            len(trace.redirects),
            " -> ".join("%d:%s" % (s, u) for s, u in trace.redirects[:5])))
    if trace.dur_ms() is not None:
        evidence.append("耗时 %.1fms" % trace.dur_ms())
    if trace.error:
        evidence.append("原始异常：%s" % trace.error[:300])
    if trace.state == "success":
        return {
            "category": "ok",
            "reason": "请求成功",
            "confidence": "high",
            "next_steps": [],
            "evidence": evidence,
            "state": trace.state,
        }
    # unknown_result 是高于具体 category 的元语义：无论底层是超时还是断连，
    # 结论都是「结果未知，勿盲目重试」。用 unknown_result 模板的建议覆盖具体分类建议。
    if trace.state == "unknown_result":
        ur_reason, ur_conf, ur_steps = netdiag.lookup("unknown_result")
        reason = "%s；%s" % (reason, ur_reason)
        conf = ur_conf
        steps = list(ur_steps)
    return {
        "category": cat,
        "reason": reason,
        "confidence": conf,
        "next_steps": list(steps),
        "evidence": evidence,
        "state": trace.state,
    }


def run_diagnosis(url, task_id=None, **kw):
    """独立入口：探测一次 + 出诊断报告。供 crawl.diagnose 能力调用。"""
    tr = probe_url(url, task_id=task_id, **kw)
    return {
        "url": url,
        "task_id": task_id,
        "trace": tr.to_dict(),
        "diagnosis": diagnose(tr),
    }
