"""天网·局域网 relay 常驻启动器（本机跑，给同局域网另一台电脑借出口）。

不依赖 commander：只把 SkyNet relay 绑到 0.0.0.0，带 token 鉴权。
另一台电脑用 relay_client.py 连：
  python relay_client.py 192.168.0.103:9798 <token> https://example.com

环境变量可覆盖：
  FLEET_RELAY_PORT  监听端口（默认 9798）
  FLEET_RELAY_TOKEN 共享 token（默认见下）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fleet.skynet_relay import SkyNetRelay

TOKEN = os.environ.get("FLEET_RELAY_TOKEN") or "skynet-lan-test-9f3a"
PORT = int(os.environ.get("FLEET_RELAY_PORT", "9798"))


def main():
    r = SkyNetRelay(host="0.0.0.0", port=PORT, token=TOKEN)
    rp = r.start(host="0.0.0.0", port=PORT, token=TOKEN)
    print(f"[relay-lan] 监听 0.0.0.0:{rp}  token={TOKEN}", flush=True)
    print(f"[relay-lan] 对端连接示例：python relay_client.py <本机IP>:{rp} {TOKEN} https://example.com", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        r.stop()
        print("[relay-lan] 已停止", flush=True)


if __name__ == "__main__":
    main()
