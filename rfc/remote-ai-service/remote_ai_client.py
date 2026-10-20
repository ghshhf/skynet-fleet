"""远程 AI 即服务 · 瘦客户端最小实现（分支 rfc/remote-ai-service）。

瘦客户端不持有任何模型权重，只把 prompt + 上下文命名空间发给编队内的 AI 节点，
收回文本。就像调一个本地服务器。

依赖：common.py（协议）、commander.py 的 send_to_node 路由（此处用最简直连示意）。
真实部署由 Commander 选「谁能干 AI」的节点（dev_pick）。
"""
import base64
import common


def ask(host, port, password, node, prompt, ctx_ns=None, model_hint=None,
        timeout=60):
    """向 AI 节点发提问，阻塞返回完整文本。

    node: 由 Commander 路由得到的 AI 节点名（如 Her-Phone）。
    返回: (text, model) 或 (None, err)
    """
    import socket
    s = socket.create_connection((host, port), timeout=10)
    common.send_msg(s, {"t": common.AUTH, "name": "thin-client", "password": password})
    if common.recv_msg(s).get("t") != common.AUTH_OK:
        return None, "认证失败"
    mid = common._next_id() if hasattr(common, "_next_id") else "ai1"
    common.send_msg(s, {"t": "ai.chat", "id": mid,
                        "prompt": prompt, "ctx_ns": ctx_ns,
                        "model_hint": model_hint})
    # 等待 AI_RESULT（此处最简：收一条）
    while True:
        m = common.recv_msg(s)
        if not m:
            return None, "连接断开"
        if m.get("t") == "ai.result" and m.get("id") == mid:
            return m.get("text", ""), m.get("model")
        # 流式 chunk 可在此累积
    s.close()


if __name__ == "__main__":
    # 自测需在 Commander + 一个带 ai 能力的节点在线时运行
    print("用法: ask(host, port, password, node, '你好')")
