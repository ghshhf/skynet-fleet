"""adskip 规则库 · 带版本与回滚（补李跳跳「规则停更即废」的短板）。

李跳跳的规则是静态 JSON，作者停更就全过期。这里把规则做成**可版本化资产**：
每次更新生成新版本，保留历史，可回滚；编队内任一设备更新、其余拉取即可。

存储：单个 JSON 文件（默认 adskip_rules.json，可用 FLEET_RULES 覆盖）。
纯 stdlib。用法：
  python rules_relay.py --put rules_new.json --note "补XX视频2026版"
  python rules_relay.py --get                 # 打印当前版本规则
  python rules_relay.py --get --version 2
  python rules_relay.py --rollback 1
  python rules_relay.py --selftest
"""
import argparse
import json
import os
import sys
import tempfile
import time

DEFAULT_PATH = os.environ.get("FLEET_RULES", "adskip_rules.json")


def _load(path):
    if not os.path.exists(path):
        return {"version": 0, "current": 0, "history": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(store, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def validate(rules):
    """规则必须是 list，每项含 triggers 列表（或兼容单 trigger）。"""
    if not isinstance(rules, list):
        return False, "规则应为 list"
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            return False, f"规则[{i}] 不是对象"
        trs = r.get("triggers")
        if trs is None and "text" not in r and "resourceId" not in r:
            return False, f"规则[{i}] 缺 triggers"
        if trs is not None and not isinstance(trs, list):
            return False, f"规则[{i}].triggers 应为 list"
    return True, ""


def put(rules, note="", path=DEFAULT_PATH):
    ok, err = validate(rules)
    if not ok:
        raise ValueError(err)
    store = _load(path)
    v = store["version"] + 1
    store["version"] = v
    store["current"] = v
    store["history"].append({"version": v, "ts": int(time.time()),
                             "note": note, "rules": rules})
    _save(store, path)
    return v


def get(version=None, path=DEFAULT_PATH):
    store = _load(path)
    v = version if version is not None else store["current"]
    for h in store["history"]:
        if h["version"] == v:
            return h["rules"]
    return None


def rollback(version, path=DEFAULT_PATH):
    store = _load(path)
    if not any(h["version"] == version for h in store["history"]):
        raise ValueError(f"版本 {version} 不存在")
    # 回滚 = 以旧版本内容生成一个新版本（保留历史，不丢失中间版本）
    return put(get(version, path), note=f"rollback->{version}", path=path)


def list_versions(path=DEFAULT_PATH):
    store = _load(path)
    return [(h["version"], h["ts"], h["note"]) for h in reversed(store["history"])]


def _selftest():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    try:
        v1 = put([{"packageName": "com.a", "triggers": [
            {"name": "跳过", "text": ["跳过"]}]}], note="初版", path=path)
        v2 = put([{"packageName": "com.a", "triggers": [
            {"name": "跳过", "text": ["跳过", "跳过广告"],
             "textRegex": r"跳过|skip"}]}], note="补正则", path=path)
        assert (v1, v2) == (1, 2), (v1, v2)
        cur = get(path=path)
        assert cur[0]["triggers"][0].get("textRegex"), "当前应为v2"
        assert get(1, path)[0]["triggers"][0].get("textRegex") is None, "v1 不应有正则"
        v3 = rollback(1, path=path)
        assert v3 == 3, v3
        assert get(path=path)[0]["triggers"][0].get("textRegex") is None, "回滚后应为v1内容"
        assert len(list_versions(path)) == 3, list_versions(path)
        bad, err = validate({"not": "list"})
        assert not bad and err, (bad, err)
        print("[selftest] 版本递增 v1->v2 OK")
        print("[selftest] 读当前/读历史版本 OK")
        print("[selftest] 回滚=生成新版本(保留历史) OK")
        print("[selftest] 非法规则被拒 OK")
        print("[selftest] PASS")
    finally:
        if os.path.exists(path):
            os.remove(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--put", default=None, help="规则 JSON 文件路径")
    ap.add_argument("--note", default="")
    ap.add_argument("--get", action="store_true")
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--rollback", type=int, default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--path", default=DEFAULT_PATH)
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    if a.put:
        with open(a.put, "r", encoding="utf-8") as f:
            rules = json.load(f)
        print("新版本:", put(rules, a.note, a.path))
    elif a.rollback is not None:
        print("回滚生成新版本:", rollback(a.rollback, a.path))
    elif a.get:
        print(json.dumps(get(a.version, a.path), ensure_ascii=False, indent=2))
    elif a.list:
        for v, ts, note in list_versions(a.path):
            print(f"v{v}\t{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}\t{note}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
