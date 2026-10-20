"""感知与动作门面：控制面只认这一个口子。

为什么要有这一层：到 2026-10-20，探测与操作能力是七个各跑各的脚本
（sweep / survey / ble / nearby / android_device / phone_ops / infrared），
commander 一个都没 import —— 能力都在仓库里，天网自己不知道。这层的职责只有三件：
收成同一套返回形状、给控制面一个命令面、把「哪些只读、哪些会动手」标出来（过闸依据）。

三条铁律（照抄 discovery 的边界，不另立规矩）：
  · 只读探测绝不改变设备状态；会动手的动作一律列在 SIDE_EFFECT，由控制面过闸。
  · 任何一路失败都降级成 ok=False + err，不把异常抛给控制面——一条腿断了
    不该让整份快照崩掉（三层感知本来就是互补的）。
  · 红外默认不发射（与 infrared.py 一致），要真发必须显式 send=True。

    python perceive.py                       # 三层快照（含与上次比对）
    python perceive.py --lan / --wifi / --bt # 只跑某一层
    python perceive.py caps                  # 能力矩阵：现在能感知什么、为什么不能
    python perceive.py phone screen|tree|apps|fg
    python perceive.py phone open|tap|type   # 会动手
    python perceive.py ir info | ir nec:0x04:0x08 [--send]
    python perceive.py --selftest
"""
import sys
import time

import android_device as A
import ble as B
import nearby as N
import phone_ops as P
import survey as V

# 会改变设备状态的动作：控制面据此走 _gate（分权 + 仲裁）
SIDE_EFFECT = {"open", "tap", "type", "back", "home", "shot"}

# 三层名字沿用 nearby 的定义，不在这里另起一套
LAYERS = N.LAYERS
LAYER_CN = N.LAYER_CN
ago = N.ago                     # 「3 分钟前」这种话只该有一种写法

# 手机动作表。先验名字再连设备：打错字与「手机没接」是两种错，不能混成一句
PHONE_ACTIONS = {"screen", "see", "look", "tree", "apps", "fg", "notify",
                 "open", "tap", "type", "back", "home", "shot", "wait"}

DEV_TTL = 30          # 手机连接缓存秒数：一条命令里连两次 adb 太慢


class _Dev:
    """手机连接的单次缓存。attach 要探活着没，逐条命令都探会明显拖慢。"""

    def __init__(self):
        self.at = 0.0
        self.d = None

    def get(self, max_age=DEV_TTL, force=False):
        now = time.time()
        if not force and self.d and now - self.at < max_age:
            return self.d
        try:
            d = A.attach()
        except Exception as e:                       # adb 缺失/驱动异常都不该炸上层
            d = {"ok": False, "err": f"连手机时出错：{e}"}
        if not isinstance(d, dict):
            d = {"ok": False, "err": "attach 返回了非字典"}
        self.d, self.at = d, now
        return d

    def reset(self):
        self.at, self.d = 0.0, None


_DEV = _Dev()


# ---------------------------------------------------------------- 感知

def snapshot(with_lan=True, with_wifi=True, with_bt=True, save=True):
    """三层快照：IP 层（电脑看）+ 无线层（手机当天线）+ 蓝牙层。

    与上次比对是重点：扫描的价值不在「这一刻有什么」，在「谁进了谁走了」。
    上一次的指纹来自 ~/.fleet/nearby_hist.json；写盘失败不影响本次结果。
    """
    out = N.run(with_lan=with_lan, with_wifi=with_wifi, with_bt=with_bt)
    fp = N.fingerprint(out)
    hist = N.load_hist()
    out["trend"] = N.trend(hist, fp)
    out["hist_file"] = ""
    if save:
        try:
            out["hist_file"] = N.save_hist(hist, fp)
        except OSError as e:
            out["notes"].append(f"指纹没留成（{e}），下次比不了变化")
    return out


def report(out):
    """人类可读的三层报告（含与上次的比对）。控制面只认这个，不去碰 nearby。"""
    return N.report(out, out.get("trend"))


def look(out):
    """把快照压成一句人话，给 REPL 摘要和「她」起话题用，不抢报告的活。"""
    parts = []
    lan = out.get("lan") or {}
    if lan.get("ok"):
        parts.append(f"IP 层 {lan.get('device_count', 0)} 台")
    wifi = out.get("wifi") or {}
    if wifi.get("ok"):
        parts.append(f"无线 {wifi['summary']['total']} 个 AP")
    bt = out.get("bt") or {}
    if bt.get("ok"):
        parts.append(f"蓝牙 {bt['summary']['total']} 个在广播")
    t = out.get("trend") or {}
    if t.get("runs"):
        moved = []
        for layer in N.LAYERS:
            seg = t.get(layer) or {}
            if seg.get("new"):
                moved.append(f"{N.LAYER_CN[layer]}新增 {len(seg['new'])}")
            if seg.get("lost"):
                moved.append(f"{N.LAYER_CN[layer]}少了 {len(seg['lost'])}")
        if moved:
            parts.append("变化：" + "、".join(moved))
    if not parts:
        return "三层都没拿到东西（见下面的原因）"
    return "；".join(parts)


def caps(probe=True):
    """能力矩阵：某项现在能不能用、不能用是缺什么。

    probe=False 只做零成本判断（adb 在不在、令牌有没有），不碰手机；
    probe=True 才真连一次，把「接得上/桥活着/能发射红外」也问清。
    """
    rows = []

    adb = A.adb_path()
    rows.append({"cap": "adb", "ok": bool(adb),
                 "why": adb or "本机找不到 adb（装 platform-tools 或设 FLEET_ADB）"})

    tok = A.bridge_token()
    rows.append({"cap": "编队桥令牌", "ok": bool(tok),
                 "why": "已找到" if tok else "没有令牌（跑 android_bridge/build.sh 构建一次）"})

    d = _DEV.get(force=probe) if probe else {"ok": False, "err": "未探测"}
    serial = (d or {}).get("serial") or ""
    rows.append({"cap": "手机连接", "ok": bool(d.get("ok")), "why":
                 f"{serial}" if d.get("ok") else (d.get("err") or "没接手机（USB 或 adb connect）")})

    if probe and d.get("ok"):
        pong = A.bridge_cmd(serial, "PING")
        rows.append({"cap": "编队桥在线", "ok": bool(pong.get("ok")),
                     "why": (pong.get("out") or pong.get("err") or "")[:60]})
        ir = A.bridge_cmd(serial, "IRINFO")
        rows.append({"cap": "红外发射器", "ok": bool(ir.get("ok")),
                     "why": (ir.get("out") or ir.get("err") or "")[:60]})
        st = B.bt_state(serial)
        rows.append({"cap": "手机蓝牙", "ok": st == "on", "why": f"状态 {st}"})
        rows.append({"cap": "WiFi 扫描", "ok": True, "why": "靠 adb shell cmd wifi 起扫"})
    elif probe:
        for name in ("编队桥在线", "红外发射器", "手机蓝牙", "WiFi 扫描"):
            rows.append({"cap": name, "ok": False, "why": "手机没连上，判不了"})
    return rows


def format_caps(rows):
    lines = []
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"  {mark} {r['cap']:<10} {r['why']}")
    ok = sum(1 for r in rows if r["ok"])
    lines.append(f"  合计 {ok}/{len(rows)} 项可用")
    return "\n".join(lines)


# ---------------------------------------------------------------- 动作（手）

def phone(action, arg="", arg2="", dry_run=False):
    """手机动作。返回 dict，一律带 ok；会动手的动作列在 SIDE_EFFECT 里。

    动作表：
      screen  看屏（前台 + 所有可见文字）      tree   整棵控件树（带 id，调试用）
      apps    第三方应用清单                   fg     只看前台是谁
      open    启动应用（包名或名称）           tap    按屏幕上的字点
      type    给焦点输入框打字                 back/home  按键
      shot    截屏存到本机                     wait   等某个字出现在界面上
    """
    act = (action or "").strip().lower()
    if act not in PHONE_ACTIONS:
        return {"ok": False, "err": f"不认识的动作 {act or '(空)'}"
                                    f"（screen/tree/apps/fg/open/tap/type/back/home/shot/wait）"}
    d = _DEV.get()
    if not d.get("ok"):
        return {"ok": False, "err": d.get("err") or "手机接不上"}
    s = d["serial"]
    try:
        if act in ("screen", "see", "look"):
            r = P.read_screen(s)
            r["labels"] = r.get("labels") or []
            r.pop("nodes", None)          # 控件树太吵，tree 才给
            return r
        if act == "tree":
            got = A.ui_xml(s)
            nodes = A.parse_ui(got["xml"]) if got.get("ok") else []
            return {"ok": bool(got.get("ok")), "count": len(nodes),
                    "text": A.format_tree(nodes, limit=int(arg or 40)),
                    "err": "" if got.get("ok") else got.get("err", "")}
        if act == "apps":
            pkgs = A.packages(s)
            return {"ok": bool(pkgs), "apps": pkgs, "count": len(pkgs),
                    "err": "" if pkgs else "一个第三方包都没读出来"}
        if act == "fg":
            info = A.info(s)
            return {"ok": True, "foreground": info.get("foreground", "")}
        if act == "notify":
            # 只读：不点、不展开。这是「手机在响什么」——感知里最日常的一层
            rows = A.notifications(s, limit=int(arg or 10))
            return {"ok": True, "count": len(rows), "items": rows}
        if act == "open":
            if not arg:
                return {"ok": False, "err": "要个包名或应用名"}
            return P.open_app(s, arg)
        if act == "tap":
            if not arg:
                return {"ok": False, "err": "要屏幕上的文字"}
            return P.press(s, arg, dry_run=dry_run)
        if act == "type":
            if not arg:
                return {"ok": False, "err": "要输入内容"}
            return P.type_text(s, arg)
        if act == "back":
            return A.shell(s, "input keyevent 4")
        if act == "home":
            return A.shell(s, "input keyevent 3")
        if act == "shot":
            path = arg or "fleet_shot.png"
            return A.shot(s, path)
        if act == "wait":
            if not arg:
                return {"ok": False, "err": "要等的关键字"}
            return P.wait_for(s, arg, timeout=float(arg2 or 10))
    except Exception as e:                     # 设备层异常不该炸控制面
        _DEV.reset()
        return {"ok": False, "err": f"动作 {act} 出错：{e}"}
    return {"ok": False, "err": f"动作 {act} 没实现"}


def ir(spec, send=False, serial=""):
    """红外。格式：
        info              看手机有没有发射器、支持什么频率
        nec:0x04:0x08     协议:地址:命令（也支持 necx / samsung / sony）
        raw:38000:9000,4500,560,...
    send=False 只算不发——真发射会把电器打开或关掉，这种动作得人点头。
    """
    text = (spec or "").strip()
    if not text or text == "info":
        d = _DEV.get()
        if not d.get("ok"):
            return {"ok": False, "err": d.get("err") or "手机接不上"}
        r = A.bridge_cmd(d["serial"], "IRINFO")
        return {"ok": bool(r.get("ok")), "out": r.get("out", ""), "err": r.get("err", "")}

    try:
        import infrared as R
    except Exception as e:
        return {"ok": False, "err": f"红外模块不可用：{e}"}

    proto, _, rest = text.partition(":")
    proto = proto.lower()
    try:
        if proto == "raw":
            freq_s, _, pat_s = rest.partition(":")
            freq, pat = int(freq_s), [int(x) for x in pat_s.split(",") if x.strip()]
        elif proto == "nec":
            a, _, c = rest.partition(":")
            freq, pat = R.nec(int(a, 0), int(c, 0))
        elif proto == "necx":
            a, _, c = rest.partition(":")
            freq, pat = R.nec_ext(int(a, 0), int(c, 0))
        elif proto == "samsung":
            a, _, c = rest.partition(":")
            freq, pat = R.samsung(int(a, 0), int(c, 0))
        elif proto == "sony":
            a, _, c = rest.partition(":")
            freq, pat = R.sony(int(a, 0), int(c, 0))
        else:
            return {"ok": False, "err": f"不认识的协议 {proto}（nec/necx/samsung/sony/raw）"}
    except (ValueError, IndexError) as e:
        return {"ok": False, "err": f"参数解析不了：{e}"}

    out = {"ok": True, "proto": proto, "freq": freq, "segments": len(pat),
           "sent": False, "pattern": pat}
    if not send:
        out["note"] = "未发射（加 send 才真发）"
        return out
    d = _DEV.get()
    if not d.get("ok"):
        # 形状要一致：序列算出来了但没发出去，不能让调用方猜字段在不在
        out["ok"], out["err"] = False, (d.get("err") or "手机接不上")
        out["note"] = "序列已算出，但设备不可达，没发出去"
        return out
    r = R.send(d["serial"], freq, pat)
    out["sent"] = bool(r.get("ok"))
    out["out"] = r.get("out", "")
    if not r.get("ok"):
        out["ok"], out["err"] = False, r.get("err", "发射失败")
    return out


# ---------------------------------------------------------------- 自测

def _stub_dev(ok=True, serial="STUB-1", err=""):
    """替身：自测不碰真机。返回一个能替换 _DEV.get 的桩。"""
    payload = {"ok": ok, "serial": serial, "err": err}

    def _get(max_age=DEV_TTL, force=False):
        return dict(payload)
    return _get


def _selftest():
    # ① 能力矩阵：没 adb 也要出完整一张表，不能抛异常（缺什么要写清）
    saved = (_DEV.get, A.adb_path, A.bridge_token)
    A.adb_path = lambda: ""
    A.bridge_token = lambda: ""
    _DEV.get = _stub_dev(ok=False, err="没接手机")
    try:
        rows = caps(probe=True)
        txt = format_caps(rows)
        assert len(rows) >= 4, rows
        assert any(r["cap"] == "adb" and not r["ok"] for r in rows)
        assert "找不到 adb" in txt
        assert "手机没连上，判不了" in txt          # 依赖手机的项要显式说判不了
        assert "合计" in txt
    finally:
        _DEV.get, A.adb_path, A.bridge_token = saved

    # ② 快照：三层都空时 look 给一句可读的，不返回 None
    empty = {"at": 0, "lan": None, "wifi": None, "bt": None, "notes": ["x"], "trend": {}}
    assert "都没拿到" in look(empty)
    assert look({"at": 0, "lan": {"ok": True, "device_count": 7},
                 "wifi": None, "bt": None, "trend": {}}) == "IP 层 7 台"

    # ③ 变化摘要要能同时报进和出
    t = {"runs": 3, "window": 5, "age": 60, "changed": True,
         "lan": {"solid": [], "new": [{"k": 1}], "lost": [], "fringe": 0},
         "wifi": {"solid": [], "new": [], "lost": [{"k": 1}, {"k": 2}], "fringe": 0},
         "bt": {"solid": [], "new": [], "lost": [], "fringe": 0}}
    got = look({"at": 0, "lan": {"ok": True, "device_count": 3}, "wifi": None, "bt": None, "trend": t})
    assert "IP 层新增 1" in got and "无线层少了 2" in got, got

    # ④ 手机动作：接不上时必须给 ok=False 而不是异常
    saved = _DEV.get
    _DEV.get = _stub_dev(ok=False, err="没接手机")
    try:
        r = phone("screen")
        assert r["ok"] is False and "没接手机" in r["err"], r
        # 打错动作名要在连设备之前就被拦下：否则「手机没接」会盖住真正的错
        r = phone("不知道啥动作")
        assert r["ok"] is False and "不认识的动作" in r["err"], r
    finally:
        _DEV.get = saved

    # ⑤ 动作分类：会动手的必须在 SIDE_EFFECT 里（控制面靠它过闸）
    for a in ("open", "tap", "type", "back", "home", "shot"):
        assert a in SIDE_EFFECT, a
    for a in ("screen", "tree", "apps", "fg", "wait", "notify"):
        assert a not in SIDE_EFFECT, a

    # ⑥ 红外默认不发：只算序列，返回 sent=False
    r = ir("nec:0x04:0x08")
    assert r["ok"] and r["sent"] is False and r["segments"] == 67, r
    assert "未发射" in r["note"]
    r = ir("samsung:0x07:0x02")
    assert r["ok"] and r["freq"] == 38000 and r["segments"] == 67, r
    r = ir("raw:38000:9000,4500,560")
    assert r["ok"] and r["pattern"] == [9000, 4500, 560], r

    # ⑦ 坏参数不炸、要说人话
    for bad in ("nec:zz:1", "sony:0x1", "unknown:1:2"):
        r = ir(bad)
        assert r["ok"] is False, (bad, r)
    assert "不认识的协议" in ir("unknown:1:2")["err"]
    assert "参数解析不了" in ir("sony:0x1")["err"]

    # ⑧ 要发但手机没接上：序列照样算出来，且明确没发出去
    saved = _DEV.get
    _DEV.get = _stub_dev(ok=False, err="没接手机")
    try:
        r = ir("nec:0x04:0x08", send=True)
        assert r["ok"] is False and r["sent"] is False and len(r["pattern"]) == 67, r
    finally:
        _DEV.get = saved

    print("perceive \u81ea\u6d4b\uff1a8 \u9879\u5168\u8fc7")


# ---------------------------------------------------------------- CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "caps":
        print(format_caps(caps(probe="--no-probe" not in argv)))
        return 0

    if argv and argv[0] == "phone":
        if len(argv) < 2:
            print(phone.__doc__)
            return 1
        r = phone(argv[1], argv[2] if len(argv) > 2 else "",
                  argv[3] if len(argv) > 3 else "",
                  dry_run="--dry-run" in argv)
        print(json_dump(r))
        return 0 if r.get("ok") else 2

    if argv and argv[0] == "ir":
        if len(argv) < 2:
            print(ir.__doc__)
            return 1
        r = ir(argv[1], send="--send" in argv)
        print(json_dump(r))
        return 0 if r.get("ok") else 2

    only = {"lan": "--lan" in argv, "wifi": "--wifi" in argv, "bt": "--bt" in argv}
    pick = any(only.values())
    out = snapshot(with_lan=(not pick) or only["lan"],
                   with_wifi=(not pick) or only["wifi"],
                   with_bt=(not pick) or only["bt"],
                   save="--no-save" not in argv)
    if "--json" in argv:
        print(json_dump({k: v for k, v in out.items() if k != "trend"}))
    else:
        print(N.report(out, out.get("trend")))
        print()
        print("一句话：" + look(out))
    return 0


def json_dump(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
