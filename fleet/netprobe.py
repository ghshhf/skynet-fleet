# -*- coding: utf-8 -*-
"""天网 · 网络观测与协议理解（抓包方向第一版，对应 V1.1 第六节 + 阶段 2/3）

不依赖底层抓包库（npcap / Wireshark），先落地「应用层协议观测」：
任何一次 HTTP 请求，都产出结构化证据（耗时、重定向链、状态码、错误分类），
并能在失败时给出故障诊断。这是比 PCAP 更轻、立刻能跑的第一步。

后续若要真抓包，把采集器换成 npcap / tshark 即可，对上接口不变。
"""
from . import clock

try:
    import httpx
except ImportError:  # 无 httpx 环境：分类退化为纯文本嗅探
    httpx = None

__all__ = [
    "classify_error", "RequestTrace", "probe_url", "diagnose", "run_diagnosis",
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


# ------------------------------------------------------------------- 诊断知识库
# category -> (一句话原因, 置信度 high/med/low, 下一步建议列表)
_DIAGNOSES = {
    "dns": ("DNS 解析失败：域名无法解析为主机地址",
            "high",
            ["核对域名拼写", "本机查 DNS：nslookup <域名> / ipconfig /flushdns",
             "若目标在代理后，确认是否需走特定通道解析"]),
    "connection": ("连接被拒 / 网络不可达：TCP 三次握手未建立",
                   "high",
                   ["确认目标主机在线、端口开放", "检查防火墙 / 代理 / 本机出口",
                    "若为内网地址确认是否在同一网络"]),
    "timeout_connect": ("连接超时：握手在预算内未完成",
                        "med",
                        ["目标响应慢或链路拥塞", "增大 timeout 或走代理重试",
                         "确认不是被限流 / 反爬拦截"]),
    "timeout_read": ("读取超时：连接已建但响应迟迟不来",
                     "med",
                     ["服务端处理慢 / 挂起", "检查是否触发反爬返回空响应",
                      "降低并发或改用分块读取"]),
    "tls": ("TLS / 证书错误：加密握手失败",
            "high",
            ["确认系统根证书完整（本机曾删过 Windows Update 组件，证书库可能不全）",
             "目标是否自签 / 过期", "代理是否做了中间人拦截"]),
    "http_status": ("HTTP 状态码错误（4xx/5xx）",
                    "med",
                    ["4xx = 客户端问题（鉴权 / 参数 / 频率 / 权限）",
                     "5xx = 服务端问题", "务必读响应体定位具体原因，别只看状态码"]),
    "protocol": ("协议错误：响应不符合 HTTP 规范",
                 "med",
                 ["目标可能不是 HTTP 服务", "代理返回了非预期内容",
                  "检查端口 / 协议是否匹配"]),
    "proxy": ("代理错误：出口代理拒绝或失败",
              "high",
              ["确认代理地址 / 凭据", "换代理或直连对比", "查代理侧日志"]),
    "http_error": ("HTTP 层错误（非上述具体类别）",
                   "med",
                   ["保留原始异常文本", "用 --deep 抓包拿更底层证据"]),
    "unknown": ("未能从异常归类失败原因（证据不足）",
                "low",
                ["保留原始异常文本", "用更底层抓包拿证据", "不要臆造原因"]),
}

# 状态机：请求生命周期。unknown 不在这条链上——unknown 只在「结果未知」时由诊断层标出。
# 这里的状态是「执行侧」状态：发起 -> 已发送 -> 已接收 -> (成功 / 失败)。
_STATE_FLOW = ("initiated", "sent", "received", "success", "failed")


class RequestTrace:
    """一次请求的结构化证据。所有时间走 clock（perf_counter），不碰墙钟算时长。"""

    __slots__ = (
        "url", "method", "state", "_t0", "_t1",
        "status", "final_url", "redirects", "error", "category",
        "bytes", "headers", "body", "body_truncated",
    )

    def __init__(self, url, method="GET"):
        self.url = url
        self.method = method
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
        self.state = "failed"

    def dur_ms(self):
        if self._t1 is None:
            return None
        return (self._t1 - self._t0) * 1000.0

    def to_metrics(self):
        """给调用方（爬虫 / 诊断器）的精简指标块。"""
        return {
            "url": self.url,
            "method": self.method,
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
              follow_redirects=True, body_limit=200000, client=None):
    """发一次真实请求并产出 RequestTrace。client 可注入（测试用），否则自动建/关。

    只做观测 + 取首段 body，不做任何解析/落盘——那是上层爬虫 handler 的事。
    """
    import httpx  # 局部导入：本模块可被「无 httpx」环境静态加载（能力注册阶段）

    tr = RequestTrace(url, method)
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
    """基于 trace 给诊断。返回 {category, reason, confidence, next_steps, evidence}。"""
    cat = trace.category or "unknown"
    reason, conf, steps = _DIAGNOSES.get(cat, _DIAGNOSES["unknown"])
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
        }
    return {
        "category": cat,
        "reason": reason,
        "confidence": conf,
        "next_steps": list(steps),
        "evidence": evidence,
    }


def run_diagnosis(url, **kw):
    """独立入口：探测一次 + 出诊断报告。供 crawl.diagnose 能力调用。"""
    tr = probe_url(url, **kw)
    return {
        "url": url,
        "trace": tr.to_dict(),
        "diagnosis": diagnose(tr),
    }
