"""瘦客户端版「远程 AI 即服务」（RFC remote-ai-service 合入主线）。

瘦客户端不持有任何模型权重：把 prompt + 上下文命名空间发给 Commander，
由 Commander 路由到「有脑」的 AI 节点（她本人跑在手机/盒子上），收回文本。
就像调一个本地服务器——电视、盒子、临时网吧机、老本子都能这么问「她」。

与 compute_runner 的关系：compute 跑通用计算任务；ai.chat 是「跑对话/人格推理」的
特化通道，二者都经 Commander 路由，共用「谁能干」的选点逻辑。

用法：
  python remote_ai_client.py --ask "帮我写段去重代码" --model-hint 4b
  from remote_ai_client import ask
  text, model, err = ask("127.0.0.1", 9000, "密码", "你好", ctx_ns="her")
"""
import argparse
import os
import socket

import common


def ask(host, port, password, prompt, ctx_ns=None, model_hint=None, timeout=120, on_delta=None):
    """经 Commander 向 AI 节点提问，返回 (text, model, err)。

    真·流式：AI 节点的每段 AI_CHUNK 到达即回调 on_delta（可实时打印），
    同时累积成完整文本；收到 AI_RESULT 即收尾。
    ctx_ns: L3 共享上下文命名空间；带上它，AI 节点会看到你跨设备累积的记忆。
    model_hint ∈ {small, 4b, gpu}：声明要什么档，Commander 路由到匹配节点。
    """
    try:
        s = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        return None, None, f"连接 Commander 失败: {e}"
    try:
        s.settimeout(timeout)
        common.send_msg(s, {"t": common.AUTH, "name": "thin-client", "password": password})
        resp = common.recv_msg(s)
        if not resp or resp.get("t") != common.AUTH_OK:
            return None, None, f"认证失败: {resp}"
        common.send_msg(s, {"t": common.AI_CHAT, "prompt": prompt,
                            "ctx_ns": ctx_ns, "model_hint": model_hint})
        parts = []
        while True:
            m = common.recv_msg(s)
            if m is None:
                if parts:
                    return "".join(parts), None, None
                return None, None, "连接中断，未收到 AI 结果"
            if m.get("t") == common.AI_CHUNK:
                d = m.get("delta", "")
                if d:
                    parts.append(d)
                    if on_delta:
                        on_delta(d)
            elif m.get("t") == common.AI_RESULT:
                text = m.get("text") or "".join(parts)
                return text, m.get("model"), m.get("err")
    finally:
        try:
            s.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--password", default=os.environ.get("FLEET_PASSWORD", "changeme"))
    ap.add_argument("--ask", required=True, help="要问「她」的 prompt")
    ap.add_argument("--ctx-ns", default=None, help="L3 共享上下文命名空间（如 her）")
    ap.add_argument("--model-hint", default=None, choices=list(common.MODEL_HINTS))
    ap.add_argument("--stream", action="store_true", help="逐段实时打印（她一边想一边说）")
    args = ap.parse_args()
    printed = []
    cb = None
    if args.stream:
        def cb(d):
            printed.append(d)
            print(d, end="", flush=True)
    text, model, err = ask(args.host, args.port, args.password, args.ask,
                           ctx_ns=args.ctx_ns, model_hint=args.model_hint, on_delta=cb)
    if err:
        print(f"[瘦客户端] 失败: {err}")
        return
    if args.stream:
        print()   # 流式已逐段输出，收尾换行
    else:
        print(f"[瘦客户端] model={model}")
        print(text)


if __name__ == "__main__":
    main()
