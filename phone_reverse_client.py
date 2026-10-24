#!/usr/bin/env python3
"""手机侧：反向隧道常驻 client（Termux 里跑）。

手机主动出站拨家里的常开中继（relay_ingress.py），认证用 device_auth 配对凭据，
REGISTER 自己能做的全部能力；之后家里发来的 CALL 就在手机本地执行（复用整套
android_phone / acquire / creative），只把 DeviceResult 回传。

→ 家里的编队里，这台手机会自动出现成一台 `remote_phone` 设备，`phone.see` /
`ir.command` / `scene.run` 等全部远程可用，跟本地调一模一样。

配对（一次性，在**家里 PC** 上做）：
    python -c "from fleet.device_auth import DeviceVault; v=DeviceVault(); print('码', v.new_code())"
  手机这端拿屏幕上 6 位码去换 device_id+secret（走你已有的配对接口），
  把 device_id / secret 填进下面配置（env 或 ~/.fleet/phone_remote.json）。

配置（优先级 env > json 文件 > 默认值）：
    FLEET_REMOTE_HOST   家里中继地址（cloudflared 给的 URL 主机 / Tailscale 100.x / 域名）
    FLEET_REMOTE_PORT   端口
    FLEET_REMOTE_TOKEN  入口 token（cloudflared/frp 暴露公网时挡扫描用，可空）
    FLEET_DEVICE_ID     dev-xxxx
    FLEET_DEVICE_SECRET fw_xxxx
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fleet.remote_link import PhoneClient  # noqa: E402


def _load_cfg():
    cfg = {
        "host": os.environ.get("FLEET_REMOTE_HOST", "127.0.0.1"),
        "port": int(os.environ.get("FLEET_REMOTE_PORT", "9799")),
        "token": os.environ.get("FLEET_REMOTE_TOKEN", ""),
        "device_id": os.environ.get("FLEET_DEVICE_ID", ""),
        "secret": os.environ.get("FLEET_DEVICE_SECRET", ""),
    }
    p = os.path.expanduser("~/.fleet/phone_remote.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except (OSError, ValueError):
        pass
    return cfg


def _make_executor():
    """手机本地 registry：CALL 来了就在这儿跑，结果原样回传。"""
    from fleet.catalog import build
    reg = build(with_nodes=False)
    # 配对的手机=自己的设备，经 device_auth 已鉴权；副作用能力在本机执行。
    # 闸门默认放行（远程控制的前提），但每次 CALL 会在家里 relay 侧留痕。
    def gate(cap, dev, args, level):
        return True, "phone-side auto-approve (paired device)"
    return lambda cap, args: reg.call(cap, args or {}, gate=gate)


def main():
    cfg = _load_cfg()
    if not cfg["device_id"] or not cfg["secret"]:
        sys.stderr.write(
            "缺少 device_id / secret。先在家里 PC 配对，再把凭据写入\n"
            "~/.fleet/phone_remote.json 或环境变量 FLEET_DEVICE_ID / FLEET_DEVICE_SECRET。\n")
        return 2
    executor = _make_executor()
    caps = lambda: list(__import__("fleet.catalog", fromlist=["build"]).build(
        with_nodes=False).cap_names())
    client = PhoneClient(cfg["host"], cfg["port"], cfg["device_id"], cfg["secret"],
                         executor, caps, token=cfg["token"], reconnect=True)
    print("[phone-reverse] 反连 %s:%s device=%s（断线自动重连）"
          % (cfg["host"], cfg["port"], cfg["device_id"]))
    try:
        client.run()                      # 阻塞常驻
    except KeyboardInterrupt:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
