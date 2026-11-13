#!/usr/bin/env python3
"""天网·电脑侧「让手机自己跑代码」入口（对等节点，局域网直连）。

手机端 self_code_* 工具开放后，电脑用这个脚本就能让手机写文件、跑脚本、
跑一组自主任务，或把手机工作区里的产物读回来。走的是和手机↔手机同一套
LAN 协议（hello + token → tools/call），不经过云中继、不需要 Termux、不需要 USB。

依赖：纯标准库 + 同目录 lan_client.py。

用法：
    python phone_code.py <target> <token> status
    python phone_code.py <target> <token> files
    python phone_code.py <target> <token> run --lang sh --code "echo hello; uname -a"
    python phone_code.py <target> <token> run --lang sh --file build.sh
    python phone_code.py <target> <token> cmd --command "ls -la"
    python phone_code.py <target> <token> task --key scaffold
    python phone_code.py <target> <token> write --name demo.py --file demo.py --lang py
    python phone_code.py <target> <token> read --name manifest.txt
    python phone_code.py <target> <token> history

<target> 形如 192.168.1.7:18765 / fe80::1%wlan0:18765 / [2408:xxxx]:18765；
<token> 是手机 self_lan_start 回执里的完整口令。

注意：run / cmd / task / write 会在手机上真的执行或落盘。回包丢了也可能已经跑完，
别盲目重试（与手机端「写操作失败不换机重试」同一原则）。
"""
import argparse
import json
import sys

from lan_client import call


def _text(reply):
    """从 MCP 回包里抠出人话文本。"""
    if not isinstance(reply, dict):
        return str(reply)
    if "error" in reply:
        return "对端报错：" + json.dumps(reply["error"], ensure_ascii=False)
    result = reply.get("result") or {}
    parts = []
    for c in result.get("content", []) or []:
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "image":
            parts.append("[图片 %s，%s 字节 base64]" % (
                c.get("mimeType", "image/jpeg"), len(c.get("data", ""))))
    if parts:
        return "\n".join(parts)
    return json.dumps(reply, ensure_ascii=False, indent=2)


def _read_source(a):
    if a.code:
        return a.code
    if a.file:
        with open(a.file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def main():
    ap = argparse.ArgumentParser(description="电脑侧：让手机自己写代码 / 跑代码 / 跑任务")
    ap.add_argument("target", help="手机地址:端口")
    ap.add_argument("token", help="手机 LAN 口令")
    ap.add_argument("action", choices=[
        "status", "files", "read", "write", "run", "cmd", "task", "history",
    ])
    ap.add_argument("--name", default="", help="文件名")
    ap.add_argument("--lang", default="sh", choices=["sh", "py", "js", "html"])
    ap.add_argument("--code", default="", help="直接给代码文本")
    ap.add_argument("--file", default="", help="从本地文件读代码")
    ap.add_argument("--command", default="", help="cmd 子命令的原始 shell 命令")
    ap.add_argument("--args", default="", help="附在代码命令后的参数")
    ap.add_argument("--key", default="probe",
                    choices=["probe", "manifest", "scaffold", "page", "cleanup"])
    ap.add_argument("--timeout-ms", type=int, default=15000)
    ap.add_argument("--net-timeout", type=float, default=60.0, help="网络超时秒数")
    a = ap.parse_args()

    tool, args = "self_code_runtime", {}
    if a.action == "status":
        tool = "self_code_runtime"
    elif a.action == "files":
        tool = "self_code_files"
    elif a.action == "history":
        tool = "self_code_history"
    elif a.action == "read":
        tool = "self_code_read"
        args = {"name": a.name}
        if not a.name:
            ap.error("read 需要 --name")
    elif a.action == "write":
        tool = "self_code_write"
        src = _read_source(a)
        if not a.name or not src:
            ap.error("write 需要 --name 以及 --code/--file")
        args = {"name": a.name, "code": src, "lang": a.lang}
    elif a.action == "run":
        tool = "self_code_run"
        src = _read_source(a)
        if not src:
            ap.error("run 需要 --code 或 --file")
        args = {
            "code": src,
            "lang": a.lang,
            "name": a.name or ("remote." + a.lang),
            "args": a.args,
            "timeout_ms": a.timeout_ms,
        }
    elif a.action == "cmd":
        tool = "self_code_command"
        if not a.command:
            ap.error("cmd 需要 --command")
        args = {"command": a.command, "timeout_ms": a.timeout_ms}
    elif a.action == "task":
        tool = "self_code_task"
        args = {"key": a.key}

    try:
        _hello, reply = call(a.target, a.token, tool, args, timeout=a.net_timeout)
    except Exception as e:
        print("连接/调用失败：%s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1
    print(_text(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
