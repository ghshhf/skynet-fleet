#!/usr/bin/env python3
"""家里常开中继入口：启动 RelayServer，并（可选）用 cloudflared 打洞把端口暴露到公网。

用法：
    python relay_ingress.py                      # 仅起中继（127.0.0.1，本机/局域网可用）
    python relay_ingress.py --cloudflared        # 额外用 cloudflared 打公网隧道（推荐入口）
    python relay_ingress.py --host 0.0.0.0 --port 9799 --token <共享密钥>

入口可插拔（relay 只绑 host:port，想怎么暴露怎么来）：
  · **cloudflared（推荐，默认走这条）**：隧道带 TLS 终止，数据不出户，免路由器端口转发。
    没装的话脚本会提示 `cloudflared tunnel --url http://localhost:<port>` 自己跑。
  · **Tailscale（最省心、最安全）**：家里 PC 和手机都装 Tailscale 进同一 tailnet，
    手机拨 `100.x.y.z:<port>` 即可。wireguard 全加密，且**绝不**把 adb 裸奔公网
    （cleanyourtv/REMOTE.md 的血训：bot 全网扫开放 5555）。
  · **frp / 路由器端口转发**：relay 绑 0.0.0.0，frp 或路由器把端口转出去。
    ⚠️ 务必配 --token，且别把手机 adb(5555) 直接转公网——只暴露**中继端口**。

安全：relay 本身明文跑在内层，加密由入口层（cloudflared TLS / Tailscale wireguard）
提供。手机鉴权走 device_auth 配对（只认自己配对的设备）。
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fleet.remote_link import RelayServer, get_relay  # noqa: E402
from fleet.device_auth import DeviceVault             # noqa: E402


def main():
    import argparse
    ap = argparse.ArgumentParser(description="天网家里常开中继入口")
    ap.add_argument("--host", default=os.environ.get("FLEET_RELAY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FLEET_RELAY_PORT", "9799")))
    ap.add_argument("--token", default=os.environ.get("FLEET_RELAY_TOKEN", ""))
    ap.add_argument("--cloudflared", action="store_true",
                    help="起 cloudflared 公网隧道（推荐入口）")
    ap.add_argument("--pair", action="store_true",
                    help="先在屏幕上打印 6 位配对码（手机用它对家里换 device_id+secret）")
    args = ap.parse_args()

    if args.pair:
        v = DeviceVault()
        code = v.new_code()
        print("\n  配对码（6 位，300s 内有效，手机端配对接口拿它换凭据）：%s\n" % code)

    relay = RelayServer(host=args.host, port=args.port, token=args.token)
    port = relay.start()
    print("[relay] 中继已起：%s:%d%s" % (args.host, port,
          ("（token=%s）" % args.token) if args.token else ""))
    print("[relay] 手机反连配置：FLEET_REMOTE_HOST=%s FLEET_REMOTE_PORT=%d"
          % (args.host, port))

    cf = None
    if args.cloudflared:
        try:
            cf = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", "http://localhost:%d" % port],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print("[relay] cloudflared 隧道启动中……（公网 https 地址会打印在它的输出里）")
        except FileNotFoundError:
            print("[relay] 没装 cloudflared。手动跑：")
            print("        cloudflared tunnel --url http://localhost:%d" % port)
            print("        或装 Tailscale，让手机拨 100.x.y.z:%d（推荐，全加密）" % port)

    print("[relay] 按 Ctrl+C 退出。")
    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        if cf:
            cf.terminate()
        relay.stop()
        print("\n[relay] 已停止")


if __name__ == "__main__":
    main()
