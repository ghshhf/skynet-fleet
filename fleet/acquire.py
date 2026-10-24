"""采集层（L2 / L3 / L5）：让编队真正「去获取」数据，而不只是分析已有信息。

这一层的意义（详见 docs/信息获取层_下一代进化方向_调研_2026-10-24.md）：
大模型本质「困在互联网文本里」——它有海量公开知识，但对「此时此地、此人此身」
一无所知。L3/L5 给编队**具身化**：看见你的房间、感知你的状态、读出你眼前的字。
L2 给编队一双「听诊器」：把空气里跑的流量拆开看。

诚实边界（沿用项目「诚实原则」）：
- L3/L5 是手机侧，靠 Termux:API（需装 Termux:API 插件）与手机本机 VLM（Qwen3-VL + mmproj）。
- L2 是 PC 侧抓包（scapy）与 bettercap（可选、需 monitor 模式网卡）。
- 视觉/传感是「本人自愿共享的自我数据」，不上云；结果只在本地/授权圈内。
- 本层只做「读 / 拍 / 抓」，绝不发射 / 重放 / 中间人——那是另一档（ir.command/phone.hotspot
  那种动手级，要过闸门且受频谱法规约束）。
"""
import base64
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter

import android_device as A
from .core import DeviceResult as R

# 本机 VLM 端点（手机 llama-server，带 mmproj 即视觉模型）。直连内网、不走系统代理。
PHONE_VLM_BASE = os.environ.get("PHONE_LLM_BASE", "http://192.168.137.40:8080")
_VLM_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LIFE_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "life.jsonl")


# ====================================================================== L3 手机传感

def phone_sense(serial):
    """L3 手机传感融合：环境光→房间明暗/occupancy；运动→身体活动；电池温度→热态。

    只读，不改设备。返回 DeviceResult。Termux:API 未装时诚实下线。
    """
    ok_s, sens = A.sensor_read(serial)
    ok_b, bat = A.battery_json(serial)
    if not ok_s and not ok_b:
        return R.unavailable("phone.sense",
                             "传感器与电池状态都读不到（Termux:API 未装？）",
                             device="phone:main", via="adb")
    readings = sens if ok_s else []
    vals = {}
    for r in readings:
        t = (r.get("type") or "").lower()
        vs = r.get("values") or []
        if vs:
            vals[t] = vs

    lux = None
    for k in ("ambient_light", "light"):
        v = vals.get(k)
        if v and isinstance(v[0], (int, float)):
            lux = float(v[0])
            break

    acc = vals.get("accelerometer") or [0.0, 0.0, 0.0]
    try:
        mag = sum((v - 9.8 if i == 2 else v) ** 2
                  for i, v in enumerate(acc)) ** 0.5
    except Exception:
        mag = 0.0
    motion = "静止" if mag < 1.5 else ("携带移动" if mag < 6 else "明显运动/步行")
    light_level = _lux_to_level(lux)
    occ = None
    if lux is not None:
        occ = "可能无人/暗室" if (lux < 5 and motion == "静止") else "大致在场"

    out = {
        "lux": lux,
        "light_level": light_level,          # 0-5（None=无光传感器）
        "motion_state": motion,
        "accel_mag": round(mag, 2),
        "occupancy_hint": occ,
        "sensors_seen": sorted(vals.keys()),
    }
    if ok_b:
        out["battery_pct"] = bat.get("percentage")
        out["battery_temp_c"] = bat.get("temperature")
        out["battery_health"] = bat.get("health")
        out["charging"] = bool(bat.get("status")
                               and "CHARGING" in str(bat.get("status")))
    out["note"] = ("lux 为环境光、occupancy 是启发式推断非直读；"
                   "电池温度>40℃ 持续需关注。传感器缺失的维度如实留空。")
    return R.success("phone.sense", "phone:main", via="adb", data=out)


def _lux_to_level(lux):
    if lux is None:
        return None
    for thr, lvl in [(1, 0), (10, 1), (100, 2), (1000, 3), (10000, 4), (1e12, 5)]:
        if lux < thr:
            return lvl
    return 5


# ====================================================================== L5 视觉生活流

def phone_see(serial, prompt=None, mode="structured", log=True, camera_id=0):
    """L5 视觉生活流：手机拍照 → pull 本机 → 喂手机本机 VLM(Qwen3-VL) → 结构化 JSON。

    只读（拍照是采集合法），不改设备。返回 DeviceResult。

    prompt：决策模型可指定问题（「桌上有什么？」）；不给则用默认结构化描述。
    mode  ：“structured” 回 JSON（供 life-stream 聚合），“describe” 回纯文本。
    """
    # 1) 拍照并拉回本机
    ok, rp = A.camera_photo(serial, camera_id=camera_id)
    if not ok:
        return R.unavailable("phone.see", rp, device="phone:main", via="adb")
    local = os.path.join(tempfile.gettempdir(), f"fleet_see_{int(time.time())}.jpg")
    ok2, lp = A.pull(serial, rp, local)
    A.shell(serial, f"rm -f {rp}")         # 顺手清手机端临时照（失败不影响）
    if not ok2:
        return R.unavailable("phone.see", lp, device="phone:main", via="adb")
    try:
        with open(lp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return R.error("phone.see", f"读照片失败: {e}", device="phone:main", via="adb")

    # 2) 调本机 VLM
    sys_p = ("你是天网的眼睛。你只看用户此刻拍到的画面，不编造画面外的事。"
             "用中文、结构化 JSON 回传：scene(场景), objects(可见物体列表), "
             "activity(人在做什么), people(人数, 不要推断关系), light(亮/暗), "
             "ocr_text(画面里能认的文字), mood_hint(画面氛围, 不要推断主观情绪)。"
             "看不到就写 null，不要猜。")
    user_p = prompt or "描述这张照片里此刻的世界：你在哪、周围有什么、在做什么、光线如何。"
    try:
        text = _vlm_ask(b64, user_p, system=sys_p, n=400,
                        model=os.environ.get("PHONE_VLM_MODEL", ""))
    except Exception as e:
        return R.error("phone.see", f"VLM 调用失败: {type(e).__name__}: {e}",
                       device="phone:main", via="adb")

    # 3) 解析
    if mode == "structured":
        obs = _extract_json(text)
        if not obs:
            obs = {"raw": text.strip()}      # 模型没回合法 JSON 也不谎报，原样留着
    else:
        obs = {"caption": text.strip()}

    # 4) 落 life-stream
    if log:
        _life_append({"ts": int(time.time()), "serial": serial,
                      "obs": obs, "prompt": user_p})

    out = dict(obs)
    out["note"] = ("视觉是「此刻此地」的真实采样；模型只描述画面，"
                   "不推断主观情绪/心理状态。这是本人自愿共享的自我数据，不出本地圈。")
    return R.success("phone.see", "phone:main", via="adb", data=out)


def _vlm_ask(b64, user_text, system="", n=256, model="", timeout=180):
    """调手机 llama-server 的 /v1/chat/completions（视觉模型）。返回正文 str。"""
    content = [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": user_text},
    ]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    payload = {"messages": messages, "max_tokens": n,
               "temperature": 0.3, "stream": False}
    if model:
        payload["model"] = model
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        PHONE_VLM_BASE.rstrip("/") + "/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"})
    with _VLM_OPENER.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    txt = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return _strip_think(txt)


def _strip_think(s):
    """剥掉推理模型漏进正文的 <think>/</think> 标签（与 tools/phone_llm.py 一致）。"""
    while True:
        t = s.lstrip()
        if t.startswith("</think>"):
            s = t[len("</think>"):]
            continue
        if t.startswith("<think>"):
            e = s.find("</think>")
            if e < 0:
                break
            s = s[e + len("</think>"):]
            continue
        break
    return s


def _extract_json(text):
    """从模型回复里抠出第一个 JSON 对象；失败返回 None。"""
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except ValueError:
        return None


# ---------------------------------------------------------- life-stream 聚合

def life_log(serial, prompt=None, mode="structured"):
    """拍一帧并追加到生活流（~/.fleet/life.jsonl）。"""
    return phone_see(serial, prompt=prompt, mode=mode, log=True)


def life_digest(period="day", limit=400):
    """聚合生活流：按日/周汇总场景/活动/光线分布/陪伴情况。返回 DeviceResult。"""
    frames = _life_read(limit=limit)
    if not frames:
        return R.success("life.digest", "phone:main", via="local", data={
            "frames": 0,
            "summary": "还没有生活流记录。先 phone.see 拍几张。"})
    keyf = (lambda f: time.strftime("%Y-%m-%d", time.localtime(f["ts"]))) if period == "day" \
        else (lambda f: time.strftime("%Y-W%W", time.localtime(f["ts"])))
    groups = {}
    for f in frames:
        groups.setdefault(keyf(f), []).append(f)

    agg = []
    for k in sorted(groups):
        fs = groups[k]
        scenes, acts, lights, people = [], [], [], 0
        for f in fs:
            o = f.get("obs") or {}
            if isinstance(o, dict):
                if o.get("scene"):
                    scenes.append(o["scene"])
                if o.get("activity"):
                    acts.append(o["activity"])
                if o.get("light"):
                    lights.append(o["light"])
                p = o.get("people")
                if isinstance(p, int) and p > 0:
                    people += p
        agg.append({
            "period": k, "frames": len(fs),
            "top_scenes": _top(scenes, 3),
            "top_activities": _top(acts, 3),
            "light_most": _top(lights, 1),
            "people_seen": people,
        })
    return R.success("life.digest", "phone:main", via="local", data={
        "frames_total": len(frames), "groups": agg[-7:],
        "note": "聚合只统计画面里「看得见」的事实；主观情绪/健康不推断。"})


def life_sentinel(baseline_frames=20):
    """生活哨兵：对比最近若干帧与更早基线，报告显著偏离（如「今晚房间持续很暗」）。只读。"""
    frames = _life_read(limit=baseline_frames * 2)
    if len(frames) < 4:
        return R.unavailable("life.sentinel", "生活流样本不够（先积累几帧）",
                             device="phone:main", via="local")
    recent = frames[-baseline_frames:]
    base = frames[:-baseline_frames] or recent

    def light_dist(fs):
        d = {}
        for f in fs:
            o = f.get("obs") or {}
            l = (o.get("light") if isinstance(o, dict) else None)
            if l:
                d[l] = d.get(l, 0) + 1
        return d

    bd, rd = light_dist(base), light_dist(recent)
    flags = []
    if rd.get("暗") and (bd.get("暗", 0) / max(1, sum(bd.values()))) < 0.3:
        flags.append("近期光线明显偏暗（基线多数时间为亮）")
    return R.success("life.sentinel", "phone:main", via="local", data={
        "flags": flags, "baseline_frames": len(base), "recent_frames": len(recent),
        "baseline_light": bd, "recent_light": rd,
        "note": "只看可观测的偏离，不做因果/健康结论。"})


def _life_append(rec):
    os.makedirs(os.path.dirname(LIFE_PATH), exist_ok=True)
    try:
        with open(LIFE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _life_read(limit=1000):
    if not os.path.isfile(LIFE_PATH):
        return []
    out = []
    try:
        with open(LIFE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= limit * 2:
                    break
    except OSError:
        return out
    return out[-limit:]


def _top(items, n):
    return [k for k, _ in Counter(items).most_common(n)]


# ====================================================================== L2 流量解剖（PC 侧）

def net_dissect(duration=5, iface=None, bpf=None, count=300):
    """L2 流量解剖（PC 侧，scapy 抓包）：提取「对话对 / 协议占比 / 明文凭据告警」。

    返回 dict（由 LanScanner 包成 DeviceResult）。scapy 缺失时诚实返回不可用。
    抓包需要 Npcap(Windows)/root(Linux) 且网卡不被本机热点独占。
    """
    try:
        from scapy.all import sniff, IP, TCP, UDP, Raw
    except Exception as e:
        return {"ok": False, "err": f"未装 scapy（pip install scapy）：{e}"}
    try:
        pkts = sniff(timeout=float(duration), iface=iface or None,
                     filter=bpf, count=int(count), store=True)
    except Exception as e:
        return {"ok": False, "err": f"抓包失败（需 Npcap/root，且网卡不被热点独占）：{e}"}

    talkers, proto, plaintext = {}, {}, []
    for p in pkts:
        ip = p.getlayer(IP)
        if not ip:
            continue
        s, d = ip.src, ip.dst
        talkers[(s, d)] = talkers.get((s, d), 0) + 1
        pn = "TCP" if p.getlayer(TCP) else ("UDP" if p.getlayer(UDP) else "OTHER")
        proto[pn] = proto.get(pn, 0) + 1
        raw = p.getlayer(Raw)
        if raw:
            hit = _plaintext_credential(bytes(raw.load))
            if hit:
                plaintext.append({"src": s, "dst": d, "why": hit})
    top = sorted(talkers.items(), key=lambda kv: kv[1], reverse=True)[:12]
    return {"ok": True, "captured": len(pkts), "seconds": float(duration),
            "talkers": [{"src": a, "dst": b, "pkts": n} for (a, b), n in top],
            "protocols": proto, "plaintext_hits": plaintext[:20],
            "note": "只抓本机网卡流量；明文凭据告警用于「自有设备排查」，不对外。"}


def _plaintext_credential(payload):
    """极简明文口令嗅探：telnet/ftp/pop3 登录、HTTP Basic、含 password= 的表单。"""
    try:
        s = payload.decode("latin-1", "ignore")
    except Exception:
        return None
    low = s.lower()
    if any(k in low for k in ("user", "pass", "login", "auth")) and any(
            c in low for c in ("telnet", "ftp", "pop3", "smtp", "imap",
                               "basic realm", "authorization: basic")):
        return "疑似明文凭据交换"
    if "password=" in low or "pwd=" in low:
        return "表单含明文口令字段"
    return None


def net_survey(duration=20, iface=None):
    """L2 无线侦察（bettercap，可选）：BLE/WiFi 设备发现。返回 dict。

    bettercap 缺失时诚实返回不可用（手机端 Termux 可 `pkg install bettercap`）。
    PC 需支持 monitor 模式的网卡才有完整 WiFi 能力——这里只确认跑通并回传长度。
    """
    import shutil
    import subprocess
    bc = shutil.which("bettercap") or shutil.which("bettercap.exe")
    if not bc:
        return {"ok": False,
                "err": "未装 bettercap（PC: go install；手机 Termux: pkg install bettercap）"}
    caplet = (f"net.recon on; ble.recon on; sleep {int(duration)}; "
              "net.recon off; ble.recon off; net.show; ble.show")
    try:
        p = subprocess.run([bc, "-iface", iface or _default_iface(),
                            "-eval", caplet],
                           capture_output=True,
                           timeout=int(duration) + 30, text=True)
    except Exception as e:
        return {"ok": False, "err": f"bettercap 运行失败: {e}"}
    txt = (p.stdout or "") + (p.stderr or "")
    return {"ok": True, "raw_len": len(txt),
            "note": "bettercap 输出为交互式事件流，结构解析留给更专用的采集器；"
                    "此处仅确认跑通并回传长度。PC 需 monitor 模式网卡才有完整 WiFi 能力。"}


def _default_iface():
    return os.environ.get("FLEET_IFACE", "")


# ====================================================================== 离线自测

def _selftest():
    checks, fails = [], []

    def check(name, cond):
        checks.append(name)
        if not cond:
            fails.append(name)

    # lux 分级
    check("lux→level 暗", _lux_to_level(0.5) == 0)
    check("lux→level 室内", _lux_to_level(300) == 3)
    check("lux→level 户外", _lux_to_level(5000) == 4)
    check("lux None", _lux_to_level(None) is None)
    # JSON 抠取
    check("extract json", _extract_json('foo {"a":1,"b":[2]} bar') == {"a": 1, "b": [2]})
    check("extract json 无", _extract_json("no json here") is None)
    # think 剥离
    check("strip think", _strip_think("<think>hi</think>好了") == "好了")
    # 聚合
    check("top 计数", _top(["a", "a", "b"], 1) == ["a"])
    # life 读写（不依赖真机）
    import tempfile as _t
    global LIFE_PATH
    old = LIFE_PATH
    LIFE_PATH = os.path.join(_t.gettempdir(), "fleet_life_test.jsonl")
    if os.path.isfile(LIFE_PATH):
        os.remove(LIFE_PATH)
    _life_append({"ts": 1, "serial": "x", "obs": {"light": "亮", "scene": "家"}})
    _life_append({"ts": 2, "serial": "x", "obs": {"light": "暗", "scene": "家"}})
    frames = _life_read()
    check("life 读写", len(frames) == 2 and frames[0]["obs"]["light"] == "亮")
    LIFE_PATH = old
    # net_dissect 在 scapy 缺失时诚实降级（强制让 import scapy 失败来验这条路径）
    import sys as _sys
    _sys.modules["scapy"] = None
    try:
        r = net_dissect(duration=0.01, count=1)
        check("net_dissect 缺 scapy 诚实报",
              (not r.get("ok")) and "scapy" in r.get("err", ""))
    finally:
        _sys.modules.pop("scapy", None)

    print(f"[acquire] {len(checks)-len(fails)}/{len(checks)} 通过"
          + (f"，失败: {fails}" if fails else " ✓"))
    return not fails


if __name__ == "__main__":
    _selftest()
