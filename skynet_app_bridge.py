"""SkyNet App（天网 1.0.20）远程模式桥接：让手机 App 也能反连家里。

现状（1.0.20）：App 经 `adb reverse tcp:8000 tcp:8000` + `--link --link-port 8000`
只在**手机与电脑同 WiFi** 时成立——这正是「方寸之限」。本模块把那条链路从
「本地 adb reverse」扩成「经互联网反连家里中继」，复用同一套 PhoneClient 协议。

App 侧要做的（在设置页加一个「远程模式」开关）：
  1. 配 ZH 家里中继地址（cloudflared 的 https / Tailscale 100.x / 域名）+ 配对凭据
     （device_id + secret，走已有的 6 位码配对接口换）。
  2. 开远程模式时，不再只依赖 `adb reverse`，而是启动一个后台服务调用本模块的
     `start_remote(client_cfg)`，它会拨家里、REGISTER 能力、承接 CALL。
  3. 本地能力（聊天/截图/红外）照旧在手机本地跑，只是「谁来下令」从「本机电脑」
     变成「家里中继转来的 CALL」——执行逻辑一行不用改。

本文件不依赖 Android SDK，纯 Python；App 侧（Kotlin）用同样协议（common.py 的
长度前缀 JSON 帧 + AUTH/REGISTER/CALL/CALL_RESULT）实现等价 client 即可。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def make_client(device_id, secret, host="127.0.0.1", port=9799, token="",
                executor=None, caps_provider=None):
    """造一个 PhoneClient。executor/caps_provider 由 App 注入：通常就是 App 本地
    的「能力分发器」和「能力清单」。返回一个已 start() 的后台 client。"""
    from fleet.remote_link import PhoneClient
    return PhoneClient(host, port, device_id, secret, executor, caps_provider,
                       token=token, reconnect=True).start()


# App 远程模式的最小配置模板（真实凭据走配对接口，不写死）
DEFAULT_CFG = {
    "host": "127.0.0.1",   # 家里中继地址（cloudflared/Tailscale/域名）
    "port": 9799,
    "token": "",           # 入口 token（公网暴露时填）
    "device_id": "",       # dev-xxxx（配对换得）
    "secret": "",          # fw_xxxx（配对换得，明文只活在配对那一次）
}


def _selftest_app_bridge():
    """验证本模块能造出 client 并连上本机 relay（不依赖 App）。"""
    import tempfile, threading, time
    from fleet.remote_link import RelayServer, PhoneClient
    from fleet.device_auth import DeviceVault

    tmp = tempfile.mkdtemp(prefix="appbridge_")
    vault = DeviceVault(path=os.path.join(tmp, "a.json"))
    code = vault.new_code()
    c = vault.claim(code, "天网App", "android")
    relay = RelayServer(host="127.0.0.1", port=0, vault=vault)
    relay.start()

    def exec_(cap, args):
        return {"ok": True, "code": "success", "cap": cap,
                "device": c["device_id"], "data": {"echo": cap}}

    cli = make_client(c["device_id"], c["secret"], relay.host, relay.port,
                      executor=exec_, caps_provider=lambda: ["phone.see", "ir.command"])
    time.sleep(0.3)
    r = relay.request(c["device_id"], "phone.see", {}, timeout=5)
    cli.stop()
    relay.stop()
    ok = r.get("ok") and r.get("cap") == "phone.see"
    print("skynet_app_bridge 自测：", "PASS" if ok else ("FAIL %r" % r))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest_app_bridge())
