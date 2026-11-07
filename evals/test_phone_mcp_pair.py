"""手机 MCP /pair 桥门禁（离线：本机起 relay + 假手机，不碰真设备）。

钉的是**这条链路**：
    控制面（PairMCPClient）──ws──→ 中继 /pair ←──ws── 手机（假）
配对后只走换行 JSON，控制面能 initialize / tools/list / tools/call，
并且错误路径不静默：未知工具、错 secret、缺配置都如实报出来。

跑法：python evals/test_phone_mcp_pair.py
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.cloudnode.wsrelay import WsTunnelServer, connect_pair   # noqa: E402
from fleet.mcp.pair import PairMCPClient, normalize_pair_url      # noqa: E402

FAIL = []
TMP = tempfile.mkdtemp(prefix="phone_mcp_pair_")


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 假手机
#: 至少两个工具；名字必须与 Android 端已落地的能力名一致。
PHONE_TOOLS = [
    {"name": "self_status", "description": "手机自身状态",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self_ui_tap", "description": "点一下屏幕",
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}}}},
    {"name": "self_ui_dump", "description": "导出界面树",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "self_ui_screenshot", "description": "截屏（回传图片）",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _json_send(sock, obj):
    sock.sendall(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")


def _phone_reply(req, state):
    """一条手机端 JSON-RPC 请求 → 回包（None 表示通知不回）。"""
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req

    if method == "initialize":
        state["initialized"] += 1
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake-phone", "version": "0.2"},
        }}
    if method == "notifications/initialized":
        state["initialized_notice"] += 1
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        state["list_requests"] += 1
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": PHONE_TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        state["calls"].append((name, args))
        if name == "self_status":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False}}
        if name == "self_ui_tap":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(args, ensure_ascii=False)}],
                "isError": False}}
        if name == "self_ui_screenshot":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"},
                    {"type": "text", "text": "/sdcard/shot.png"},
                ],
                "isError": False}}
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": f"没有这个工具: {name}"}],
            "isError": True}}
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"没有这个方法: {method}"}}


def fake_phone(relay_url, room, secret, state):
    try:
        sock = connect_pair(relay_url, room, secret, timeout=10)
    except Exception as e:
        state["err"] = f"{type(e).__name__}: {e}"
        state["done"].set()
        return
    state["paired"] = True
    buf = b""
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                state["received"] += 1
                try:
                    req = json.loads(line.decode("utf-8"))
                except Exception:
                    _json_send(sock, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700,
                                                "message": "parse error"}})
                    continue
                if not isinstance(req, dict):
                    continue

                # 故意先塞一条通知 + 一条错 id 的回包：客户端的
                # 「跳过非本请求消息」必须真的生效，不能被错配骗走。
                if req.get("method") == "tools/call" and \
                        (req.get("params") or {}).get("name") == "self_status":
                    sock.sendall((json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/phone",
                         "params": {"noise": 1}}, ensure_ascii=False)
                        + "\n").encode("utf-8"))
                    _json_send(sock, {"jsonrpc": "2.0", "id": 999999,
                                      "result": {"content": [
                                          {"type": "text", "text": "错 id 的假结果"}],
                                          "isError": False}})

                resp = _phone_reply(req, state)
                if resp is None:
                    continue
                payload = json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
                # tools/list 的回包**拆成两个物理帧**发：
                # 协议允许一条消息被拆帧，客户端必须能重新拼起来。
                if req.get("method") == "tools/list":
                    half = max(1, len(payload) // 2)
                    sock.sendall(payload[:half])
                    time.sleep(0.01)
                    sock.sendall(payload[half:])
                else:
                    sock.sendall(payload)
    except Exception as e:
        state["err"] = f"{type(e).__name__}: {e}"
    finally:
        sock.close()
        state["done"].set()


# ---------------------------------------------------------------- 起中继
def start_relay():
    secret = "phone-mcp-" + uuid.uuid4().hex
    room = "room-" + uuid.uuid4().hex[:14]
    srv = WsTunnelServer(0, host="127.0.0.1", verbose=False, secret=secret,
                         addr_path=os.path.join(TMP, "addrbook.json"))
    box = {}
    threading.Thread(target=srv.serve_forever,
                     kwargs={"ready_cb": lambda p: box.update(port=p)},
                     daemon=True).start()
    for _ in range(200):
        if "port" in box:
            break
        time.sleep(0.02)
    if "port" not in box:
        raise SystemExit("中继没起来")
    return f"ws://127.0.0.1:{box['port']}/pair", room, secret


def main():
    relay_url, room, secret = start_relay()

    print("① 地址归一：裸域名自动补 /pair")
    check(normalize_pair_url("wss://relay.example.com") ==
          "wss://relay.example.com/pair", "wss://host → /pair")
    check(normalize_pair_url("wss://relay.example.com/") ==
          "wss://relay.example.com/pair", "wss://host/ → /pair")
    check(normalize_pair_url("wss://relay.example.com/custom") ==
          "wss://relay.example.com/custom", "已有路径原样保留")

    print("② 假手机先进等待态，控制面走完整链路")
    state = {"paired": False, "err": "", "done": threading.Event(),
             "received": 0, "list_requests": 0, "initialized": 0,
             "initialized_notice": 0, "calls": []}
    threading.Thread(target=fake_phone, args=(relay_url, room, secret, state),
                     daemon=True).start()
    time.sleep(0.25)          # 让假手机先连上、进等待态

    client = PairMCPClient(relay_url, room, secret, name="fake-phone",
                           timeout=8.0)
    st = client.status()
    check(st.get("ok") is True, f"status ok（reason={st.get('reason')!r}）")
    check(st.get("server_name") == "fake-phone",
          f"initialize 里拿到 server_name（{st.get('server_name')!r}）")
    check(st.get("protocol") == "2025-06-18",
          f"protocol 是 2025-06-18（{st.get('protocol')!r}）")
    check(st.get("tools") == len(PHONE_TOOLS),
          f"tools/list 拿到 {len(PHONE_TOOLS)} 个工具（实际 {st.get('tools')}）")

    tools = client.list_tools()
    names = {t.get("name") for t in tools}
    check("self_status" in names and "self_ui_tap" in names,
          f"至少 self_status / self_ui_tap 在线：{sorted(names)}")
    check(state.get("list_requests") == 1,
          f"tools/list 被缓存（手机只收到 1 次，实际 {state.get('list_requests')}）")
    client.list_tools(refresh=True)
    check(state.get("list_requests") == 2,
          f"refresh=True 会重问手机（实际 {state.get('list_requests')}）")

    print("③ 调工具：回包正确，且跳过通知/错 id 不串包")
    res = client.call_tool("self_status")
    content = (res or {}).get("content") or []
    check((res or {}).get("isError") is False, "self_status 返回 isError=false")
    check(content and content[0].get("text") == "ok",
          f"self_status 文本是 ok（{content[:1]}）")
    check(("self_status", {}) in state.get("calls", []),
          "手机真的收到了 tools/call self_status")
    check(state.get("initialized") == 1 and state.get("initialized_notice") == 1,
          "initialize 后发了 notifications/initialized")

    args = {"x": 100, "y": 200}
    res = client.call_tool("self_ui_tap", args)
    tap_text = json.loads(((res or {}).get("content") or [{}])[0].get("text") or "{}")
    check(tap_text == args, f"self_ui_tap 回显 arguments（{tap_text}）")

    res = client.call_tool("self_ui_screenshot")
    shot = ((res or {}).get("content") or [])
    check(shot and shot[0].get("type") == "image",
          f"照片回包保留 image 内容（{shot[:1]}）")
    check(shot and shot[0].get("data") == "aGVsbG8=",
          "图片 data 原样透传，没有被客户端改写")
    check(len(shot) > 1 and shot[1].get("text") == "/sdcard/shot.png",
          "图片后的文字说明仍在（远端知道原图落点）")

    res = client.call_tool("definitely_not_a_tool")
    check((res or {}).get("isError") is True,
          "未知工具 → isError:true（不是异常，也不是空成功）")
    check("没有这个工具" in (((res or {}).get("content") or [{}])[0].get("text") or ""),
          "未知工具的失败原因写清楚了")

    client.close()

    print("④ 缺配置 / 错 secret：都必须报错，不许静默")
    bad_cfg = PairMCPClient("", "", "").status()
    check(bad_cfg.get("ok") is False, "缺配置时 status 返回 ok=false")
    check("配置不全" in bad_cfg.get("reason", ""),
          f"缺配置的原因写明（{bad_cfg.get('reason')!r}）")

    wrong = PairMCPClient(relay_url, room, "wrong-secret", timeout=8.0)
    st2 = wrong.status()
    check(st2.get("ok") is False, "错 secret 时 status 返回 ok=false")
    reason = st2.get("reason", "")
    check("bad token" in reason or "refused" in reason,
          f"错 secret 被中继拒绝且原因可见（{reason!r}）")
    wrong.close()

    state["done"].wait(3)
    shutil.rmtree(TMP, ignore_errors=True)
    print("test_phone_mcp_pair:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
