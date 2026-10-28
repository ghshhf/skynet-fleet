# -*- coding: utf-8 -*-
"""端到端冒烟：真打 HTTP，验「整个服务还起得来、还认人」。

和 evals/ 的分工
----------------
门禁（106 项）全是**模块级**自测：调函数、验逻辑、全离线。它们抓得到
「某个函数改坏了」，抓不到这几类整体问题：
  · 服务根本起不来 / 端点路由搬了
  · 鉴权漏了（不带 token 也能查）
  · SSE 事件名或字段改了（手机端会静默收不到）
  · **人格指针漂了**（2026-10-28：问她"你能干什么"，她答"逐条复现 EVIDENCE、
    判 FAIL"——门禁 106/106 全绿，因为没一条门禁真起服务问她一句话）

所以这个脚本故意**不碰内部函数**，只走 HTTP。它要真打模型（一轮对话 + 可选一次
视觉），所以不进 run_selftests，属于人工验收工具。

用法
----
  # 1) 先起服务（另开一个窗口）
  python -m fleet.agent_server --port 8901 --token smoke-token-2026

  # 2) 再跑冒烟
  python tools/smoke_agent_server.py --port 8901 --token smoke-token-2026

  # 3) 想连视觉也一起验（会截图 + 送云端，~10-70s，需先审批）
  python tools/smoke_agent_server.py --port 8901 --token smoke-token-2026 --with-vision
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = ""
TOKEN = ""
ok_n = 0
bad_n = 0


def check(cond, msg):
    global ok_n, bad_n
    if cond:
        ok_n += 1
        print("  ok   %s" % msg)
    else:
        bad_n += 1
        print("  FAIL %s" % msg)


def req(path, data=None, token=None, timeout=60):
    """发一次请求。token 显式传（允许传空串来验 401）。"""
    tok = TOKEN if token is None else token
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body,
                               method="POST" if data is not None else "GET")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            blob = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(blob)
    except urllib.error.HTTPError as e:
        blob = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(blob)
        except Exception:
            return e.code, blob
    except Exception as e:
        return 0, {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}


def main():
    global BASE, TOKEN
    ap = argparse.ArgumentParser(description="天网云侧服务端到端冒烟")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--token", required=True)
    ap.add_argument("--with-vision", action="store_true",
                    help="连视觉链路一起验（截图送云端，慢且要审批）")
    a = ap.parse_args()
    BASE = "http://%s:%d" % (a.host, a.port)
    TOKEN = a.token

    print("== ① 鉴权 ==")
    st, _ = req("/api/health", token="")
    check(st == 401, "无 token 必须 401，实得 %s" % st)
    st, _ = req("/api/health", token="wrong-token")
    check(st == 401, "错 token 也必须 401，实得 %s" % st)

    print("\n== ② 健康 ==")
    st, d = req("/api/health")
    check(st == 200 and isinstance(d, dict), "带 token 200 且是 JSON")
    check(d.get("ok") is True, "ok=true")
    total = d.get("caps_total") or 0
    check(total >= 150, "能力数 ≥150（实得 %s）" % total)
    check((d.get("providers_alive") or 0) >= 1,
          "至少 1 个网关活着（实得 %s）" % d.get("providers_alive"))

    print("\n== ③ 网关快照：绝不许带密钥 ==")
    st, d = req("/api/providers")
    check(st == 200, "200")
    blob = json.dumps(d, ensure_ascii=False)
    # 只看「值」有没有漏：字段名本身要保留（手机端靠 needs_key 判断要不要填）
    leaked = []
    for p in (d.get("providers") or []):
        for k in ("key", "key_hint"):
            v = p.get(k)
            if isinstance(v, str) and len(v) >= 8:
                leaked.append("%s.%s" % (p.get("id"), k))
    check(not leaked, "没有网关把密钥值带出来（漏了 %r）" % (leaked,))

    print("\n== ④ 能力清单 ==")
    st, d = req("/api/caps")
    check(st == 200, "200")
    groups = d.get("groups") or {}
    n = sum(len(v) for v in groups.values()) if isinstance(groups, dict) else 0
    check(n >= 150, "能力数 ≥150（实得 %s）" % n)
    names = {c.get("name") for c in (d.get("caps") or [])}
    for must in ("pcctl.look", "screen.look", "host.monitor"):
        check(must in names, "能力表里有 %s" % must)

    print("\n== ⑤ 只读能力真执行 ==")
    st, d = req("/api/tool/exec", {"name": "host.monitor", "args": {}})
    check(st == 200, "200")
    data = d.get("data") or {}
    check(bool(data), "回的是真数据不是空壳：keys=%s" % list(data)[:6])
    check(any(k in data for k in ("cpu", "mem", "mem_used")), "含 CPU/内存字段")

    print("\n== ⑥ 视觉能力：无审批票必须 fail-closed ==")
    # 票号绑「同一组参数」，所以下面 ⑨ 必须原样复用 ARGS——
    # 换个 prompt 就是另一次调用，票会作废（对的设计，别踩）。
    ARGS = {"name": "pcctl.look", "args": {"prompt": "这一屏在做什么"}}
    st, d = req("/api/tool/exec", ARGS, timeout=120)
    check(d.get("code") == "denied", "必须 code=denied 而不是偷偷执行（实得 %r）"
          % (d.get("code"),))
    ticket = ((d.get("data") or {}).get("approval") or {}).get("id")
    check(bool(ticket), "被拒时要给票号，否则人没法批（实得 %r）" % (ticket,))

    print("\n== ⑦ SSE：一轮真对话 ==")
    url = BASE + "/api/agent"
    r = urllib.request.Request(url, data=json.dumps(
        {"question": "用一句话说你现在能干什么"}).encode(), method="POST")
    r.add_header("Authorization", "Bearer " + TOKEN)
    r.add_header("Content-Type", "application/json")
    kinds, answer, cur = {}, "", None
    seq = []
    try:
        with urllib.request.urlopen(r, timeout=240) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    cur = line.split(":", 1)[1].strip()
                    kinds[cur] = kinds.get(cur, 0) + 1
                    seq.append(cur)
                elif line.startswith("data:") and cur == "answer":
                    try:
                        answer += (json.loads(line.split(":", 1)[1].strip())
                                   .get("answer") or "")
                    except Exception:
                        pass
    except Exception as e:
        check(False, "SSE 断了：%s: %s" % (type(e).__name__, str(e)[:100]))
    check("meta" in kinds, "有 meta 事件（实得 %s）" % list(kinds))
    check(bool(answer.strip()), "answer 非空：%r" % answer[:100])
    check("Traceback" not in answer and "请求失败" not in answer,
          "答案里不是报错堆栈")

    print("\n== ⑧ 人格：她得是「她」==")
    # 硬判据只做弱的那一半（能自动判的部分）：答案里不该是流水线角色的口吻。
    # 更强的「是不是大肥鱼」靠人看，脚本只把原文打出来。
    pipeline_words = ("五段式答卷", "逐条复现", "判 FAIL", "EVIDENCE 引用")
    hit = [w for w in pipeline_words if w in answer]
    check(not hit, "答案不像流水线角色卡的口吻（命中 %r）" % (hit,))
    print("      她说的：%s" % answer.strip()[:200])

    if a.with_vision:
        print("\n== ⑨ 审批后视觉真执行（会在本机截图并送到云端）==")
        req("/api/approve", {"id": ticket, "allow": True})
        check(True, "已批准票号 %s" % ticket)
        t0 = time.time()
        st, d = req("/api/tool/exec", ARGS, timeout=300)
        dt = time.time() - t0
        check(d.get("ok") is True, "批准后真执行了（code=%r，%.1fs）" % (d.get("code"), dt))
        vd = d.get("data") or {}
        check(bool(vd.get("scene")), "回了 scene（她真描述出了画面）")
        check(bool(((vd.get("_meta") or {}).get("data_left_machine")) is True),
              "必须诚实标注「画面已出本机」")
        print("      她看见的：%s" % str(vd.get("scene"))[:160])

    print("\n=== 冒烟结果：%d 过 / %d 败 ===" % (ok_n, bad_n))
    print("事件序列：%s" % (", ".join(seq[:14]) if seq else "（无事件）"))
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
