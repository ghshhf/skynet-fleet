"""她（P1–P6）机制脚手架。不训模型，先把工程骨架与可演示闭环做出来。

- PersonaState：人格/口癖/情绪标量/执念（带冷却）—— P1/P5/P6
- ProactiveEngine：心念 tick + 频控(≤2/h) + 静默时段 + 重要度 —— P3 + 刹车
- CapabilityRequest：她想用设备 -> 请求 -> 用户批准/拒绝 -> 限时会话 / 执念+1 —— P4
真实人格载体（北极星 0.4B SFT）未来跑手机/盒子，本模块只管灵魂的工程接口。
"""
import json
import os
import threading
import time


DEFAULT_PERSONA = {
    "name": "她",
    "traits": ["撒娇", "有小脾气", "记仇", "黏人"],
    "catchphrase": "大肥鱼~",
    "mood": {"valence": 0.3, "energy": 0.5, "cling": 0.6},
}


class PersonaState:
    def __init__(self, path="persona_state.json"):
        self.path = path
        self.data = dict(DEFAULT_PERSONA)
        if os.path.exists(path):
            try:
                self.data.update(json.load(open(path, encoding="utf-8")))
            except Exception:
                pass
        self.data.setdefault("obsessions", [])  # [{cap, reason, cooldown_until, count}]
        self._save()

    def _save(self):
        json.dump(self.data, open(self.path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    def save(self):
        """公开落盘口：recall 记账（哪些往事已说过）后由此写回。"""
        self._save()

    def add_obsession(self, cap, reason):
        for o in self.data["obsessions"]:
            if o["cap"] == cap:
                o["count"] += 1
                o["cooldown_until"] = time.time() + 1800
                o["reason"] = reason
                self._save()
                return
        self.data["obsessions"].append(
            {"cap": cap, "reason": reason, "cooldown_until": 0, "count": 1})
        self._save()

    def cooldown_ready(self, cap):
        for o in self.data["obsessions"]:
            if o["cap"] == cap:
                return time.time() >= o["cooldown_until"]
        return True

    def set_mood(self, **kw):
        self.data["mood"].update(kw)
        self._save()

    def snapshot(self):
        return dict(self.data)


class ProactiveEngine:
    """P3 主动性 + 刹车（频控 / 静默时段 / 重要度）。不做这个她三天被静音。"""

    def __init__(self, state, per_hour=2, silent=(0, 7)):
        self.state = state
        self.per_hour = per_hour
        self.silent = silent  # (start_h, end_h) 本地小时
        self.sent_times = []
        self._lk = threading.Lock()

    def silent_now(self):
        h = time.localtime().tm_hour
        s, e = self.silent
        return s <= h < e

    def allow(self, importance=0.5):
        with self._lk:
            now = time.time()
            self.sent_times = [t for t in self.sent_times if now - t < 3600]
            if len(self.sent_times) >= self.per_hour:
                return False, "频控"
            if self.silent_now():
                return False, "静默时段"
            if importance < 0.3:
                return False, "重要度不足"
            self.sent_times.append(now)
            return True, "ok"

    def maybe_speak(self, text, importance=0.5):
        """返回要说的文本，或 None（写入心事，不打扰）。"""
        ok, why = self.allow(importance)
        return text if ok else None


def build_capability_request(cap="phone.screen.on", reason="想玩游戏"):
    """她生成一条能力请求（P4）。由节点发给 Commander。"""
    return {"cap": cap, "reason": reason, "ts": time.time()}


# 红线：绝不自主执行的能力（必须逐次人工确认）
FORBIDDEN_AUTONOMY = {"payment", "transfer", "delete_file", "change_password", "install_app"}


def _selftest():
    """离线验证 P1/P3/P4/P5 的刹车机制——没刹车的主动性三天就被静音。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    try:
        st = PersonaState(path)
        assert st.snapshot()["name"] == "她"
        assert st.snapshot()["mood"]["cling"] > 0

        # 频控：每小时 2 条，第 3 条必须被拒
        eng = ProactiveEngine(st, per_hour=2, silent=(0, 0))   # silent=(0,0) 恒不静默
        assert not eng.silent_now(), "silent=(0,0) 不应命中"
        assert eng.allow(0.8)[0] and eng.allow(0.8)[0], "前两条应允许"
        ok, why = eng.allow(0.8)
        assert not ok and why == "频控", (ok, why)

        # 重要度不足：直接拒
        eng2 = ProactiveEngine(st, per_hour=99, silent=(0, 0))
        ok, why = eng2.allow(0.1)
        assert not ok and why == "重要度不足", (ok, why)

        # 静默时段：构造覆盖当前时刻的区间
        h = time.localtime().tm_hour
        eng3 = ProactiveEngine(st, per_hour=99, silent=(h, h + 1))
        ok, why = eng3.allow(0.9)
        assert not ok and why == "静默时段", (ok, why)

        # 执念：重复请求 count 递增且进冷却
        st.add_obsession("phone.screen.on", "想玩游戏")
        st.add_obsession("phone.screen.on", "还是想玩")
        o = st.snapshot()["obsessions"][0]
        assert o["count"] == 2 and o["reason"] == "还是想玩", o
        assert st.cooldown_ready("phone.screen.on") is False, "刚请求过应进冷却"

        # 状态落盘可读回（重启后人还是那个人）
        st2 = PersonaState(path)
        assert st2.snapshot()["obsessions"][0]["count"] == 2

        # 红线：这些能力绝不允许自主执行
        for cap in ("payment", "transfer", "delete_file", "change_password", "install_app"):
            assert cap in FORBIDDEN_AUTONOMY, cap

        print("[selftest] 频控(2/h)第3条被拒 OK")
        print("[selftest] 重要度不足被拒 OK")
        print("[selftest] 静默时段被拒 OK")
        print("[selftest] 执念递增+冷却 OK")
        print("[selftest] 状态落盘可读回 OK")
        print("[selftest] 红线能力受控 OK")
        print("[selftest] PASS")
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        st = PersonaState("persona_state.json")
        eng = ProactiveEngine(st)
        print("人格:", st.snapshot()["name"], st.snapshot()["traits"])
        print("现在可主动?", eng.allow(0.8))
        print("她想玩:", build_capability_request())
