"""设备凭据库：配对一次，之后免令牌。

主人指出的真问题：**令牌只在"前往新设备"那次才用**，配对完就不该反复配。
而当时的实现是 GUI 每次启动 `secrets.token_urlsafe(32)` 现生成一枚、只活内存，
电脑一重启手机就 403 —— 于是「令牌问题」天天复发，只能重新抄一遍。
那是设计缺陷，不是主人用错了。

本模块补的就是这一环：把「一次性配对码」换成「长期设备凭据」。

  · 配对码 6 位数字，TTL 300s，试爆 5 次锁 600s（同 `pair.py` 的纪律，fail-closed）
  · 换来的 `(device_id, secret)` 值得长期保存，电脑重启不变
  · 落盘的只有 `sha256(id|secret)`，明文 secret 只在 claim 那一次返回，
    之后连本进程都取不回来——泄了库也拿不到能用的凭据
  · 撤销即时生效，不存在「缓存里还能用一会儿」

为什么不做成"把 GUI 令牌写死到文件"：那等于给所有人同一把钥匙。
设备凭据是**一台设备一把**，可单独撤销、能看到谁在什么时候连过，
这才是配对这件事值得存在的原因。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time

# ---------------------------------------------------------------- 常量

#: 与 fleet.pair 保持同一档纪律：不另发明一套，运维只需要记一组数
CODE_TTL = 300.0          # 配对码有效期（秒）
CODE_MAX_FAILS = 5        # 试爆上限
CODE_LOCK_SECS = 600.0    # 锁定时长（秒）
#: 设备密钥熵（字节）→ token_urlsafe 约 32 字符
SECRET_BYTES = 24
#: 落库节流：设备每次请求都会 verify，每次都写盘会把磁盘写烂
TOUCH_SECS = 60.0

DEVICE_PREFIX = "dev-"
SECRET_PREFIX = "fw_"

#: 默认库位置。跟着用户走而不是跟着仓库走——换目录跑 GUI 不该丢配对。
#:
#: ⚠️ 文件名**必须**和 fleet/catalog.py 的 DEVICES_PATH 区分开。
#: 曾经两边都指向 ~/.fleet/devices.json：一个是「见过的设备」（kind/via/caps/
#: first_seen…），一个是「配对凭据」（hash/secret_head/platform），schema 不同、
#: 各自整文件覆盖。结果对方一落盘就把凭据表抹平，表现是手机端「凭据被拒」，
#: 而且 _load 只收带 hash 的记录 → 读回来是空库，人会以为配对从来没成功过。
#: 这类「两个模块抢一个状态文件」的 bug 不会报错，只会安静丢数据，所以在这里
#: 写死分开，并在 _selftest 里断言两者不相等。
DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "device_auth.json")


# ---------------------------------------------------------------- 工具

def new_device_id():
    return DEVICE_PREFIX + secrets.token_hex(6)


def new_secret():
    """明文密钥的形态：`fw_` + 32 字符。前缀是为了肉眼可辨（不是令牌、不是 key）。"""
    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


def _hash(device_id, secret):
    """凭据指纹。id 参与哈希：偷到哈希也没法挪到另一个 id 上复用。"""
    raw = ("%s|%s" % (device_id, secret)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_header(value):
    """把 `X-Fleet-Device: dev-xxx:fw_yyy` 拆成 (id, secret)；不合法返回 None。

    分隔符用**第一个**冒号切：secret 是 token_urlsafe，不含冒号，但
    假设它永远不含是脆的，切第一个冒号能容错后半段出现冒号。
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if ":" not in v:
        return None
    did, _, sec = v.partition(":")
    did, sec = did.strip(), sec.strip()
    if not did.startswith(DEVICE_PREFIX) or not sec:
        return None
    return did, sec


# ---------------------------------------------------------------- 库

class DeviceVault:
    """设备凭据库。线程安全：GUI 线程发码、网络线程 claim/verify 是并发常态。

    用法：
        v = DeviceVault(name="PC-A")
        code = v.new_code()                 # 电脑屏幕上显示这 6 位
        r = v.claim(code, "红米", "android")  # 手机提交 → {ok, device_id, secret}
        v.verify(r["device_id"], r["secret"])  # 之后每次请求
        v.revoke(r["device_id"])               # 一键踢掉这台
    """

    def __init__(self, path=None, name="", ttl=CODE_TTL,
                 max_fails=CODE_MAX_FAILS, lock_secs=CODE_LOCK_SECS):
        self.path = os.path.abspath(path or os.environ.get("FLEET_DEVICE_DB")
                                    or DEFAULT_PATH)
        self.name = str(name or "")
        self.ttl = float(ttl)
        self.max_fails = int(max_fails)
        self.lock_secs = float(lock_secs)
        self._lk = threading.Lock()
        self._devices = {}          # id -> record
        self._code = ""
        self._code_at = 0.0
        self._fails = 0
        self._locked_until = 0.0
        self._load()

    # ---------------- 落盘 ----------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        devs = obj.get("devices") or {}
        if isinstance(devs, dict):
            for did, rec in devs.items():
                if isinstance(rec, dict) and rec.get("hash"):
                    self._devices[str(did)] = rec

    def _save(self):
        d = os.path.dirname(self.path)
        try:
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "name": self.name,
                           "devices": self._devices}, f,
                          ensure_ascii=False, indent=1, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass                     # Windows 上 chmod 语义不同，能写就行
            os.replace(tmp, self.path)
        except OSError:
            # 存不下不是致命错误：内存里还有效，只是重启会丢。不抛，
            # 免得一个只读目录把整条配对链弄成 500。
            pass

    # ---------------- 配对码 ----------------

    def new_code(self):
        """换一枚新码（GUI 点「生成配对码」）。同时清零试爆计数、解锁。"""
        with self._lk:
            self._code = "%06d" % (secrets.randbelow(900000) + 100000)
            self._code_at = time.time()
            self._fails = 0
            self._locked_until = 0.0
            return self._code

    def current_code(self):
        """当前码原文——**只给本机 GUI 显示**，没有任何 HTTP 端点吐它。

        手机是拿着肉眼看到的码来 claim 的。码一旦能通过网络读，
        配对就退化成「谁先连上谁赢」。
        """
        with self._lk:
            if not self._code:
                return ""
            if time.time() - self._code_at > self.ttl:
                return ""
            return self._code

    def code_state(self):
        with self._lk:
            now = time.time()
            return {"has_code": bool(self._code) and (now - self._code_at <= self.ttl),
                    "expires_in": max(0.0, round(self._code_at + self.ttl - now, 1))
                    if self._code else 0.0,
                    "locked_for": max(0.0, round(self._locked_until - now, 1)),
                    "fails": self._fails}

    def _check_code(self, code, now):
        if self._locked_until > now:
            return False, "配对码被锁了（试错太多次），还有 %.0f 秒" \
                          % (self._locked_until - now)
        if not self._code:
            return False, "电脑上还没生成配对码（点「生成配对码」）"
        if now - self._code_at > self.ttl:
            return False, "配对码已过期（有效期 %.0f 秒），电脑上重新生成一枚" % self.ttl
        if not secrets.compare_digest(str(code or "").strip(), self._code):
            self._fails += 1
            if self._fails >= self.max_fails:
                self._locked_until = now + self.lock_secs
                return False, "配对码错太多次，锁 %.0f 秒" % self.lock_secs
            return False, "配对码不对（还剩 %d 次机会）" % (self.max_fails - self._fails)
        return True, ""

    # ---------------- 握手 ----------------

    def claim(self, code, name="", platform=""):
        """设备持码换长期凭据。成功 → {ok, device_id, secret, device}。

        码是一次性的：成功即作废。防的是「偷看一眼屏幕，之后反复进」。
        """
        with self._lk:
            now = time.time()
            ok, err = self._check_code(code, now)
            if not ok:
                return {"ok": False, "err": err}
            did = new_device_id()
            while did in self._devices:      # 撞了就换，不覆盖老设备
                did = new_device_id()
            sec = new_secret()
            rec = {"id": did, "hash": _hash(did, sec),
                   "name": str(name or "未命名设备")[:48],
                   "platform": str(platform or "")[:16],
                   "created_at": now, "last_seen": now,
                   "secret_head": sec[:len(SECRET_PREFIX) + 6]}
            self._devices[did] = rec
            self._code = ""                  # 一次性
            self._fails = 0
            self._save()
            return {"ok": True, "device_id": did, "secret": sec,
                    "device": self._public(rec)}

    def verify(self, device_id, secret):
        """校验长期凭据。有效 → 设备公开信息（含 id）；无效 → None。

        故意不区分「id 不存在」和「密钥不对」：对外都是 403 一句话，
        否则就是个免费的用户名枚举器。
        """
        did = str(device_id or "")
        if not did or not secret:
            return None
        with self._lk:
            rec = self._devices.get(did)
            if rec is None:
                return None
            if not secrets.compare_digest(str(rec.get("hash") or ""),
                                          _hash(did, str(secret))):
                return None
            now = time.time()
            dirty = False
            if now - float(rec.get("last_seen") or 0) > TOUCH_SECS:
                rec["last_seen"] = now
                dirty = True
            out = self._public(rec)
            out["id"] = did                  # verify 才回 id：调用方要拿它做审计
            if dirty:
                self._save()
            return out

    # ---------------- 管理 ----------------

    def list(self):
        with self._lk:
            rows = [self._public(r) for r in self._devices.values()]
            rows.sort(key=lambda r: -(r.get("last_seen") or 0))
            return rows

    def revoke(self, device_id):
        """掐断一台设备。幂等：不存在也返回 True（熔断不该报错）。"""
        with self._lk:
            gone = self._devices.pop(str(device_id or ""), None)
            if gone is None:
                return True
            self._save()
            return True

    def revoke_all(self):
        with self._lk:
            n = len(self._devices)
            self._devices.clear()
            if n:
                self._save()
            return n

    def stats(self):
        with self._lk:
            return {"devices": len(self._devices), "path": self.path,
                    **self.code_state()}

    @staticmethod
    def _public(rec):
        """对外形态：**没有 hash**。hash 只在库里，出门就摘掉。"""
        return {"id": rec.get("id", ""), "name": rec.get("name", ""),
                "platform": rec.get("platform", ""),
                "created_at": rec.get("created_at", 0),
                "last_seen": rec.get("last_seen", 0),
                "secret_head": rec.get("secret_head", "")}


# ---------------------------------------------------------------- 自测

def _selftest():
    import tempfile
    ok = fail = 0

    def chk(cond, what):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [X] %s" % what)

    tmpd = tempfile.mkdtemp(prefix="devvault_")
    p = os.path.join(tmpd, "devices.json")
    v = DeviceVault(path=p, name="PC-A")

    chk(v.list() == [], "空库列表为空")
    chk(v.current_code() == "", "未生成码时为空")
    code = v.new_code()
    chk(len(code) == 6 and code.isdigit(), "码是 6 位数字")

    r = v.claim(code, "红米", "android")
    chk(r["ok"] and r["device_id"].startswith("dev-"), "claim 发设备凭据")
    chk(r["secret"].startswith("fw_") and len(r["secret"]) > 20, "secret 形态对")
    chk(v.claim(code, "别的", "")["ok"] is False, "码一次性：用过即废")

    did, sec = r["device_id"], r["secret"]
    chk(v.verify(did, sec) is not None, "正确凭据通过")
    chk(v.verify(did, sec + "x") is None, "错密钥拒绝")
    chk(v.verify("dev-nope", sec) is None, "未知 id 拒绝")
    chk(v.verify(did, "") is None, "空密钥拒绝")

    # 落盘 → 新实例读回：模拟电脑重启
    v2 = DeviceVault(path=p, name="PC-A")
    chk(v2.verify(did, sec) is not None, "重启后凭据仍有效（这是本模块存在的理由）")
    chk(len(v2.list()) == 1, "重启后设备表还在")
    chk("hash" not in v2.list()[0], "对外形态不含 hash")
    raw = open(p, encoding="utf-8").read()
    chk(sec not in raw, "明文密钥不落盘")
    chk("hash" in raw, "库里存的是哈希")

    # 撤销
    v2.revoke(did)
    chk(v2.verify(did, sec) is None, "撤销即时生效")
    chk(DeviceVault(path=p).verify(did, sec) is None, "撤销已落盘")
    chk(v2.revoke(did) is True, "撤销幂等")

    # 试爆锁
    v3 = DeviceVault(path=os.path.join(tmpd, "b.json"))
    c3 = v3.new_code()
    wrong = "000000" if c3 != "000000" else "111111"
    for i in range(CODE_MAX_FAILS):
        v3.claim(wrong, "attacker", "")
    chk(v3.code_state()["locked_for"] > 0, "试爆 %d 次后锁定" % CODE_MAX_FAILS)
    chk(v3.claim(c3, "晚到的", "")["ok"] is False, "锁定时连正确的码也拒")
    c4 = v3.new_code()
    chk(v3.claim(c4, "主人手机", "android")["ok"], "刷新码即解锁")

    # 禁明文外泄：/api/pair/me 之类只能给 secret_head
    chk(len(v3.list()[0]["secret_head"]) < 16, "列表只露密钥前缀")

    # 过期
    v4 = DeviceVault(path=os.path.join(tmpd, "c.json"), ttl=0.05)
    c5 = v4.new_code()
    time.sleep(0.08)
    chk(v4.claim(c5, "慢", "")["ok"] is False, "过期码拒收")
    chk(v4.current_code() == "", "过期后 current_code 返回空")

    # header 解析
    chk(parse_header("%s:%s" % (did, sec)) == (did, sec), "解析 header")
    chk(parse_header("abc:def") is None, "缺前缀当非法")
    chk(parse_header("dev-x") is None, "缺密钥当非法")
    chk(parse_header(None) is None, "None 当非法")

    # ★ 回归守卫：凭据库和「见过的设备」表必须是两个文件。
    # 撞在一起时谁后写谁覆盖，凭据会被静默抹掉（真踩过，表现是手机端「凭据被拒」）。
    try:
        from .catalog import DEVICES_PATH as _SEEN_PATH
    except Exception:
        _SEEN_PATH = ""
    chk(bool(_SEEN_PATH) and os.path.abspath(DEFAULT_PATH) != os.path.abspath(_SEEN_PATH),
        "凭据库与设备表不能是同一个文件（DEFAULT_PATH=%s, DEVICES_PATH=%s）"
        % (DEFAULT_PATH, _SEEN_PATH))

    print("\n设备凭据库自测：%d 通过，%d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
