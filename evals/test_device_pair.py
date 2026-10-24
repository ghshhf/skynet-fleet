# -*- coding: utf-8 -*-
"""配对门禁：令牌不该天天重抄。

钉的是**这条语义**：
  · 配对码只在本机 GUI 里拿得到，HTTP 里没有任何接口吐它
  · 持码 claim 一次 → 拿到长期凭据；再用同一个码不认（一次性）
  · 设备凭据过鉴权，且**电脑重启后仍然有效**（这是本模块存在的全部理由）
  · 已配对设备不能替主人发新码 / 踢别的设备（管理接口只认本机令牌）
  · 撤销即时生效，重启后也不复活

跑法：python evals/test_device_pair.py
"""
import json
import os
import sys
import tempfile
import types
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="pairgate_")
os.environ["FLEET_DEVICE_DB"] = os.path.join(TMP, "devices.json")
os.environ["FLEET_JOBS"] = os.path.join(TMP, "jobs.json")
os.environ["FLEET_ARTIFACTS"] = os.path.join(TMP, "arts")

from fleet.gui.server import FleetServer            # noqa: E402

FAILS = []
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(cond, why):
    print(("  [ok]   " if cond else "  [FAIL] ") + why)
    if not cond:
        FAILS.append(why)


class FakeReg:
    def capabilities(self):
        return [{"name": "x"}]

    def devices(self):
        return []

    def call(self, name, args=None, gate=None, **kw):
        return types.SimpleNamespace(ok=True, data={}, err="")


def req(base, path, method="GET", body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with _OPENER.open(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"err": raw}
    except (ConnectionError, OSError) as e:
        # 🔴 连接被重置时**必须说清是哪个请求**，否则只会看到一行
        # ConnectionAbortedError，连发到哪一步都不知道（诊断成本极高）。
        raise SystemExit("请求被重置：%s %s%s —— %s"
                         % (method, path, "" if body is None else "（带 body）", e))


def main():
    srv = FleetServer(registry=FakeReg())
    port = srv.start()
    base = "http://127.0.0.1:%d" % port
    tok = {"X-Fleet-Token": srv.token}

    print("— 没配对什么都进不去 —")
    st, j = req(base, "/api/health")
    check(st == 401, "没凭据 → 401（不是 403，要和『拿旧凭据来敲』分开）")
    check("配对" in (j.get("err") or ""), "401 的文案直接告诉用户去配对")

    print("— 码只在本机，网络里拿不到 —")
    code = srv.devices.new_code()
    check(len(code) == 6 and code.isdigit(), "本机生成了 6 位码")
    st, j = req(base, "/api/pair/devices", headers=tok)
    check(st == 200 and j.get("code") == code, "本机令牌能从 /api/pair/devices 读到码")
    st, j = req(base, "/api/pair/code", method="POST", headers={}, body={})
    check(st == 401, "/api/pair/code 不带凭据被拒（%d）" % st)
    # /api/pair/state 是**故意公开**的：它只回答「这台机器开没开配对、叫什么」，
    # 手机在配对前必须能问这一句。所以门禁钉的是「它不许泄密」，不是「它要 401」。
    st, j = req(base, "/api/pair/state", method="POST", headers={}, body={})
    check(st == 200 and set(j.keys()) <= {"ok", "enabled", "name"},
          "/api/pair/state 公开但只有 ok/enabled/name，没有码")

    print("— 持码换长期凭据 —")
    st, j = req(base, "/api/pair/claim", method="POST",
                body={"code": "000000" if code != "000000" else "111111",
                      "name": "攻击者"})
    check(st == 401 and not j.get("ok"), "错码 → 401，不发凭据")
    st, j = req(base, "/api/pair/claim", method="POST",
                body={"code": code, "name": "红米 25060RK16C", "platform": "android"})
    check(st == 200 and j.get("ok"), "对的码 → 拿到凭据")
    did, sec = j.get("device_id", ""), j.get("secret", "")
    check(did.startswith("dev-") and sec.startswith("fw_"), "凭据形态对")
    check(code not in json.dumps(j), "响应里不带回配对码本身")

    st, j = req(base, "/api/pair/claim", method="POST",
                body={"code": code, "name": "同一个人再来一次"})
    check(st == 401, "同一个码第二次不认（一次性）")

    print("— 设备凭据能过鉴权 —")
    dev = {"X-Fleet-Device": "%s:%s" % (did, sec)}
    st, j = req(base, "/api/health", headers=dev)
    check(st == 200 and j.get("ok"), "带设备凭据能访问 /api/health")
    st, j = req(base, "/api/pair/me", headers=dev)
    check(st == 200 and (j.get("device") or {}).get("name", "").startswith("红米"),
          "/api/pair/me 回得出这台是谁")
    st, j = req(base, "/api/health", headers={"X-Fleet-Device": did + ":wrong"})
    check(st == 403, "密钥不对 → 403")
    st, j = req(base, "/api/health", headers={"X-Fleet-Device": "garbage"})
    check(st == 403, "格式不对 → 403（不是 500）")

    print("— 已配对设备不许当管理员 —")
    for p, m, b in (("/api/pair/code", "POST", {}),
                    ("/api/pair/devices", "GET", None),
                    ("/api/pair/devices/revoke", "POST", {"all": True})):
        st, j = req(base, p, method=m, headers=dev, body=b)
        check(st == 403, "%s %s 用设备凭据被拒" % (m, p))

    print("— 电脑重启后仍有效（这是关键）—")
    srv.stop()
    srv2 = FleetServer(registry=FakeReg())
    port2 = srv2.start()
    base2 = "http://127.0.0.1:%d" % port2
    check(srv2.token != srv.token, "重启后本机令牌确实变了（老问题就在这）")
    st, j = req(base2, "/api/health", headers=dev)
    check(st == 200, "同一枚设备凭据在重启后依然通行")
    st, j = req(base2, "/api/health", headers=tok)
    check(st == 403, "老令牌失效（符合预期，所以手机不该用令牌）")

    print("— 撤销即时且持久 —")
    st, j = req(base2, "/api/pair/devices", headers=tok)
    check(st == 403, "重启后的服务不认上一轮令牌")
    st, j = req(base2, "/api/pair/devices", headers={"X-Fleet-Token": srv2.token})
    check(st == 200 and len(j.get("devices") or []) == 1, "本机能看到已配对设备")
    st, j = req(base2, "/api/pair/devices/revoke", method="POST",
                headers={"X-Fleet-Token": srv2.token}, body={"device_id": did})
    check(st == 200 and j.get("revoked") == 1, "撤销成功")
    st, j = req(base2, "/api/health", headers=dev)
    check(st == 403, "撤销后当场 403")

    srv2.stop()
    srv3 = FleetServer(registry=FakeReg())
    port3 = srv3.start()
    st, j = req("http://127.0.0.1:%d" % port3, "/api/health", headers=dev)
    check(st == 403, "撤销已落盘，再重启也不复活")
    srv3.stop()

    print("\n配对门禁：%d 项失败" % len(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
