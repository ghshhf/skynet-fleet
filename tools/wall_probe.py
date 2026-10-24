"""认证墙测绘 CLI（tools/wall_probe.py）——回答「能扫描到监控，为什么拿不到数据」。

纪律（这条线为什么划在这里，不搞笼统的「监控不能碰」）：
可做的 = **任何合法播放器/扫描器零凭据第一步就会做的事**：
  - RTSP OPTIONS：问服务器支持哪些方法。VLC 连摄像头第一字节就是这个。
  - RTSP DESCRIBE 无凭据：设备不认证就回 SDP（流元数据）；要认证就回 401，
    **401 本身就是数据**（「此设备有墙」是可报告的事实）。拿到 401/403 立即停，
    绝不带任何凭据重试。
  - HTTP GET 未授权公开路径：登录页形态、Server 头、重定向去向。
  - ONVIF WS-Discovery Probe 组播：标准设备发现机制，设计给匿名客户端。
不可做的 = 拿到凭据后才能做的事：SETUP/PLAY（开始收 RTP 流）、任何携带
凭据的请求、任何口令尝试。这些是刑法 285/286 的事，不是能力的事。

换句话说：本工具把每台设备的「暴露面」和「墙形态」完整记录下来——
如果拿不到视频，报告会精确指出缺的是「授权」这一个输入，
而不是含糊的「不行」。

**本文件只是 CLI 外壳。** 协议/解析/判定/时间线的唯一实现在
`fleet/wallprobe.py`（PC 侧 PCSender、手机侧 BridgeSender 共用一份，
`python -m fleet.wallprobe` 跑它的自测）。改行为改那边，不要在这里复制逻辑——
2026-10-24 就是发现这里躺着第二份 _parse_sdp（还带着修不掉的 framesize
bug：`a=framesize:96 1920-1080` 是连字符写法，split() 只有两段），
才把整个外壳改成委托。
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fleet.wallprobe as W

CACHE = os.path.join(os.path.dirname(__file__), "..", "tmp", "sweep_cache.json")
OUT = os.path.join(os.path.dirname(__file__), "..", "tmp", "wall_probe.jsonl")


def probe_one(send, dev):
    """一台设备的完整暴露面。委托核心，只补本工具需要的上下文字段。"""
    rec = W.probe_device(send, dev["ip"], dev.get("open_ports") or [])
    rec["mac"] = dev.get("mac", "")
    rec["category"] = dev.get("category", "")
    return rec


def _pick_local_ip():
    """挑一张 192.168.* 的网卡发组播（多网卡机器上问错网段=零应答）。"""
    import sweep
    ips = sorted(sweep.local_ips() or [])
    return next((i for i in ips if i.startswith("192.168.")),
                ips[0] if ips else None)


def main():
    if not os.path.isfile(CACHE):
        print("没有设备缓存，先跑 sweep", file=sys.stderr)
        return 2
    r = json.load(open(CACHE, encoding="utf-8"))
    devs = r["devices"]

    send = W.PCSender(timeout=2.0)
    local_ip = _pick_local_ip()
    if local_ip:
        # 多网卡：显式从办公网那张卡问出去（否则内核挑默认路由，问错网段=零应答）
        def mcast(payload, wait_ms=2000):
            return send.mcast(payload, wait_ms=wait_ms, local_ip=local_ip)
    else:
        mcast = send.mcast
    onvif = W.onvif_probe(mcast, wait_ms=2000)

    interesting = [d for d in devs if d.get("open_ports")]
    print(f"探测 {len(interesting)} 台有端口设备 + ONVIF 组播…")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        recs = list(ex.map(lambda d: probe_one(send, d), interesting))
    for rec in recs:
        for o in onvif:
            if o["ip"] == rec["ip"]:
                rec["probes"]["onvif"] = o
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"完成 {time.time()-t0:.0f}s，写入 {OUT}")
    print()
    print(f"{'IP':16s} {'类别':8s} {'RTSP 判定':30s} 流/暴露面")
    for rec in recs:
        d = rec["probes"].get("rtsp", {})
        streams = d.get("streams") or []
        if streams:
            flow = "%d 路 %s %s" % (len(streams),
                                    streams[0].get("codec", ""),
                                    streams[0].get("resolution", ""))
        else:
            flow = ""
        face = ""
        for k, v in (rec.get("probes") or {}).items():
            if k.startswith("http_") and v.get("title"):
                face = (v.get("server", "") + " " + v["title"])[:36]
                break
        print(f"{rec['ip']:16s} {rec.get('category', ''):8s} "
              f"{d.get('describe_verdict') or '无554口':30s} {flow}{face}")
    print()
    if onvif:
        print(f"ONVIF 应答 {len(onvif)} 台：")
        for o in onvif:
            print(f"  {o['ip']:16s} {o['xaddrs'][:70]}")
    else:
        print("ONVIF 组播：无应答")
    return 0


if __name__ == "__main__":
    sys.exit(main())
