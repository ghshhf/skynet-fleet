# -*- coding: utf-8 -*-
"""t77 · 网络诊断深化（结果未知状态机 + 知识库持久化 + incident 沉淀 + task 关联）

全部离线：用 mock client 注入异常，不发起任何真实网络请求。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fleet import netprobe, netdiag
from fleet.kinds import crawler_device


def _ok(cond, msg):
    assert cond, "✗ " + msg
    print("  ✓", msg)


# 临时 KB / incident 落盘位置，避免污染仓库
_TMP = tempfile.mkdtemp(prefix="t77_")
netdiag.KB_PATH = os.path.join(_TMP, "_netdiag_kb.json")
netdiag.INCIDENT_PATH = os.path.join(_TMP, "_netdiag_incidents.jsonl")
netdiag._cache = {"kb": None, "mtime": 0.0}


# ---- 一个不会真连网的 mock client：request 抛指定异常 ----
class _Boom:
    def __init__(self, exc):
        self._exc = exc
    def request(self, *a, **k):
        raise self._exc
    def close(self):
        pass


class _DnsExc(Exception):
    pass
# 让文本嗅探把它归到 dns：str 含 getaddrinfo
_DnsExc.__str__ = lambda self: "getaddrinfo failed: nodename nor servname"


def test_infer_terminal_state():
    print("[infer_terminal_state]")
    _ok(netprobe.infer_terminal_state(httpx.ReadTimeout("read timeout"), sent=False)
        == "unknown_result", "timeout_read(未发)→unknown_result")
    _ok(netprobe.infer_terminal_state(httpx.ReadTimeout("read timeout"), sent=True)
        == "unknown_result", "timeout_read(已发)→unknown_result")
    _ok(netprobe.infer_terminal_state(httpx.ConnectError("connection reset"), sent=True)
        == "unknown_result", "connection(已发且中途断)→unknown_result")
    _ok(netprobe.infer_terminal_state(httpx.ConnectError("connection refused"), sent=False)
        == "failed", "connection(未发/被拒)→failed")
    _ok(netprobe.infer_terminal_state(_DnsExc(), sent=True) == "failed",
        "dns(即使已发)→failed")
    _ok(netprobe.infer_terminal_state(Exception("certificate verify failed"), sent=True)
        == "failed", "tls→failed")


def test_trace_state_machine():
    print("[RequestTrace 状态机]")
    tr = netprobe.probe_url("http://x/", client=_Boom(httpx.ReadTimeout("read timeout")))
    _ok(tr.category == "timeout_read", "ReadTimeout→category=timeout_read")
    _ok(tr.state == "unknown_result", "ReadTimeout→state=unknown_result")
    tr2 = netprobe.probe_url("http://x/",
                             client=_Boom(httpx.UnsupportedProtocol("bad scheme")))
    _ok(tr2.category == "protocol", "UnsupportedProtocol→category=protocol")
    _ok(tr2.state == "failed", "协议错误→state=failed")
    d = netprobe.diagnose(tr)
    _ok(d["state"] == "unknown_result", "diagnose 透出 state=unknown_result")
    joined = "".join(d["next_steps"])
    _ok("盲目重试" in joined or "查状态" in joined,
        "unknown_result 的 next_steps 强调「不盲目重试 / 查状态」")


def test_task_id_passthrough():
    print("[task_id 透传]")
    tr = netprobe.probe_url("http://x/", client=_Boom(httpx.ReadTimeout("x")),
                            task_id="job-9")
    _ok(tr.task_id == "job-9", "RequestTrace.task_id 写入")
    _ok(tr.to_metrics()["task_id"] == "job-9", "to_metrics 带 task_id")
    rep = netprobe.run_diagnosis("http://x/", task_id="job-9",
                                 client=_Boom(httpx.ReadTimeout("x")))
    _ok(rep["task_id"] == "job-9", "run_diagnosis.task_id 透传")
    _ok(rep["trace"]["task_id"] == "job-9", "trace 内 task_id 落地")


def test_kb_persist_and_register():
    print("[知识库持久化]")
    kb = netdiag.ensure_loaded()
    _ok("unknown_result" in kb, "seed KB 含 unknown_result")
    _ok("dns" in kb and "proxy" in kb, "seed KB 含 dns / proxy")
    netdiag._cache = {"kb": None, "mtime": 0.0}
    kb2 = netdiag.ensure_loaded()
    _ok(kb2.get("unknown_result") == kb.get("unknown_result"), "reload 后 seed 仍在")
    netdiag.register_pattern("quota_exceeded", "配额超限", "high", ["降频", "换 key"])
    netdiag._cache = {"kb": None, "mtime": 0.0}
    kb3 = netdiag.ensure_loaded()
    _ok(kb3.get("quota_exceeded", (None,))[0] == "配额超限",
        "register_pattern 后 reload 仍在")
    _ok(os.path.exists(netdiag.KB_PATH), "KB 文件已落盘")


def test_incident_record_and_load():
    print("[incident 沉淀与回读]")
    trace = {"url": "http://a/", "final_url": "http://a/", "status": None,
             "state": "unknown_result"}
    diag = {"category": "timeout_read", "reason": "读取超时", "confidence": "med",
            "next_steps": ["查状态"]}
    row = netdiag.record_incident(trace, diag, task_id="job-9", resolution=None)
    _ok(row["task_id"] == "job-9", "incident 记 task_id")
    _ok(row["category"] == "timeout_read", "incident 记 category")
    incs = netdiag.load_incidents()
    _ok(len(incs) >= 1, "load_incidents 读回 >=1 条")
    _ok(incs[0]["task_id"] == "job-9", "最新 incident 在前且 task_id 正确")
    netdiag.record_incident(trace, diag, task_id="job-9", resolution="增大 timeout")
    incs2 = netdiag.load_incidents()
    _ok(any(i.get("resolution") == "增大 timeout" for i in incs2),
        "incident resolution 可回填")


def test_crawl_diagnose_records_incident():
    print("[crawl.diagnose 端到端（本地被拒端口）]")
    before = len(netdiag.load_incidents())
    dev = crawler_device.CrawlerDevice()
    r = dev.invoke("crawl.diagnose",
                   {"url": "http://127.0.0.1:1/", "task_id": "e2e-1"})
    _ok(r.ok, "crawl.diagnose 返回 ok")
    st = r.data["diagnosis"]["state"]
    _ok(st in ("failed", "unknown_result", "success"), "诊断出有效 state")
    after = len(netdiag.load_incidents())
    if st != "success":
        _ok(after == before + 1, "故障诊断自动沉淀 1 条 incident")
    else:
        _ok(after == before, "成功探测不写 incident")


if __name__ == "__main__":
    test_infer_terminal_state()
    test_trace_state_machine()
    test_task_id_passthrough()
    test_kb_persist_and_register()
    test_incident_record_and_load()
    test_crawl_diagnose_records_incident()
    print("\n[t77] 全部断言通过 ✅")
