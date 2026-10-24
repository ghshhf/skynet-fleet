"""红外码库：把「打开电视」变成「发指定品牌的红外码」。

infrared.py 只有 NEC 编码生成函数，只能手算 addr/cmd。
码库的作用：用户只说「小米电视 开机」，不用知道 0x04 0x08。

码库格式（community IRDB / SmartIR 兼容的子集）：
  {
    "brand": "小米",
    "device": "电视",
    "protocol": "nec",
    "keys": {
      "power": {"addr": 4, "cmd": 8},
      "vol_up": {"addr": 4, "cmd": 9},
      ...
    }
  }

为什么不用现成的码库文件拼：
  · SmartIR 用 YAML+base64 pattern，体积大、本仓更喜欢「纯数据 JSON」；
  · LIRC / IRDB 是纯文本，格式不一。这里只收「最常见品牌 × 最关键按键」。
  · 没有的品牌 → 走 learn（如果硬件支持）或直接报「不支持」，不瞎编。

本机红外收发器是 NEC-only（38kHz），没有学习模式（25060RK16C 只有发射口），
所以码库是唯一路线——实测码表来自社区公开发布，不是破解。
"""
import json
import os

# 码库目录：ir_codes/ 下每个品牌一张 JSON
CODEBOOK_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ir_codes")

# 常用键名映射（标准化输入）
_KEY_ALIASES = {
    "on": "power", "off": "power", "power": "power",
    "电源": "power", "开机": "power", "关机": "power", "关机": "power",
    "vol+": "vol_up", "volup": "vol_up", "音量加": "vol_up", "音量+" : "vol_up",
    "vol-": "vol_down", "voldown": "vol_down", "音量减": "vol_down", "音量-": "vol_down",
    "ok": "ok", "确定": "ok", "回车": "ok",
    "up": "up", "下": "down", "left": "left", "right": "right",
    "菜单": "menu", "回家": "home", "主页": "home",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9", "0": "0",
}


def _normalize_key(key):
    """把各种写法统一成码表里的键。"""
    k = (key or "").strip().lower()
    return _KEY_ALIASES.get(k, k)


def load_codebook(brand, device_type=None):
    """加载一张品牌码库。

    查找策略：
      1. ir_codes/<brand>.json  → 精确
      2. ir_codes/<brand>*/*.json → 子目录（如小米/电视.json）

    返回 codebook dict 或 None。
    """
    if not os.path.isdir(CODEBOOK_DIR):
        return None

    # 直接文件
    path = os.path.join(CODEBOOK_DIR, f"{brand}.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cb = json.load(f)
            if device_type and cb.get("device") != device_type and cb.get("device") != "generic":
                return None
            return cb
        except (OSError, ValueError):
            pass

    # 子目录
    sub = os.path.join(CODEBOOK_DIR, brand)
    if os.path.isdir(sub):
        targets = []
        if device_type:
            targets.append(os.path.join(sub, f"{device_type}.json"))
            targets.append(os.path.join(sub, "generic.json"))
        else:
            targets.append(os.path.join(sub, "default.json"))
            targets.append(os.path.join(sub, "generic.json"))
        for t in targets:
            if os.path.isfile(t):
                try:
                    with open(t, encoding="utf-8") as f:
                        return json.load(f)
                except (OSError, ValueError):
                    pass
    return None


def lookup(brand, device_type, key):
    """从码库查一个按键的 addr/cmd。

    返回 (protocol, addr, cmd) 或 (None, None, None)。
    """
    cb = load_codebook(brand, device_type)
    if cb is None:
        return None, None, None
    keys = cb.get("keys", {})
    norm = _normalize_key(key)
    entry = keys.get(norm)
    if entry is None and isinstance(keys.get(_normalize_key(key)), dict):
        entry = keys[_normalize_key(key)]
    if entry is None:
        # 直接试原始 key
        entry = keys.get(key)
    if entry is None:
        return None, None, None
    if isinstance(entry, dict):
        return (entry.get("protocol", cb.get("protocol", "nec")),
                entry.get("addr", 0), entry.get("cmd", 0))
    if isinstance(entry, list) and len(entry) >= 2:
        # 简写 [addr, cmd]
        return cb.get("protocol", "nec"), entry[0], entry[1]
    return None, None, None


def available_brands():
    """列出有码库的品牌（供能力层报给用户）。"""
    if not os.path.isdir(CODEBOOK_DIR):
        return []
    brands = []
    for name in sorted(os.listdir(CODEBOOK_DIR)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(CODEBOOK_DIR, name), encoding="utf-8") as f:
                    cb = json.load(f)
                brands.append({"brand": cb.get("brand", name[:-5]),
                               "device": cb.get("device", "未知"),
                               "keys": len(cb.get("keys", {}))})
            except ValueError:
                pass
    for sub in sorted(os.listdir(CODEBOOK_DIR)):
        subpath = os.path.join(CODEBOOK_DIR, sub)
        if os.path.isdir(subpath):
            for fn in sorted(os.listdir(subpath)):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(subpath, fn), encoding="utf-8") as f:
                            cb = json.load(f)
                        brands.append({"brand": cb.get("brand", sub),
                                       "device": cb.get("device", fn[:-5]),
                                       "keys": len(cb.get("keys", {}))})
                    except ValueError:
                        pass
    return brands


# ---------------------------------------------------------------- 自测

def _selftest():
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [FAIL] {label}")

    # 键名归一化
    check(_normalize_key("电源") == "power", "电源 -> power")
    check(_normalize_key("音量+") == "vol_up", "音量+ -> vol_up")
    check(_normalize_key("volup") == "vol_up", "volup -> vol_up")
    check(_normalize_key("on") == "power", "on -> power")

    # 查找不存在的品牌 → None
    check(load_codebook("nonexistent_brand") is None, "不存在品牌返回 None")

    print(f"ir_codes 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
