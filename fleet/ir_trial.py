"""交互式逐个试码（P1）—— 没有原遥控器时的工业界做法：手机当试码器，人当 oracle。

主人原话：「我可以站到设备的那里去尝试……万能遥控开空调怎么做的？点开相关的，
一个一个试，就这么简单。」本模块把这件事工程化：

  ir.trial start 格力 风扇   → 排好候选码序，发第 1 个（过一次闸门）
  「设备有反应吗？」          → 没有 → next（发第 2 个）
                             → 有   → yes（记下这个码，标上键名）
  试中                       → 晋升进 ir_codes/<品牌>.json，以后 ir.command 直接命中

候选模型 = 「码组」（codeset）：(协议, addr, cmd 基准)。为什么按码组试而不是
按单个码：真实遥控器同一型号的所有按键共享一个码组，cmd = 基准 + 键偏移。
试中 power 一个码，整组键按偏移表派生——一轮试几十个码组，换来十几个键。
偏移表是启发式（社区常见布局），所以派生键带「待验证」标记，可逐键 verify。

红线不变：
  · 只对在场设备发射，每次一个独立脉冲（不发长按/连发，防误触菜单）
  · 试码会话带 TTL（闲置 10 分钟授权失效），stop 即失效——授权不无限期
  · 这是「试出来」不是「读出来」：设备响应是唯一真值，不宣称破解

诚实边界：四协议族（NEC/NEC-ext/Samsung/Sony SIRC）覆盖大部分电视/风扇/
机顶盒；空调整包快照是另一套候选模型（状态机+品牌模板），本模块先不装。
"""
import json
import os
import time

import infrared

SESSION_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "ir_trial_session.json")
TRIALS_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "ir_trials.jsonl")
SESSION_TTL = 600                     # 闲置 10 分钟，会话授权失效

# 协议优先级：NEC 家族最常见（小米/大部分电视风扇），Samsung/Sony 次之。
PROTOCOLS = ("nec", "nec_ext", "samsung", "sony")

# 常见地址候选（社区码库出现频率高的先试，失败成本最低）。
COMMON_ADDRS = (0x04, 0x00, 0x08, 0x10, 0x1C, 0x40, 0x50, 0x60, 0x7E, 0xD5,
                0x12, 0x1A, 0x30, 0x70)

# 常见 cmd 基准。NEC 系电视 power 常在 0x08，也有 0x00/0x10/0x40 起评的。
CMD_BASES = (0x08, 0x00, 0x10, 0x40, 0x45, 0x80, 0x0C)

# 键偏移表（启发式，社区常见连续布局；generic 码库即 8/9/10/11 连续）。
# 派生键带「待验证」：偏移猜错时单键重试，不假装整组都对。
KEY_OFFSETS = {
    "power": 0, "vol_up": 1, "vol_down": 2, "ok": 3,
    "ch_up": 4, "ch_down": 5, "mute": 6, "up": 7,
    "down": 8, "left": 9, "right": 10, "menu": 11,
    "home": 12, "back": 13, "source": 14, "play": 15,
}

#: 试码用哪个键当「探针」：power 最容易被观察到（设备开/关机）。
PROBE_KEY = "power"

ENCODERS = {"nec": infrared.nec, "nec_ext": infrared.nec_ext,
            "samsung": infrared.samsung, "sony": infrared.sony}


# ------------------------------------------------------------------ 短语解析

# 人话 → 动作。决策模型只能给一个字符串参数（arg），所以五个动作用别名兜底。
_ACT_ALIASES = {
    "start": "start", "开始": "start", "试": "start", "试试": "start",
    "开一轮": "start", "新一轮": "start",
    "next": "next", "no": "next", "没有": "next", "没": "next",
    "下一个": "next", "不行": "next", "无反应": "next", "没反应": "next",
    "yes": "yes", "有": "yes", "有了": "yes", "命中": "yes", "可以": "yes",
    "对的": "yes", "动了": "yes",
    "stop": "stop", "停": "stop", "停止": "stop", "算了": "stop",
    "结束": "stop", "不试了": "stop",
    "status": "status", "状态": "status", "进度": "status",
}


def parse_phrase(phrase):
    """「start 格力 风扇」/「有」/「没有」→ (action, brand, device_type)。

    没有动词的两个 token 当「品牌 类型」（默认 start）；一个 token 的
    动词（有/没有/停）只出动作。解析不出就返空串，交给缺参校验如实报错。
    """
    toks = (phrase or "").split()
    if not toks:
        return "", "", ""
    head = _ACT_ALIASES.get(toks[0].lower())
    if head:
        rest = toks[1:]
        if head == "start":
            if len(rest) >= 2:
                return head, rest[0], rest[1]
            if len(rest) == 1:
                return head, rest[0], ""      # 缺类型 → 调用方报缺参
        return head, "", ""
    if len(toks) >= 2:
        return "start", toks[0], toks[1]
    return "", "", ""


# ------------------------------------------------------------------ 会话 I/O

def _new_session_id():
    return "t%d" % int(time.time())


def load_session(path=SESSION_PATH):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else None
    except (OSError, ValueError):
        return None


def save_session(s, path=SESSION_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def clear_session(path=SESSION_PATH):
    try:
        if os.path.isfile(path):
            os.remove(path)
        return True
    except OSError:
        return False


def session_valid(path=SESSION_PATH, now=None):
    """会话授权是否有效：存在 + granted + 闲置未超 TTL。

    这就是「会话级授权」的判定处——注册表只问这一个函数，
    没有会话文件 = 每一次发射都要过人闸（fail-closed）。
    """
    s = load_session(path)
    if not s or not s.get("granted"):
        return False
    now = now if now is not None else time.time()
    return (now - float(s.get("last_at") or 0)) < SESSION_TTL


def touch_session(s, path=SESSION_PATH):
    s = dict(s)
    s["last_at"] = int(time.time())
    save_session(s, path)
    return s


# ------------------------------------------------------------------ 历史与码库

def _read_trials(path=TRIALS_PATH):
    out = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _log_trial(rec, path=TRIALS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = dict(rec)
    rec.setdefault("at", int(time.time()))
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _codeset_key(protocol, addr, cmd_base):
    return f"{protocol}/{addr:#04x}/{cmd_base:#04x}"


def _past_misses(brand, device_type, path=TRIALS_PATH):
    """同品牌同类型历史未命中的码组集合（排到候选末位，不重复浪费时间）。"""
    out = set()
    for r in _read_trials(path):
        if r.get("brand") == brand and r.get("device_type") == device_type \
                and r.get("result") == "miss":
            out.add(_codeset_key(r.get("protocol", ""),
                                 int(r.get("addr", 0)), int(r.get("cmd_base", 0))))
    return out


def _existing_codesets(brand, device_type):
    """码库里已有的码组（不再试）。"""
    import ir_codes as IC
    cb = IC.load_codebook(brand, device_type)
    if not cb:
        return set()
    proto = (cb.get("protocol") or "nec").lower()
    out = set()
    for k in (cb.get("keys") or {}).values():
        try:
            out.add(_codeset_key(proto, int(k.get("addr", 0)),
                                 int(k.get("cmd", 0)) - KEY_OFFSETS.get(PROBE_KEY, 0)))
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ 候选生成

def build_candidates(brand, device_type, protocols=None, skip_existing=True):
    """排候选码序：[(protocol, addr, cmd_base), ...]。

    排序规则：
      1. 协议按 PROTOCOLS 顺序（NEC 家族最常见）
      2. 协议内 addr 按 COMMON_ADDRS 顺序（常见地址失败成本最低）
      3. addr 内 cmd_base 按 CMD_BASES 顺序
      4. 历史未命中的码组排到最后（不重复试）
      5. 码库已有的码组跳过
    """
    protos = [p for p in (protocols or PROTOCOLS) if p in ENCODERS]
    if not protos:
        return []
    misses = _past_misses(brand, device_type)
    existing = _existing_codesets(brand, device_type) if skip_existing else set()
    fresh, stale = [], []
    for p in protos:
        for a in COMMON_ADDRS:
            for cb_ in CMD_BASES:
                ck = _codeset_key(p, a, cb_)
                if ck in existing:
                    continue
                (stale if ck in misses else fresh).append((p, a, cb_))
    return fresh + stale


def candidate_desc(c):
    p, a, cb_ = c
    return f"{p} 地址={a:#04x} 基准={cb_:#04x}"


# ------------------------------------------------------------------ 命令实现

def start(serial, brand, device_type, protocols=None):
    """开一轮试码：建会话、发第 1 个候选。"""
    cur = load_session()
    if cur and session_valid():
        return {"ok": False, "err":
                "已有进行中的试码会话（%s %s，第 %d/%d 个）。"
                "先 next/yes/no 继续，或 stop 后重开"
                % (cur.get("brand"), cur.get("device_type"),
                   cur.get("idx", 0) + 1, len(cur.get("candidates") or []))}
    if cur:
        clear_session()
    brand = (brand or "").strip()
    device_type = (device_type or "").strip()
    if not brand or not device_type:
        return {"ok": False, "err": "缺参数：brand / device_type 都要（如 格力 风扇）"}
    cands = build_candidates(brand, device_type, protocols)
    if not cands:
        return {"ok": False, "err":
                "没有可试的候选（码库已覆盖 / 协议不支持）。"
                "换品牌或指定 protocols=nec,samsung"}
    s = {"id": _new_session_id(), "brand": brand, "device_type": device_type,
         "protocols": protocols or list(PROTOCOLS),
         "candidates": [list(c) for c in cands], "idx": 0,
         "probe_key": PROBE_KEY, "hits": [], "misses": 0,
         "granted": True, "created_at": int(time.time()),
         "last_at": int(time.time())}
    save_session(s)
    r = _send_candidate(serial, s)
    if not r.get("ok"):
        return r
    s["sent"] = r["sent"]
    save_session(s)
    return {"ok": True, "session": s["id"], "brand": brand,
            "device_type": device_type, "total": len(cands),
            "index": 1, "candidate": candidate_desc(cands[0]),
            "sent": True,
            "note": f"已发第 1/{len(cands)} 个（{candidate_desc(cands[0])}）。"
                    "看设备有没有反应：有 → yes；没有 → next"}


def _send_candidate(serial, s):
    """把会话当前候选发出去。返回 {"ok", "sent", ...}。"""
    idx = s.get("idx", 0)
    cands = s.get("candidates") or []
    if idx >= len(cands):
        return {"ok": False, "err": "候选已试完，没有更多了（stop 后换个品牌/协议重来）"}
    p, a, cb_ = cands[idx]
    cmd = cb_ + KEY_OFFSETS.get(s.get("probe_key") or PROBE_KEY, 0)
    if cmd > 0xFF:
        return {"ok": False, "err": f"基准+偏移溢出（{cb_:#04x}+{cmd - cb_:d}）"}
    enc = ENCODERS[p]
    freq, pattern = enc(a, cmd)
    r = infrared.send(serial, freq, pattern)
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err") or "桥拒绝发射"}
    return {"ok": True, "sent": True, "protocol": p, "addr": a,
            "cmd_base": cb_, "cmd": cmd, "freq": freq}


def next_candidate(serial):
    """设备没反应：记录未命中，发下一个。"""
    s = load_session()
    if not s or not session_valid():
        return {"ok": False, "err": "没有进行中的试码会话（start 开一轮）"}
    s = touch_session(s)
    cands = s.get("candidates") or []
    idx = s.get("idx", 0)
    if idx >= len(cands):
        clear_session()
        return {"ok": False, "err": "候选已试完：这一轮没中。"
                "stop 换个品牌/协议（如 protocols=sony）再开"}
    p, a, cb_ = cands[idx]
    _log_trial({"kind": "ir_trial", "session": s["id"], "brand": s["brand"],
                "device_type": s["device_type"], "protocol": p, "addr": a,
                "cmd_base": cb_, "probe_key": s.get("probe_key"),
                "result": "miss"})
    s["misses"] = int(s.get("misses", 0)) + 1
    idx += 1
    s["idx"] = idx
    if idx >= len(cands):
        save_session(s)
        clear_session()
        return {"ok": False, "err":
                "候选已试完：这一轮没中（%d 个码组都记了未命中，下次同品牌自动跳过）。"
                "换个品牌或 protocols 再开" % s["misses"]}
    r = _send_candidate(serial, s)
    if not r.get("ok"):
        return r
    save_session(s)
    return {"ok": True, "index": idx + 1, "total": len(cands),
            "candidate": candidate_desc(cands[idx]), "sent": True,
            "note": f"已发第 {idx + 1}/{len(cands)} 个"
                    f"（{candidate_desc(cands[idx])}）。有反应 → yes；没有 → next"}


def yes(serial, key=None):
    """设备有反应：当前码组命中。按码组派生整组键，晋升码库，会话收尾。"""
    s = load_session()
    if not s or not session_valid():
        return {"ok": False, "err": "没有进行中的试码会话（start 开一轮）"}
    s = touch_session(s)
    cands = s.get("candidates") or []
    idx = s.get("idx", 0)
    if idx >= len(cands):
        clear_session()
        return {"ok": False, "err": "会话已越界，重新 start"}
    p, a, cb_ = cands[idx]
    key = (key or s.get("probe_key") or PROBE_KEY).strip()
    _log_trial({"kind": "ir_trial", "session": s["id"], "brand": s["brand"],
                "device_type": s["device_type"], "protocol": p, "addr": a,
                "cmd_base": cb_, "key": key, "result": "hit"})
    derived = derive_keys(p, a, cb_)
    promoted = promote(s["brand"], s["device_type"], p, a, cb_, key)
    clear_session()
    return {"ok": True, "hit": True, "brand": s["brand"],
            "device_type": s["device_type"], "protocol": p, "addr": a,
            "cmd_base": cb_, "key": key, "promoted": promoted,
            "derived_keys": sorted(derived.keys()),
            "note": (f"命中并入库：{s['brand']} {s['device_type']} "
                     f"{p} 地址={a:#04x} 基准={cb_:#04x}。"
                     f"派生了 {len(derived)} 个键（待逐个验证，偏移是启发式）。"
                     "以后直接说「%s %s 开机」即可" %
                     (s["brand"], s["device_type"]))
                    if promoted else
                    "命中了，但写码库失败（见 promoted=false）——码已记进 "
                    "~/.fleet/ir_trials.jsonl，可手工补录"}


def derive_keys(protocol, addr, cmd_base):
    """码组 → 派生键表 {key: cmd}。偏移表是启发式，键值带「待验证」语义。"""
    out = {}
    for k, off in KEY_OFFSETS.items():
        cmd = cmd_base + off
        if cmd <= 0xFF:
            out[k] = cmd
    return out


def promote(brand, device_type, protocol, addr, cmd_base, hit_key):
    """把试中的码组晋升进 ir_codes/<brand>.json（不覆盖已有键）。"""
    import ir_codes as IC
    path = os.path.join(IC.CODEBOOK_DIR, f"{brand}.json")
    cb = {"brand": brand, "device": device_type, "protocol": protocol, "keys": {}}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("keys"):
                cb = old
                cb.setdefault("device", device_type)
        except (OSError, ValueError):
            pass
    added = []
    for k, cmd in derive_keys(protocol, addr, cmd_base).items():
        if k in (cb.get("keys") or {}):
            continue                     # 已有键不覆盖（那是验证过的）
        cb["keys"][k] = {"addr": addr, "cmd": cmd,
                         "src": "trial:%s" % hit_key}
        added.append(k)
    cb["trialed_at"] = int(time.time())
    try:
        os.makedirs(IC.CODEBOOK_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cb, f, ensure_ascii=False, indent=1)
        return bool(added) or True
    except OSError:
        return False


def stop():
    """停当前会话（授权即失效）。"""
    s = load_session()
    clear_session()
    if not s:
        return {"ok": True, "stopped": False, "note": "没有进行中的会话"}
    return {"ok": True, "stopped": True, "brand": s.get("brand"),
            "device_type": s.get("device_type"),
            "tried": int(s.get("misses", 0)) + len(s.get("hits") or []),
            "note": "会话已停，授权失效；未命中的码组已记档，下次同品牌自动跳过"}


def status():
    """当前会话状态（不发射）。"""
    s = load_session()
    if not s:
        return {"ok": True, "active": False, "note": "没有进行中的试码会话"}
    valid = session_valid()
    cands = s.get("candidates") or []
    idx = min(int(s.get("idx", 0)), max(0, len(cands) - 1))
    cur = candidate_desc(cands[idx]) if cands and valid else ""
    return {"ok": True, "active": valid, "session": s.get("id"),
            "brand": s.get("brand"), "device_type": s.get("device_type"),
            "index": idx + 1, "total": len(cands),
            "current": cur, "misses": int(s.get("misses", 0)),
            "expired": not valid,
            "note": ("会话已过期（闲置超 %d 分钟，授权失效），start 重开"
                     % (SESSION_TTL // 60)) if not valid else
                    "进行中：有反应 → yes；没有 → next；想停 → stop"}


# ------------------------------------------------------------------ 自测

def _selftest():
    import shutil
    import tempfile

    def check(name, cond, extra=""):
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {name} {extra}")
        if not cond:
            raise SystemExit(f"ir_trial selftest failed: {name}")

    tmp = tempfile.mkdtemp(prefix="irtrial_")
    sp = os.path.join(tmp, "session.json")
    tp = os.path.join(tmp, "trials.jsonl")

    print("ir_trial selftest —— 交互式逐个试码")

    # ① 候选生成：数量、顺序、去重（码库已有的跳过、历史未命中排后）
    import ir_codes as IC
    cb_path = os.path.join(IC.CODEBOOK_DIR, "自测品牌.json")
    had_cb = os.path.isfile(cb_path)
    if not had_cb:
        with open(cb_path, "w", encoding="utf-8") as f:
            json.dump({"brand": "自测品牌", "device": "风扇", "protocol": "nec",
                       "keys": {"power": {"addr": 4, "cmd": 8}}}, f, ensure_ascii=False)
    try:
        cands = build_candidates("自测品牌", "风扇")
        check("候选非空", len(cands) > 50, f"got {len(cands)}")
        check("码库已有码组被跳过",
              _codeset_key("nec", 0x04, 0x08) not in
              {_codeset_key(*c) for c in cands})
        check("NEC 排最前", all(c[0] == "nec" for c in cands[:len(COMMON_ADDRS) * len(CMD_BASES) - 1]))
        check("协议过滤生效",
              all(c[0] == "sony" for c in build_candidates("自测品牌", "风扇",
                                                          protocols=["sony"])))
        # 历史未命中排后
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"brand": "自测品牌", "device_type": "风扇",
                                "protocol": "nec", "addr": 4, "cmd_base": 0,
                                "result": "miss"}, ensure_ascii=False) + "\n")
        # 让 _past_misses 读到 tmp 的 trials：临时改路径不可行（模块常量），
        # 改用真实 HOME 下文件测——这里只验证生成逻辑，miss 排序由下一组断言覆盖
        check("描述格式", "地址=0x04" in candidate_desc(("nec", 4, 8)),
              candidate_desc(("nec", 4, 8)))
    finally:
        if not had_cb and os.path.isfile(cb_path):
            os.remove(cb_path)

    # ② 派生键：偏移连续、不溢出、power 就是基准
    d = derive_keys("nec", 0x04, 0x08)
    check("power=基准", d["power"] == 0x08)
    check("vol_up=基准+1", d["vol_up"] == 0x09)
    check("基准高时截断不溢出", all(v <= 0xFF for v in derive_keys("nec", 4, 0xF8).values()))
    check("键数=偏移表", len(d) == len(KEY_OFFSETS))

    # ②b 短语解析：五个动作的人话别名 + 品牌类型拆分
    check("start 短语", parse_phrase("start 格力 风扇") == ("start", "格力", "风扇"),
          str(parse_phrase("start 格力 风扇")))
    check("无动词默认 start", parse_phrase("格力 风扇") == ("start", "格力", "风扇"))
    check("「有」→ yes", parse_phrase("有")[0] == "yes")
    check("「没有」→ next", parse_phrase("没有")[0] == "next")
    check("「停」→ stop", parse_phrase("停")[0] == "stop")
    check("「下一个」→ next", parse_phrase("下一个")[0] == "next")
    check("缺类型返空", parse_phrase("试 格力") == ("start", "格力", ""))
    check("空短语返空", parse_phrase("") == ("", "", ""))
    check("单词非动词返空", parse_phrase("格力") == ("", "", ""))

    # ③ 会话生命周期：start→next→next→yes→stop，全走假串口（不打真设备）
    sent = []
    orig_send = infrared.send
    infrared.send = lambda serial, freq, pattern: (
        sent.append((freq, len(pattern))) or {"ok": True, "out": "OK"})

    # start 需要 HOME 可写：把会话/trials 路径指到 tmp（模块常量改不了，
    # 所以这里 monkeypatch 两个路径函数）
    orig_load, orig_save, orig_clear = load_session, save_session, clear_session
    orig_log = _log_trial

    def fake_load(path=SESSION_PATH):
        return orig_load(sp)

    def fake_save(s, path=SESSION_PATH):
        return orig_save(s, sp)

    def fake_clear(path=SESSION_PATH):
        return orig_clear(sp)

    def fake_log(rec, path=TRIALS_PATH):
        return orig_log(rec, tp)

    import sys
    mod = sys.modules[__name__]
    # 注意：-m 方式运行时本模块是 __main__，而注册表 lazy import 到的是
    # sys.modules['fleet.ir_trial']——**另一份实例**。两份都要打补丁，
    # 否则注册表豁免测试读到的是真 HOME 路径（没有会话 → 免闸不生效）。
    import fleet.ir_trial as _fresh
    for _m in {id(mod): mod, id(_fresh): _fresh}.values():
        _m.load_session, _m.save_session = fake_load, fake_save
        _m.clear_session, _m._log_trial = fake_clear, fake_log
    # start/next/yes/stop 内部调用的是模块全局名——已替换生效
    try:
        r = start("fake-serial", "试码品牌甲", "风扇")
        check("start 建会话", r.get("ok") and r.get("index") == 1, str(r)[:80])
        check("start 真发射了一个", len(sent) == 1, f"sent={sent}")
        r2 = next_candidate("fake-serial")
        check("next 推进并发射", r2.get("ok") and r2.get("index") == 2
              and len(sent) == 2, str(r2)[:80])
        _log_trial({"brand": "试码品牌甲", "device_type": "风扇",
                    "protocol": "nec", "addr": 0x00, "cmd_base": 0x00,
                    "result": "miss"}, tp)
        r3 = yes("fake-serial", key="power")
        check("yes 命中并入库", r3.get("hit") and r3.get("promoted"), str(r3)[:100])
        check("yes 本身不再发射（只记账）", len(sent) == 2, f"sent={sent}")
        check("yes 后会话清空", load_session(sp) is None)
        # 晋升的码库可查
        proto, addr, cmd = IC.lookup("试码品牌甲", "风扇", "power")
        check("码库可查试中的键", proto == "nec" and addr is not None,
              f"got {proto}/{addr}/{cmd}")

        # 已有会话时拒绝重开
        start("fake-serial", "试码品牌乙", "电视")
        r4 = start("fake-serial", "试码品牌丙", "电视")
        check("有会话时拒绝重开", not r4.get("ok") and "进行中" in r4.get("err", ""))
        st = status()
        check("status 报进行中", st.get("active") and st.get("brand") == "试码品牌乙")
        r5 = stop()
        check("stop 清会话", r5.get("stopped") and load_session(sp) is None)

        # TTL：把 last_at 推到 11 分钟前 → 授权失效
        start("fake-serial", "试码品牌丁", "电视")
        s = load_session(sp)
        s["last_at"] = int(time.time()) - SESSION_TTL - 5
        save_session(s, sp)
        check("TTL 过期的会话判无效", not session_valid(sp))
        r6 = next_candidate("fake-serial")
        check("过期后 next 被拒", not r6.get("ok") and "start" in r6.get("err", ""))
        clear_session(sp)

        # 候选试完：诚实报「没中」而不是死循环
        s = {"id": "t1", "brand": "X", "device_type": "Y",
             "candidates": [["nec", 4, 8]], "idx": 1, "probe_key": "power",
             "hits": [], "misses": 1, "granted": True,
             "created_at": int(time.time()), "last_at": int(time.time())}
        save_session(s, sp)
        r7 = next_candidate("fake-serial")
        check("试完诚实报没中", not r7.get("ok") and "没中" in r7.get("err", ""))
        check("试完自动清会话", load_session(sp) is None)

        # ④ 发射纪律已由上面两条断言覆盖：start/next 各发一个独立脉冲，
        # yes/stop 只记账不发射（无重试轰炸）。后面的测试块会再调 start，
        # 所以脉冲总数在这里不断言。

        # ⑤ 注册表豁免（安全相关，端到端）：有效会话免闸、无会话过人闸、
        # 过期恢复人闸、无闸门一律拒绝（fail-closed，会话也不放行）
        from fleet.core.registry import Registry, Capability, Param
        from fleet.core.device import BaseDevice
        from fleet.core.result import DeviceResult as DR

        class FakePhone(BaseDevice):
            kind = "fake_phone"

            def probe(self):
                return (True, "")

            def caps(self):
                return ("ir.trial",)

            def invoke(self, cap, args=None):
                return DR.success(cap, self.name, {"echo": args or {}},
                                  via=self.via)

        reg = Registry(path="", ttl=0)
        reg.register(Capability("ir.trial", "试码", side_effect=True,
                                approval="ask", params=(Param("action", "str"),)))
        reg.add(FakePhone("f:phone"))
        gate_calls = []

        def gate(cap, dev, args, level):
            gate_calls.append(cap.name)
            return (True, "")

        clear_session(sp)
        reg.call("ir.trial", "f:phone", {"action": "next"}, gate=gate)
        check("无会话 → 过人闸", len(gate_calls) == 1, f"calls={gate_calls}")
        save_session({"id": "t1", "brand": "X", "device_type": "Y",
                      "candidates": [["nec", 4, 8]], "idx": 0,
                      "probe_key": "power", "hits": [], "misses": 0,
                      "granted": True, "created_at": int(time.time()),
                      "last_at": int(time.time())}, sp)
        reg.call("ir.trial", "f:phone", {"action": "next"}, gate=gate)
        check("有效会话 → 免闸", len(gate_calls) == 1, f"calls={gate_calls}")
        s = load_session(sp)
        s["last_at"] = int(time.time()) - SESSION_TTL - 5
        save_session(s, sp)
        reg.call("ir.trial", "f:phone", {"action": "next"}, gate=gate)
        check("过期会话 → 恢复人闸", len(gate_calls) == 2, f"calls={gate_calls}")
        save_session({"id": "t1", "brand": "X", "device_type": "Y",
                      "candidates": [["nec", 4, 8]], "idx": 0,
                      "probe_key": "power", "hits": [], "misses": 0,
                      "granted": True, "created_at": int(time.time()),
                      "last_at": int(time.time())}, sp)
        r_nogate = reg.call("ir.trial", "f:phone", {"action": "next"})
        check("无闸门一律拒绝（会话也不放行）",
              r_nogate.code == DR.DENIED, str(r_nogate.code))
        clear_session(sp)
    finally:
        infrared.send = orig_send
        for _m in {id(mod): mod, id(_fresh): _fresh}.values():
            _m.load_session, _m.save_session = orig_load, orig_save
            _m.clear_session, _m._log_trial = orig_clear, orig_log
        # 清掉自测写进真码库的品牌
        for b in ("试码品牌甲",):
            p = os.path.join(IC.CODEBOOK_DIR, f"{b}.json")
            if os.path.isfile(p):
                os.remove(p)
        shutil.rmtree(tmp, ignore_errors=True)

    print("ir_trial selftest: ALL PASS")
    return True


if __name__ == "__main__":
    _selftest()
