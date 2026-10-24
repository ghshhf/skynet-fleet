"""设备能力自检 device_probe——每次接入新设备跑一遍，记天网可达的极限。

为什么要存在：
  主人要求「每一次接入新设备，都有一个自检」——不是可达性三层（那是
  android_phone.probe 干的），而是**能力边界摸底**：这台设备有什么硬件、
  网络频段能不能看见、ADB 在它这台 ROM 上被卡到哪。

  比如 MIUI 上 `input keyevent` 全被 INJECT_EVENTS 拦、`settings put` 默认被
  拒需 appops 放权、`media` 二进制根本不存在——这些「卡在哪」只有接上真机
  跑一遍才知道。不摸清楚，上层代码就会在「以为能、实际不能」的假设上建功能。

每条结论带 authority（复用 authority.mk_evidence，四级）：
  - 设备自报的型号/版本/ROM            ⇒ shipped-artifact（实物读到）
  - 我们跑命令观察到的（频段/能力探测） ⇒ controlled-replay（可复现，依赖当时环境）
  - 端口/特征推断的                    ⇒ analyst-inference（本模块尽量不用推断，缺就 unknown）

带文件缓存（`~/.fleet/devprobe/<hash>.json`）：**仅首次接入（缓存不存在）跑全量，
缓存存在即直接返回、不重探**——对应「只第一次接入的时候自检，不用每次都自检」。
需要显式复探（如机主 root 后/ROM 升级后想刷新）调 `run_onboard(force=True)`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

import android_device as A
from . import authority as AUTH

CACHE_DIR = os.path.expanduser("~/.fleet/devprobe")
# 注意：默认不自动过期——「只第一次接入时自检」意味着缓存存在即复用、不重探。
# 显式复探请调 run_onboard(force=True)。


def _sh(serial, cmd, timeout=10):
    """统一 shell 入口；返回 (rc, out)。shell 抛异常按 rc=-1 处理，不静默。"""
    try:
        rc, out = A.shell(serial, cmd, timeout=timeout)
        return rc, (out or "")
    except Exception as e:  # noqa: BLE001
        return -1, "shell-error: %s" % e


# --------------------------------------------------------------------------
# 各 facet 探针：每个返回 (value, authority, limitations)
#   value: 人类可读字符串（落到 evidence 会被截断 400 字符）
#   authority: 见上
#   limitations: 我没看到什么 / 可信度边界
# --------------------------------------------------------------------------

def system_info(serial):
    """设备自报：型号/品牌/Android 版本/ROM/SDK。全部 getprop 实物读到 ⇒ shipped-artifact。"""
    rc, out = _sh(serial, "getprop | grep -E 'ro.product.model|ro.product.brand|"
                          "ro.build.version.release|ro.build.version.sdk|ro.miui.version.name|"
                          "ro.build.flavor|ro.build.user'", timeout=8)
    if rc != 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["getprop 失败，未读到系统信息"])
    d = {}
    for line in out.splitlines():
        m = re.search(r"\[([^\]]+)\]:\s*\[([^\]]*)\]", line)
        if m:
            d[m.group(1)] = m.group(2)
    model = d.get("ro.product.model", "")
    brand = d.get("ro.product.brand", "")
    rel = d.get("ro.build.version.release", "")
    sdk = d.get("ro.build.version.sdk", "")
    miui = d.get("ro.miui.version.name", "")
    rom = "HyperOS/MIUI(%s)" % miui if miui else "AOSP-like"
    val = "model=%s brand=%s android=%s sdk=%s rom=%s" % (model, brand, rel, sdk, rom)
    lim = [] if (model and rel) else ["部分 getprop 字段缺失"]
    return (val, "shipped-artifact", lim)


def wifi_band(serial):
    """当前 WiFi 连接频段（2.4G/5G）。从 `dumpsys wifi` 解析，跑出来的 ⇒ controlled-replay。"""
    rc, out = _sh(serial, "dumpsys wifi | grep -iE 'freq|band|mFrequency|getConfiguredNetworks' | head -20",
                  timeout=10)
    if rc != 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys wifi 失败，无法判断频段"])
    freq = None
    for line in out.splitlines():
        m = re.search(r"(?:freq(?:uency)?|mFrequency)\D*(\d{4,5})", line, re.I)
        if m:
            freq = int(m.group(1))
            break
    if not freq:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys wifi 无频率字段，可能未连 WiFi 或字段名变化"])
    band = "5GHz" if freq >= 4900 else "2.4GHz"
    return ("%s (%dMHz)" % (band, freq), "controlled-replay",
            ["仅当前连接频段；多 AP/双频无法从单次 dumpsys 全读"])


def net_reach(serial):
    """外网连通性：ping 一个公共 DNS。跑出来的 ⇒ controlled-replay。"""
    rc, out = _sh(serial, "ping -c 1 -W 3 8.8.8.8", timeout=8)
    if rc == 0:
        return ("ok: 8.8.8.8 reachable", "controlled-replay", [])
    return ("fail: 8.8.8.8 unreachable (rc=%d)" % rc, "controlled-replay",
            ["ping 不通不代表无网（可能 ICMP 被丢），仅证伪外网可达"])


def camera_count(serial):
    """摄像头数：从 `dumpsys media.camera` 数 Camera ID。跑出来的 ⇒ controlled-replay。"""
    rc, out = _sh(serial, "dumpsys media.camera | grep -iE 'Camera ID|device.*camera|\\{[^}]+\\}' | head -30",
                  timeout=10)
    if rc != 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys media.camera 失败"])
    ids = set(re.findall(r"Camera ID[:\s]*(\d+)|camera_id[=:\s]*(\d+)", out, re.I))
    flat = {x for t in ids for x in t if x}
    n = len(flat)
    if n == 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys 未解析到摄像头 ID（字段名随 ROM 变）"])
    return ("%d cameras" % n, "controlled-replay", [])


def _audio_devices(serial):
    """`dumpsys audio` 看录音/放音设备是否存在。返回 (mic, speaker, raw)。"""
    rc, out = _sh(serial, "dumpsys audio | grep -iE 'Devices.*:|MIC|Speaker|Earpiece' | head -20",
                  timeout=10)
    if rc != 0:
        return (False, False, "")
    mic = bool(re.search(r"\bMIC\b|mic\b", out, re.I))
    spk = bool(re.search(r"SPEAKER|Speaker|earpiece", out, re.I))
    return (mic, spk, out[:400])


def mic_present(serial):
    mic, _spk, raw = _audio_devices(serial)
    if not raw:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys audio 失败"])
    return (("present" if mic else "absent"), "controlled-replay",
            [] if mic else ["dumpsys audio 未出现 MIC 标记，可能命名差异"])


def speaker_present(serial):
    _mic, spk, raw = _audio_devices(serial)
    if not raw:
        return ("unknown", AUTH.authority_of_probe({"alive": False}),
                ["dumpsys audio 失败"])
    return (("present" if spk else "absent"), "controlled-replay",
            [] if spk else ["dumpsys audio 未出现 Speaker 标记，可能命名差异"])


def screen_info(serial):
    """屏幕分辨率。wm size 实物读到 ⇒ shipped-artifact。"""
    rc, out = _sh(serial, "wm size", timeout=8)
    if rc != 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}), ["wm size 失败"])
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        return ("%sx%s" % (m.group(1), m.group(2)), "shipped-artifact", [])
    return ("unknown", AUTH.authority_of_probe({"alive": False}), ["wm size 解析失败"])


def root_state(serial):
    """root 状态：看 id 是否 uid 0 / 有无 su。跑出来的 ⇒ controlled-replay。"""
    rc, out = _sh(serial, "id", timeout=8)
    if rc != 0:
        return ("unknown", AUTH.authority_of_probe({"alive": False}), ["id 失败"])
    if "uid=0" in out or "root" in out:
        return ("rooted", "controlled-replay", [])
    # 有无 su
    rc2, out2 = _sh(serial, "which su 2>/dev/null; ls /sbin/su /system/bin/su 2>/dev/null", timeout=8)
    if rc2 == 0 and out2.strip():
        return ("has-su-but-not-active", "controlled-replay", [])
    return ("not-rooted", "controlled-replay", [])


def adb_keyevent(serial):
    """ADB 能否注入按键（INJECT_EVENTS）。实测 MIUI 全禁 ⇒ 这是熄屏/亮屏能力边界。"""
    rc, out = _sh(serial, "input keyevent 26", timeout=8)
    if rc == 0:
        return ("allowed", "controlled-replay", [])
    # 255 + SecurityException ⇒ INJECT_EVENTS 未授权
    if "INJECT_EVENTS" in out or "SecurityException" in out or rc == 255:
        return ("blocked: INJECT_EVENTS (rc=%d)" % rc, "controlled-replay",
                ["熄屏/亮屏类 input keyevent 在此 ROM 不可用，只能靠机主手动"])
    return ("unknown (rc=%d)" % rc, "controlled-replay", ["非预期返回码，需人工复核"])


def adb_settings(serial):
    """settings put 是否需要 WRITE_SETTINGS 放权（MIUI 默认拒，需 appops allow 救活）。"""
    # 用 timeout 键往返测试，写回原值避免副作用
    rc0, before = _sh(serial, "settings get system screen_off_timeout", timeout=8)
    try:
        test = "123456"
        rc, _ = _sh(serial, "settings put system screen_off_timeout %s" % test, timeout=8)
        # 恢复
        if rc0 == 0 and before.strip().isdigit():
            _sh(serial, "settings put system screen_off_timeout %s" % before.strip(), timeout=8)
        if rc == 0:
            return ("granted", "controlled-replay", [])
        if "WRITE_SETTINGS" in _ or rc == 255:
            return ("needs_appops", "controlled-replay",
                    ["需 `appops set com.android.shell WRITE_SETTINGS allow` 放权"])
        return ("blocked (rc=%d)" % rc, "controlled-replay", [])
    except Exception as e:  # noqa: BLE001
        return ("error: %s" % e, AUTH.authority_of_probe({"alive": False}), ["settings 往返异常"])


def adb_media_bin(serial):
    """`media` 二进制是否存在。MIUI 上不存在 ⇒ 音量得走 service call。"""
    rc, out = _sh(serial, "which media; ls /system/bin/media* 2>/dev/null", timeout=8)
    if rc == 0 and out.strip():
        return ("present: %s" % out.strip().splitlines()[0], "shipped-artifact", [])
    return ("absent", "shipped-artifact", ["MIUI 无 media 二进制，音量控制需绕路 service call audio"])


def adb_service_call_audio(serial):
    """音量控制链路与静音路径探测。**不自残**：只读 `cmd media_session volume --get`
    验证音量链路通，静音能力据隐私屏已真机实测结论标注（MIUI 无 media 二进制时
    service call audio 13 是唯一可靠静音路径）。避免自检改动设备状态。"""
    rc, out = _sh(serial, "cmd media_session volume --get", timeout=8)
    if rc != 0:
        rc, out = _sh(serial, "media volume --get 2>/dev/null || cmd media volume --get 2>/dev/null",
                      timeout=8)
    if rc != 0:
        return ("volume-read-unavailable", AUTH.authority_of_probe({"alive": False}),
                ["无法读取音量，音量控制链路可能不可用"])
    return ("volume-readable; mute-path=service call audio 13 (verified in privacy.py on MIUI)",
            "controlled-replay",
            ["未实际静音（避免自检改动设备状态）；静音有效性见 privacy.py 真机实测"])


# --------------------------------------------------------------------------
# 聚合：probe_device
# --------------------------------------------------------------------------

_FACETS = {
    "system": system_info,
    "wifi_band": wifi_band,
    "net_reach": net_reach,
    "camera": camera_count,
    "mic": mic_present,
    "speaker": speaker_present,
    "screen": screen_info,
    "root": root_state,
    "adb.keyevent": adb_keyevent,
    "adb.settings": adb_settings,
    "adb.media_bin": adb_media_bin,
    "adb.service_call_audio": adb_service_call_audio,
}


def _cache_path(serial):
    h = hashlib.sha256(serial.encode("utf-8", "replace")).hexdigest()[:12]
    return os.path.join(CACHE_DIR, "%s.json" % h)


def _load_cache(serial):
    """缓存存在即返回——只首次接入跑；之后谁调都直接复用，不重探。"""
    try:
        with open(_cache_path(serial), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(serial, rec):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(serial), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def probe_device(serial, force=False):
    """接入新设备时跑一遍全量自检。

    返回 {
      "serial": str, "ts": int, "from_cache": bool,
      "facts": { facet: {"value","authority","limitations","evidence_id"} },
      "summary": str   # 一行能力摘要，便于人读 / 接日志
    }
    """
    if not force:
        cached = _load_cache(serial)
        if cached:
            cached["from_cache"] = True
            return cached

    facts = {}
    for facet, fn in _FACETS.items():
        value, authority, limitations = fn(serial)
        ev = AUTH.mk_evidence("device.probe", facet, value, authority,
                              subject=serial, limitations=limitations)
        facts[facet] = {
            "value": ev.value,
            "authority": ev.authority,
            "limitations": ev.limitations,
            "evidence_id": ev.id,
        }

    # 能力摘要：把关键限制点挑出来
    limits = []
    if facts["adb.keyevent"]["value"].startswith("blocked"):
        limits.append("ADB 无法注入按键(熄屏/亮屏靠手动)")
    if facts["adb.settings"]["value"] == "needs_appops":
        limits.append("settings 写需放权")
    if facts["adb.media_bin"]["value"] == "absent":
        limits.append("无 media 二进制(走 service call 静音)")
    if facts["mic"]["value"] == "absent":
        limits.append("无麦克风")
    if facts["speaker"]["value"] == "absent":
        limits.append("无扬声器")
    summary = "cap: cam=%s mic=%s spk=%s root=%s | adbkey=%s set=%s mute=%s | %s" % (
        facts["camera"]["value"], facts["mic"]["value"], facts["speaker"]["value"],
        facts["root"]["value"], facts["adb.keyevent"]["value"],
        facts["adb.settings"]["value"], facts["adb.service_call_audio"]["value"],
        "; ".join(limits) or "no known limit",
    )

    rec = {"serial": serial, "ts": int(time.time()), "from_cache": False,
           "facts": facts, "summary": summary}
    _save_cache(serial, rec)
    return rec


def run_onboard(serial):
    """接入钩子：首次发现即跑全量（force=True 不读缓存），结果落缓存后可被随时取。"""
    return probe_device(serial, force=True)


# --------------------------------------------------------------------------
# 自测（不依赖真机）：mock A.shell，验证 authority 标级 + 缓存幂等 + unknown
# --------------------------------------------------------------------------

def _selftest():
    calls = {"n": 0}

    def fake_shell(serial, cmd, timeout=10):
        calls["n"] += 1
        if "getprop" in cmd:
            return 0, "[ro.product.model]: [Xiaomi 14]\n[ro.build.version.release]: [14]"
        if "input keyevent" in cmd:
            return 255, "SecurityException: INJECT_EVENTS"
        if "dumpsys media.camera" in cmd:
            return 0, "Camera ID: 3"
        if "wm size" in cmd:
            return 0, "Physical size: 1080x2400"
        if "id" in cmd:
            return 0, "uid=2000(shell)"
        if "settings get" in cmd:
            return 0, "600000"
        if "which media" in cmd:
            return 1, ""
        if "service call audio" in cmd:
            return 0, ""
        if "dumpsys audio" in cmd:
            return 0, "Devices: Speaker"
        if "ping" in cmd:
            return 0, "64 bytes"
        if "dumpsys wifi" in cmd:
            return 0, "mFrequency=5180"
        return 0, ""

    global A
    A = type("A", (), {"shell": staticmethod(fake_shell)})()

    r = run_onboard("TEST")
    assert r["summary"], r
    assert r["facts"]["system"]["authority"] == "shipped-artifact", r
    assert r["facts"]["adb.keyevent"]["value"].startswith("blocked"), r
    assert r["facts"]["adb.keyevent"]["authority"] == "controlled-replay", r
    assert r["facts"]["camera"]["value"] == "1 cameras", r  # 解析到 ID 3 ⇒ 1 个
    assert r["facts"]["wifi_band"]["value"].startswith("5GHz"), r
    # 缓存：二次调用不跑真命令
    r2 = probe_device("TEST")
    assert r2["from_cache"] is True, r2
    print("device_probe selftest OK (probe calls in onboard: %d)" % calls["n"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("-t", "--test", "selftest"):
        _selftest()
    elif len(sys.argv) > 1:
        serial = sys.argv[1]
        rec = run_onboard(serial)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        print("usage: python device_probe.py <serial> | -t", file=sys.stderr)
