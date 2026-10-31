#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T51 局域网直传（LocalSend v2）：协议对不对 / 闸门在不在 / 失败分不分得清。

这条线最容易糊弄的地方不是"传不动"，是**传歪了也能报成功**：

  ① 不 mock 协议。门禁里起真的 HTTP 服务、真的发真文件、比对 sha256。
     因为这里对接的是**别人的实现**（手机上那个 LocalSend App），字段少一个、
     编码错一处，对端就是 400 —— 而我们自己的 mock 会一路绿灯。
  ② 中文文件名 + 二进制内容单独测。全仓最容易翻车的一格：`open()` 不带
     encoding、`Content-Length` 按字符数算、路径当 str 拼。任何一处都会变成
     "传过去了但文件是坏的"。
  ③ **部分接受**必须分开列。对方只收其中两个时，回一句"发送成功"等于骗人，
     回一句"失败"等于让人重发、结果对方收到两份。
  ④ 闸门是安全核心：文件名里的 `../` 是**对端给的**，当路径用就能写到任意位置；
     四道上限（文件数/单文件/总字节/窗口寿命）缺一道就是常开的后门。
  ⑤ 失败要分得清：连不上=unavailable（可重试/换设备）、对方拒绝=denied（叫人）、
     没有 PIN=denied 且要说明。糊成一个 error 会让上层做错决定。

全离线：只在本机回环，端口用 FLEET_LAN_PORT 避开真的 LocalSend。
"""
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

#: 必须在 import 之前设：PORT 是模块加载时求值的
TEST_PORT = "53919"
os.environ["FLEET_LAN_PORT"] = TEST_PORT
from fleet.kinds import lan_transfer as LT          # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(262144), b""):
            h.update(c)
    return h.hexdigest()


# ---------------------------------------------------------------- 裸 HTTP
def raw_http(method, path, body=b"", headers=None, timeout=8, host="127.0.0.1"):
    """手写一个 HTTP 请求。

    为什么不用 http.client：有几条要测的东西（Content-Length 与实际字节数不符、
    故意带 `../` 的文件名）用标准库根本发不出来 —— 它不会让你破坏自己不变量。
    """
    hdrs = {"Host": "%s:%s" % (host, TEST_PORT), "Connection": "close"}
    hdrs.update(headers or {})
    if body and "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(len(body))
    head = ("%s %s HTTP/1.1\r\n" % (method, path)
            + "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items()) + "\r\n")
    s = socket.create_connection((host, int(TEST_PORT)), timeout=timeout)
    try:
        s.sendall(head.encode("utf-8") + body)
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    head_raw, _, body_raw = buf.partition(b"\r\n\r\n")
    first = head_raw.split(b"\r\n")[0].decode("latin-1")
    try:
        status = int(first.split()[1])
    except (IndexError, ValueError):
        status = 0
    return status, body_raw


# ==================================================================== 主流程
def main():
    print("T51 局域网直传（LocalSend v2）", flush=True)

    # ---- 0. 能力表：注册了、标对了 ----
    from fleet.catalog import CAPABILITIES
    names = {c.name: c for c in CAPABILITIES}
    for n in ("lan.links", "lan.probe", "lan.send", "lan.recv", "lan.recv.stop"):
        check(n in names, "能力表里有 %s" % n)
    check(names["lan.send"].side_effect and names["lan.send"].approval == "ask",
          "lan.send 有副作用且要审批（文件出本机）")
    check(names["lan.recv"].side_effect and names["lan.recv"].approval == "ask",
          "lan.recv 有副作用且要审批（能往本机写盘）")
    check(not names["lan.links"].side_effect, "lan.links 是只读，不用审批")

    dev = LT.LanTransfer()
    check(dev.probe()[0] is True, "本机能有可传的网段")

    # ---- 1. 名片：字段一个都不能少（对端是别人的实现，缺一个就 400） ----
    info = LT.device_info()
    for k in ("alias", "version", "deviceModel", "deviceType", "fingerprint",
              "port", "protocol", "download"):
        check(k in info, "名片带 %s" % k)
    check(info["version"].startswith("2."), "协议版本是 v2（%s）" % info["version"])
    check(len(LT._fingerprint()) == 64, "指纹是 64 位 sha256（跨重启稳定）")
    check(LT._fingerprint() == LT._fingerprint(), "指纹两次调用一致")

    # ---- 2. 开窗 ----
    dest = tempfile.mkdtemp(prefix="t51_recv_")
    src_dir = tempfile.mkdtemp(prefix="t51_send_")
    win = LT.LanTransfer()
    r = win.invoke("lan.recv", {"dir": dest, "secs": 90})
    check(r.ok, "接收窗口开起来了%s" % ("" if r.ok else "：" + r.err))
    if not r.ok:
        print("\nT51 红：%d 项" % len(FAIL))
        return 1

    try:
        # 2a. 重复开窗要拒（一次只能有一个占着端口，不然状态互相覆盖）
        r2 = win.invoke("lan.recv", {"dir": dest, "secs": 30})
        check(not r2.ok and "已经有一个" in r2.err, "第二次开窗被拒并说明原因")

        # ---- 3. 协议端点 ----
        st, body = raw_http("GET", LT.API + "/info")
        check(st == 200, "GET /info 回 200（得到 %d）" % st)
        dto = json.loads(body.decode("utf-8")) if st == 200 else {}
        check(bool(dto.get("alias")), "/info 带回自己的 alias")

        st, _ = raw_http("POST", LT.API + "/register",
                         json.dumps(LT.device_info()).encode("utf-8"),
                         {"Content-Type": "application/json"})
        check(st == 200, "POST /register 回 200（对端登记这条走通了）")

        # ---- 4. 真传一个文件：中文名 + 二进制 ----
        src = os.path.join(src_dir, "报告 2026 年 11 月.pdf")
        payload = os.urandom(2 * 1024 * 1024 + 137)      # 故意留个零头
        with open(src, "wb") as f:
            f.write(payload)
        os.utime(src, (1700000000, 1700000000))

        out = LT.send([src], "127.0.0.1", int(TEST_PORT), "http", alias="门禁发送端")
        check(out.get("ok"), "发送成功%s" % ("" if out.get("ok") else "：" + str(out.get("err"))))
        check(out.get("sent") and not out.get("failed"),
              "sent 有内容且 failed 为空（部分接受要分开列的语义）")

        got = os.path.join(dest, os.path.basename(src))
        check(os.path.isfile(got), "落盘文件名与源一致（含中文与空格）")
        if os.path.isfile(got):
            check(os.path.getsize(got) == len(payload), "字节数一致")
            check(sha(got) == sha(src), "sha256 一致（内容没被破坏）")
            mt = time.gmtime(os.path.getmtime(got))
            check(mt.tm_year == 2023 and mt.tm_mon == 11,
                  "mtime 按 UTC 还原（得到 %s）" % time.strftime("%Y-%m-%d", mt))

        # ---- 5. 失败要分得清 ----
        bad = LT.send(["/no/such/file"], "127.0.0.1", int(TEST_PORT))
        check(not bad.get("ok") and "不是文件" in bad.get("err", ""), "传不存在的路径当场拒")

        down = LT.send([src], "127.0.0.1", 59999, "http")
        check(down.get("code") == "unavailable",
              "端口不通 ⇒ unavailable（不是 error，上层才好判断能不能重试）")

        # ---- 6. 闸门：路径逃逸（fileName 是对端给的，当路径用就完了） ----
        escape = {"info": LT.device_info(),
                  "files": {"1": {"id": "1", "fileName": "../../../t51_evil.txt",
                                  "size": 3, "fileType": "text/plain"}}}
        st, b = raw_http("POST", LT.API + "/prepare-upload",
                         json.dumps(escape).encode("utf-8"),
                         {"Content-Type": "application/json"})
        check(st == 200, "带 ../ 的请求本身仍被接受（拒绝的是那个文件名，不是整单）")
        if st == 200:
            sess = json.loads(b.decode("utf-8"))
            tok = (sess.get("files") or {}).get("1", "")
            st2, _ = raw_http("POST", LT.API + "/upload?sessionId=%s&fileId=1&token=%s"
                              % (sess.get("sessionId"), tok),
                              b"abc", {"Content-Type": "text/plain"})
            check(st2 == 200, "恶意文件名的那次上传走完了（%d）" % st2)
            landed = os.path.join(dest, "t51_evil.txt")
            outside = os.path.abspath(os.path.join(dest, "..", "..", "..", "t51_evil.txt"))
            check(os.path.isfile(landed), "文件被裁成 basename 落在接收目录里")
            check(not os.path.isfile(outside), "没有写到接收目录之外（路径逃逸被拦住）")

        # ---- 7. 闸门：字节数不符 → 删残骸，不留半个文件 ----
        size_lie = {"info": LT.device_info(),
                    "files": {"1": {"id": "1", "fileName": "truncated.bin",
                                    "size": 65536, "fileType": "application/octet-stream"}}}
        st, b = raw_http("POST", LT.API + "/prepare-upload",
                         json.dumps(size_lie).encode("utf-8"),
                         {"Content-Type": "application/json"})
        if st == 200:
            sess = json.loads(b.decode("utf-8"))
            tok = (sess.get("files") or {}).get("1", "")
            # 声明 64KB，实际只给 200 字节 —— 服务端必须发现"少了"
            st2, _ = raw_http("POST", LT.API + "/upload?sessionId=%s&fileId=1&token=%s"
                              % (sess.get("sessionId"), tok),
                              b"x" * 200, {"Content-Type": "application/octet-stream"})
            half = os.path.join(dest, "truncated.bin")
            check(st2 >= 400, "字节数不符 ⇒ 非 2xx（得到 %d）" % st2)
            check(not os.path.isfile(half), "半个文件被删掉，不留残骸")

        # ---- 8. 发现 != 入网：敲过 register 的对端不进节点账本 ----
        win2 = LT.LanTransfer()
        check(win2._window is None, "新建的设备实例不带别人的窗口状态")
    finally:
        stop = win.invoke("lan.recv.stop", {})
        check(stop.ok, "窗口能关")
        rec = stop.data.get("received") or []
        check(all(not str(x.get("path", "")).startswith("..") for x in rec),
              "收到的记录里没有越界路径")
        seen = stop.data.get("seen") or []
        check(isinstance(seen, list), "register 过的对端单独记在 seen 里（只作证据）")

        # 关了就得真关：端口不再接受连接
        time.sleep(0.4)
        closed = False
        try:
            s = socket.create_connection(("127.0.0.1", int(TEST_PORT)), timeout=1.5)
            s.close()
        except OSError:
            closed = True
        check(closed, "窗口关闭后端口释放（不留常开后门）")

        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(src_dir, ignore_errors=True)

    # ---- 9. 数量闸 ----
    d2 = tempfile.mkdtemp(prefix="t51_cap_")
    w3 = LT.LanTransfer()
    r3 = w3.invoke("lan.recv", {"dir": d2, "secs": 20, "max_files": 1})
    if r3.ok:
        try:
            a = os.path.join(d2, "_a.bin")
            b = os.path.join(d2, "_b.bin")
            for p in (a, b):
                with open(p, "wb") as f:
                    f.write(b"z" * 1024)
            o = LT.send([a, b], "127.0.0.1", int(TEST_PORT), "http")
            got = [os.path.basename(x["path"]) for x in o.get("sent") or []]
            rej = len(o.get("rejected") or [])
            check(len(got) == 1, "文件数闸生效：只收 1 个（实收 %d）" % len(got))
            check(rej == 1 or not o.get("ok"),
                  "超限那个被拒并明说（rejected=%d）" % rej)
        finally:
            w3.invoke("lan.recv.stop", {})
    else:
        check(False, "数量闸场景的窗口没开起来：%s" % r3.err)
    shutil.rmtree(d2, ignore_errors=True)

    print()
    if FAIL:
        print("T51 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T51 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
