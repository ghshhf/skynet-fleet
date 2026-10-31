"""可达层：只要有一条通道连得上，它就是网里的一台设备。

🔴 这一层为什么存在（2026-11-01，主人定的口径）
  天网过去的模型是「**先入网，才有网**」：装 agent → 密码入网 → 账本里一条记录 →
  然后才有能力。这个模型对内网自建是对的，对**世界上的其他设备**是错的 ——
  一台同事的笔记本、主人手机上那个现成的 App、一块能连上的开发板，全都进不来，
  不是因为连不上，是因为「没走入网流程」。

  LocalSend 的模型反过来：**只要能连，就已在网里**。它不区分子网、不问归属，
  只维护一件事 ——「这台设备，眼下有几条通道是通的」。通了就出现在列表里，
  断了就标不可达，再通就回来。设备不是被「批准加入」的，是被「观察到」的。

  这一层就是把那个模型搬过来。从此天网的拓扑有两个同心圆：

      内圈 · 编队节点   入网过（有 PSK、有账本记录）→ 能执行命令、能用算力
      外圈 · 可达对端   只要连得上            → 能传文件、能读名片、能被看见

  **两个圈之间那道墙是这一层最重要的东西**：可达 ≠ 可执行。
  谁能 ping 通我们的端口，不代表他能让我们跑命令；反过来，
  外圈设备也不能因为「连上了」就获得内圈权限。这道墙不写在文档里，
  写在这个模块与注册表里（见 `EXEC_CAPS`）。

通道模型（照抄 LocalSend `discovery/store.rs` 的语义，因为它已被上亿次安装验证过）
  · 设备身份 = fingerprint（稳定、跨重启不变），**不是 IP**。同一台设备换了网段、
    换了协议、走了热点，都还是它 —— 用 IP 当身份，手机一换 WiFi 就成了新设备。
  · 一台设备 = **多条通道**，每条通道各自 Available / NotReachable。
    组播通了但 HTTPS 挂了，仍然算「在」，只是要按排好序的通道去试。
  · 排序：**可用优先 → IPv4 优先 → 最近确认优先**（就是 localsend 的规则）。
    最近确认放最后是有道理的：先按「现在就能用」，再看「上次什么时候见过」，
    否则一台昨天在线、今天断了的设备会排在刚连上的设备前面。

诚实边界
  · 这一层只**记账**，不主动探测。谁把设备交进来（发现 / 发送成功 / 用户指定），
    谁就负责报告状态；这一层不偷偷发包 —— 一台设备被我们反复测活，
    在对方的抓包里就是「有个不明程序在扫我」。
  · 落盘只存「这台长什么样、哪条通道什么时候通过」，**绝不存凭据**。
    沿用注册表的 NEVER 纪律，PIN / token 一个都不许进来。
"""
import json
import os
import tempfile
import threading
import time

#: 账本位置。可用 FLEET_REACH 覆盖（门禁用临时文件，别动真账本）
PATH = os.environ.get("FLEET_REACH") or os.path.join(
    os.path.expanduser("~"), ".fleet", "reach.json")

VERSION = 1
#: 每台设备最多留几条历史。只够回答「上次见它是什么时候」，不留长篇日志
MAX_LOGS = 24
#: 通道状态只有两种，和 localsend 一致。不做「疑似可用」那种中间态 ——
#: 那种状态最后一定变成「既不敢用它，也不肯删它」
AVAILABLE = "available"
UNREACHABLE = "unreachable"
STATUSES = (AVAILABLE, UNREACHABLE)

#: 落盘时永远剔除的键（同注册表纪律）
_NEVER = ("password", "passwd", "pwd", "token", "secret", "psk", "key",
          "api_key", "pin")

_lock = threading.RLock()
_cache = None


# ------------------------------------------------------------------ 底座

def _blank():
    return {"version": VERSION, "devices": {}}


def _load():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "devices" not in data:
            raise ValueError("形状不对")
    except (OSError, ValueError):
        data = _blank()
    _cache = data
    return _cache


def _save():
    """原子替换。写一半断电不能把账本写成半截 JSON —— 那会让整层静默清零。"""
    data = _load()
    try:
        d = os.path.dirname(PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".reach-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
    except OSError:
        pass            # 落盘失败不影响本次会话的内存账本，只是重启会丢


def _clean(meta):
    return {k: v for k, v in (meta or {}).items()
            if k.lower() not in _NEVER and isinstance(v, (str, int, float, bool))}


def reset():
    """清空账本。给门禁用，也给「我不想再看见这些设备」用。"""
    global _cache
    with _lock:
        _cache = _blank()
        _save()


# ------------------------------------------------------------------ 记账

def channel_key(kind, addr, port=None):
    """通道的唯一键。`lan:192.168.1.5:53317` / `ble:AA:BB:CC:DD:EE:FF`。

    为什么把 addr 拼进键里：同一台设备可能同时有两条 lan 通道（有线 + 无线），
    它们的可用性会不一样。用 kind 当键就分不开了。
    """
    a = str(addr or "").strip()
    return "%s:%s%s" % (kind, a, (":%d" % int(port)) if port else "")


def split_key(key):
    """把通道键拆回 (kind, addr, port)。给上层判断「这条通道是什么」用。"""
    parts = str(key or "").split(":")
    if len(parts) < 2:
        return (parts[0] if parts else ""), "", None
    kind = parts[0]
    if len(parts) >= 3 and parts[-1].isdigit():
        return kind, ":".join(parts[1:-1]), int(parts[-1])
    return kind, ":".join(parts[1:]), None


def note(device_id, alias="", channel=None, status=AVAILABLE, meta=None, rtt=None,
         kind=None):
    """记一次确认。设备不在就建，在就更新那条通道。

    返回更新后的设备记录。**这是这一层唯一的写入口** —— 发现、发送成功、
    手动指定，全都走它，免得「同一件事三处各记一遍、三处各自不一致」。
    """
    if not device_id:
        raise ValueError("设备必须有 id（fingerprint），不能拿 IP 当身份")
    if status not in STATUSES:
        raise ValueError("通道状态只许 %s，得到 %r" % (STATUSES, status))
    key = channel or "unknown:?"
    now = int(time.time())

    with _lock:
        data = _load()
        dev = data["devices"].get(device_id)
        if dev is None:
            dev = {"id": device_id, "alias": alias or device_id[:8],
                   "first_seen": now, "last_seen": now,
                   "channels": {}, "logs": [], "meta": {}}
            data["devices"][device_id] = dev
        if alias:
            dev["alias"] = alias
        dev["meta"].update(_clean(meta))
        dev["last_seen"] = now

        ch = dev["channels"].get(key) or {"first_seen": now, "fails": 0}
        prev = ch.get("status")
        ch["status"] = status
        ch["last_seen"] = now
        if rtt is not None:
            ch["rtt_ms"] = round(float(rtt), 1)
        if status == AVAILABLE:
            ch["fails"] = 0
            ch.pop("why", None)
        else:
            ch["fails"] = int(ch.get("fails") or 0) + 1
            if meta and meta.get("why"):
                ch["why"] = str(meta["why"])[:200]
        dev["channels"][key] = ch

        dev["logs"].append({"at": now, "channel": key, "status": status,
                            "kind": "discovered" if prev is None else "updated"})
        dev["logs"] = dev["logs"][-MAX_LOGS:]
        _save()
        return json.loads(json.dumps(dev))     # 给出去的是副本，防手滑改内存


def mark_failed(device_id, channel, why=""):
    """这条通道这次没打通。**不删设备** —— 它还在，只是这条路不通了。

    这一点很关键：一次超时就把设备删掉，会让「手机锁屏了 30 秒」变成
    「手机从网里消失了」，然后用户看到一个空列表，以为功能坏了。
    """
    return note(device_id, channel=channel, status=UNREACHABLE,
                meta={"why": why} if why else None)


def forget(device_id):
    """真删一台设备（连同它的所有通道与历史）。返回是否删掉了。"""
    with _lock:
        data = _load()
        hit = data["devices"].pop(device_id, None)
        if hit:
            _save()
        return hit is not None


# ------------------------------------------------------------------ 读取

def ranked_channels(dev):
    """按 localsend 的规则给通道排序，好的在前。

    规则（顺序不能换）：
      ① 可用的排不可用的前面 —— 「现在能用」永远压过「以前能用」；
      ② IPv4 排 IPv6 前面 —— 不是偏好，是实测：IPv6 链路本地地址在 Windows 上
         带 scope id，很多库拼 URL 会直接失败；
      ③ 最近确认的排前面 —— 同样是可用通道，刚通过的那条更可能是当前路由。
    """
    items = list((dev.get("channels") or {}).items())
    items.sort(key=lambda kv: (
        kv[1].get("status") != AVAILABLE,
        ":" in str(kv[0]) and _looks_v6(kv[0]),
        -(int(kv[1].get("last_seen") or 0)),
    ))
    return [k for k, _ in items]


def _looks_v6(channel):
    _, addr, _ = split_key(channel)
    return ":" in (addr or "")


def best_channel(dev):
    chs = ranked_channels(dev)
    return chs[0] if chs else None


def devices(only_available=True):
    """列设备。默认只给「至少一条通道可用」的 —— 断了的设备留在账本里但不展示。"""
    data = _load()
    out = []
    for dev in data["devices"].values():
        chs = dev.get("channels") or {}
        live = [k for k, v in chs.items() if v.get("status") == AVAILABLE]
        if only_available and not live:
            continue
        d = dict(dev)
        d["channels"] = dict(chs)
        d["live"] = live
        d["best"] = best_channel(dev)
        out.append(d)
    out.sort(key=lambda d: -(int(d.get("last_seen") or 0)))
    return out


def get(device_id):
    return (_load()["devices"] or {}).get(device_id)


def find(text):
    """按 id 前缀 / 别名 / 通道地址找一台设备。找不到返回 None。

    为什么要模糊找：「把文件发给手机」这句话里没有 fingerprint。人记的是名字，
    工具记的是哈希 —— 中间必须有一步翻译，而这一步只能在这里做一次。
    """
    if not text:
        return None
    t = str(text).strip().lower()
    cands = []
    for dev in _load()["devices"].values():
        score = 0
        if dev["id"].lower().startswith(t):
            score = 100
        elif t in (dev.get("alias") or "").lower():
            score = 80
        else:
            for ch in (dev.get("channels") or {}):
                if t in ch.lower():
                    score = 60
                    break
        if score:
            cands.append((score, dev))
    if not cands:
        return None
    cands.sort(key=lambda x: (-x[0], -(int(x[1].get("last_seen") or 0))))
    return cands[0][1]


def stats():
    data = _load()
    devs = data["devices"]
    live = sum(1 for d in devs.values()
               if any(v.get("status") == AVAILABLE for v in (d.get("channels") or {}).values()))
    return {"devices": len(devs), "reachable": live, "path": PATH}


# ------------------------------------------------------------------ 自测

def _selftest():
    import shutil
    global PATH, _cache
    old, PATH = PATH, os.path.join(tempfile.mkdtemp(), "reach.json")
    try:
        reset()
        d1 = note("f" * 64, alias="手机", channel=channel_key("lan", "192.168.1.9", 53317),
                  meta={"model": "Phone"})
        assert d1["alias"] == "手机" and d1["logs"][-1]["kind"] == "discovered"

        # 同设备第二条通道：不该变成第二台设备
        note("f" * 64, channel=channel_key("ble", "AA:BB:CC:DD:EE:FF"))
        assert len(devices()) == 1, "同 fingerprint 多通道必须是同一台"
        assert len(get("f" * 64)["channels"]) == 2

        # 一条通道断了，设备仍在
        mark_failed("f" * 64, channel_key("lan", "192.168.1.9", 53317), why="超时")
        dev = get("f" * 64)
        assert len(devices()) == 1, "断一条通道不等于设备消失"
        assert best_channel(dev).startswith("ble:"), "可用的那条该排前面"

        # 全都断了才从默认列表里消失，但账本里仍在
        mark_failed("f" * 64, channel_key("ble", "AA:BB:CC:DD:EE:FF"))
        assert devices() == [] and devices(only_available=False)
        assert get("f" * 64) is not None

        # 恢复
        note("f" * 64, channel=channel_key("lan", "192.168.1.9", 53317))
        assert len(devices()) == 1 and get("f" * 64)["channels"][
            channel_key("lan", "192.168.1.9", 53317)]["fails"] == 0

        # 凭据绝不落盘
        note("e" * 64, alias="x", channel="lan:1.2.3.4:1", meta={"pin": "1234", "token": "t"})
        raw = json.dumps(_load(), ensure_ascii=False)
        assert "1234" not in raw and '"token"' not in raw, "凭据进了账本"

        # 身份不许是 IP
        try:
            note("", channel="lan:1.1.1.1:1")
            raise AssertionError("空 id 居然过了")
        except ValueError:
            pass
        assert split_key("lan:192.168.1.5:53317") == ("lan", "192.168.1.5", 53317)

        print("reach 自测：全过")
    finally:
        shutil.rmtree(os.path.dirname(PATH), ignore_errors=True)
        PATH, _cache = old, None


if __name__ == "__main__":
    _selftest()
