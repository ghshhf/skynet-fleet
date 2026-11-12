# -*- coding: utf-8 -*-
"""t76 · 抓包方向第一版：网络观测与协议理解（netprobe）

全部离线：用 unittest.mock 注入 client，不给网络发任何真实请求。
覆盖：错误分类、状态机、计时、重定向链、诊断、probe_url 成功/失败路径、run_diagnosis。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from unittest.mock import MagicMock

from fleet import netprobe


_total = 0
_fails = []


def chk(cond, msg):
    global _total
    _total += 1
    if cond:
        print("  ✔", msg)
    else:
        _fails.append(msg)
        print("  ✘", msg)


def _resp(status, content=b"", history=(), final_url="http://a/"):
    r = httpx.Response(status, content=content,
                      request=httpx.Request("GET", final_url))
    try:
        r.history = history
    except Exception:
        object.__setattr__(r, "history", history)
    return r


# ---------------------------------------------------------------- 1. 错误分类
print("== 1. 错误分类 ==")
chk(netprobe.classify_error(httpx.ConnectError("connection refused")) == "connection",
    "ConnectError -> connection")
chk(netprobe.classify_error(httpx.ConnectTimeout("cto")) == "timeout_connect",
    "ConnectTimeout -> timeout_connect")
chk(netprobe.classify_error(httpx.ReadTimeout("rto")) == "timeout_read",
    "ReadTimeout -> timeout_read")
chk(netprobe.classify_error(httpx.TimeoutException("to")) == "timeout",
    "TimeoutException -> timeout")
chk(netprobe.classify_error(httpx.ProxyError("pe")) == "proxy",
    "ProxyError -> proxy")
chk(netprobe.classify_error(httpx.RemoteProtocolError("rp")) == "protocol",
    "RemoteProtocolError -> protocol")
chk(netprobe.classify_error(httpx.UnsupportedProtocol("up")) == "protocol",
    "UnsupportedProtocol -> protocol")
chk(netprobe.classify_error(
        httpx.HTTPStatusError("hs", request=httpx.Request("GET", "http://x"),
                              response=httpx.Response(500))) == "http_status",
    "HTTPStatusError -> http_status")
chk(netprobe.classify_error(httpx.HTTPError("he")) == "http_error",
    "HTTPError -> http_error")
# 文本嗅探（非 httpx 异常）
chk(netprobe.classify_error(RuntimeError("getaddrinfo: nodename nor servname")) == "dns",
    "文本嗅探 -> dns")
chk(netprobe.classify_error(RuntimeError("SSL: CERTIFICATE_VERIFY_FAILED")) == "tls",
    "文本嗅探 -> tls")
chk(netprobe.classify_error(RuntimeError("some random boom")) == "unknown",
    "未知异常 -> unknown（不臆造）")


# ---------------------------------------------------------------- 2. 状态机
print("== 2. 状态机 ==")
tr = netprobe.RequestTrace("http://x")
chk(tr.state == "initiated", "初始状态 initiated")
tr.mark_sent()
chk(tr.state == "sent", "mark_sent -> sent")
tr.mark_received()
chk(tr.state == "received", "mark_received -> received")
tr.finish_success(_resp(200, b"hi"))
chk(tr.state == "success", "finish_success -> success")
tr2 = netprobe.RequestTrace("http://y")
tr2.mark_sent()
tr2.finish_error(RuntimeError("boom"))
chk(tr2.state == "failed", "finish_error -> failed")

# 终态推断：连接建立前的失败必须算 failed；只有「已发出后断开」才是 unknown_result。
chk(netprobe.infer_terminal_state(httpx.ConnectError("getaddrinfo failed"), True) == "failed",
    "DNS 解析失败（ConnectError）-> failed")
chk(netprobe.infer_terminal_state(
        httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED"), True) == "failed",
    "TLS 握手失败（ConnectError）-> failed")
chk(netprobe.infer_terminal_state(httpx.ConnectError("connection refused"), True) == "failed",
    "连接被拒 -> failed")
chk(netprobe.infer_terminal_state(httpx.ConnectError("Connection reset by peer"), True)
    == "unknown_result",
    "已连上后中途断开 -> unknown_result（结果未知）")


# ---------------------------------------------------------------- 3. 计时（perf_counter）
print("== 3. 计时 ==")
tr3 = netprobe.RequestTrace("http://z")
time.sleep(0.012)
tr3.finish_success(_resp(200, b"x"))
d = tr3.dur_ms()
chk(d is not None and d > 0, "dur_ms 为正（%.2fms）" % (d or 0))


# ---------------------------------------------------------------- 4. 重定向链
print("== 4. 重定向链 ==")
r1 = httpx.Response(301, headers={"Location": "http://b/"},
                   request=httpx.Request("GET", "http://a/"))
r2 = _resp(200, b"done", history=(r1,), final_url="http://b/")
tr4 = netprobe.RequestTrace("http://a/")
tr4.finish_success(r2)
chk(tr4.redirects == [(301, "http://a/")], "重定向链记录 [(301, 被重定向请求的url)]")
chk(tr4.final_url == "http://b/", "final_url 落到最终地址")
chk(len(tr4.redirects) == 1, "redirect_count == 1")
chk(tr4.status == 200, "status 200")


# ---------------------------------------------------------------- 5. 诊断
print("== 5. 诊断 ==")
d_ok = netprobe.diagnose(tr4)
chk(d_ok["category"] == "ok", "成功 → category=ok")
chk(d_ok["confidence"] == "high", "成功 → 高置信")
chk("http 200" in " ".join(d_ok["evidence"]).lower() or d_ok["evidence"], "成功诊断带证据")

tr5 = netprobe.RequestTrace("http://z")
tr5.finish_error(httpx.ConnectError("connection refused"))
d_fail = netprobe.diagnose(tr5)
chk(d_fail["category"] == "connection", "失败 → category=connection")
chk(len(d_fail["next_steps"]) > 0, "失败 → 给出下一步建议")
chk("原始异常" in " ".join(d_fail["evidence"]), "失败 → 证据含原始异常")


# ---------------------------------------------------------------- 6. probe_url（mock client）
print("== 6. probe_url ==")
mc = MagicMock()
mc.request.return_value = r2
res = netprobe.probe_url("http://a/", client=mc)
chk(res.state == "success", "probe_url 成功路径")
chk(res.status == 200, "probe_url 拿到 status")
chk(len(res.redirects) == 1, "probe_url 重定向计数")
chk(res.body == "done", "probe_url 取回 body")

mc2 = MagicMock()
mc2.request.side_effect = httpx.ConnectTimeout("cto")
res2 = netprobe.probe_url("http://a/", client=mc2)
chk(res2.state == "failed", "probe_url 失败路径 -> failed")
chk(res2.category == "timeout_connect", "probe_url 失败分类 timeout_connect")
chk(res2.error is not None, "probe_url 失败带 error 文本")

# 注入的 client 不复用关闭
mc3 = MagicMock()
mc3.request.return_value = r2
netprobe.probe_url("http://a/", client=mc3)
chk(mc3.close.called == False, "注入 client 不被 probe_url 关闭")


# ---------------------------------------------------------------- 7. metrics 结构
print("== 7. metrics 结构 ==")
m = res.to_metrics()
for k in ("url", "method", "final_url", "status", "state", "dur_ms",
          "redirects", "redirect_count", "bytes", "category", "error"):
    chk(k in m, "metrics 含字段 %s" % k)


# ---------------------------------------------------------------- 8. run_diagnosis
print("== 8. run_diagnosis ==")
rd = netprobe.run_diagnosis("http://a/", client=mc)
chk("diagnosis" in rd and "trace" in rd, "run_diagnosis 含 diagnosis + trace")
chk(rd["diagnosis"]["category"] == "ok", "run_diagnosis 成功诊断")


# ---------------------------------------------------------------- 9. 异常不向上抛
print("== 9. 健壮性 ==")
mc4 = MagicMock()
mc4.request.side_effect = RuntimeError("weird")
res4 = netprobe.probe_url("http://a/", client=mc4)
chk(res4.state == "failed", "任意异常都被转成观测证据，不抛出")
chk(res4.category is not None, "任意异常都能归类（含 unknown）")


print("\n" + ("=" * 50))
if _fails:
    print("t76: %d/%d 失败" % (len(_fails), _total))
    for f in _fails:
        print("   ✘", f)
    sys.exit(1)
print("t76 全部通过：%d 项断言" % _total)
