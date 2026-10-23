"""扫码入网：一次性、可离线验签的入网载荷（二维码只负责搬一段自证凭据）。

对应计划书 §一「手机升格为核心控制面」与 §八·一「入网注入」：新设备入网不该让用户
在那台机器上敲密码——手机出示二维码，设备扫一下就能装、就能连。二维码里那串东西
必须自己站得住，于是载荷满足四条：

- **可离线验签**：目标端不联网、不回调控制面。拿共享密钥算 HMAC-SHA256 就能确认
  「这确实是中心签的」，网络不通也能当场判真假。
- **一次性**：同一份载荷兑换过就作废（兑换落本地账本），截图外流也只是一张废纸。
- **有时效**：默认 10 分钟，过期即废；过期不是靠对方时钟绝对准确，而是靠签名后的
  时间戳字段——改了时间戳签名即碎。
- **绑节点**：载荷里写死节点名与平台，A 的载荷装不成 B。

载荷里**不放用户密码**：只带一枚随机 PSK，装完之后节点用 PSK 认证，安装密码不再
出现在计划任务参数里（原来 `--password` 是明文写在 XML 里的）。

两段生命周期必须分开，否则会出「重启即失联」的坑：

| 东西 | 寿命 | 判据 |
|---|---|---|
| 入网载荷（二维码） | 默认 10 分钟 | 签名里的 `e` 字段 |
| PSK（节点凭据） | 长期，直到中心撤销 | 中心侧账本，无 `e` 即不过期 |

若 PSK 跟着二维码一起 10 分钟过期，节点一重启就再也连不上——这是设计上必须避开的
错误，故 PSK 默认不设过期，撤销走中心（`enroll_revoke`），而不是等它自己烂掉。

诚实边界：一次性判重靠**兑换方本地账本**。同一张二维码被拿到两台机器上分别兑换，
两台各自账本都会放行（无中心时做不到全局去重）。真正的兜底在中心侧——同一枚 PSK
只绑一个节点名，第二个名字拿去认证必然失败。
"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time

VERSION = 1
PREFIX = "fleet://enroll?"
DEFAULT_TTL = 600            # 二维码有效期：10 分钟，够走到那台机器
NONCE_KEEP = 7 * 86400       # 兑换账本保留期（过期载荷重放无意义，留着只为审计）
REQUIRED = ("v", "n", "p", "h", "o", "k", "e", "j")

_LK = threading.Lock()       # 账本读改写串行化（控制面 REPL 与服务线程会并发）


class EnrollError(Exception):
    """入网载荷不可用。reason 为机器可读原因码，上层据此给人话提示。"""

    def __init__(self, reason, msg=""):
        self.reason = reason
        self.msg = msg or reason
        super().__init__(f"{reason}: {self.msg}")


# ---------------------------------------------------------------- 共享密钥

def secret_path():
    """中心与目标端共享的签名密钥位置（可用环境变量覆盖，便于换机器/测试）。"""
    p = os.environ.get("FLEET_ENROLL_SECRET_FILE")
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".fleet", "enroll_secret")


def load_secret(create=True):
    """读共享密钥；首次调用自动生成 32 字节随机密钥并落盘（仅本人可读）。

    `FLEET_ENROLL_SECRET` 环境变量优先——部署时用它可以完全不落盘。
    """
    env = os.environ.get("FLEET_ENROLL_SECRET")
    if env:
        return env.encode("utf-8")
    p = secret_path()
    if os.path.exists(p):
        raw = open(p, "rb").read().strip()
        if raw:
            return raw
        if not create:
            raise EnrollError("secret_empty", "共享密钥文件是空的")
    if not create:
        raise EnrollError("secret_missing", f"找不到共享密钥：{p}")
    raw = os.urandom(32).hex().encode("utf-8")
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    return raw


# ---------------------------------------------------------------- 载荷编解码

def _body(payload):
    """签名对象 = 确定性 JSON 字节（排序键 + 紧凑分隔符），避免规范化歧义。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sign(body, secret):
    return hmac.new(secret, body, hashlib.sha256).hexdigest()[:32]


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(txt):
    pad = "=" * (-len(txt) % 4)
    try:
        return base64.urlsafe_b64decode(txt + pad)
    except (ValueError, TypeError) as e:
        raise EnrollError("malformed", f"载荷不是合法 base64url：{e}")


def issue(node, platform="windows", host="127.0.0.1", port=9000,
          ttl=DEFAULT_TTL, secret=None, now=None, psk=None):
    """签发一份入网载荷（未兑换）。返回 payload dict；`encode()` 再转成二维码文本。"""
    if not node:
        raise EnrollError("missing_field", "节点名不能为空")
    now = int(now if now is not None else time.time())
    return {
        "v": VERSION,
        "n": node,
        "p": platform,
        "h": host,
        "o": int(port),
        "k": psk or os.urandom(16).hex(),   # 节点长期凭据（中心侧账本可撤销）
        "e": now + int(ttl),                # 载荷本身的失效时刻
        "j": os.urandom(8).hex(),           # 一次性编号，兑换时判重
    }


def encode(payload, secret=None):
    """payload -> `fleet://enroll?p=<b64url(body)>.<sig>`，长度可控（能进二维码）。"""
    secret = secret if secret is not None else load_secret()
    body = _body(payload)
    return f"{PREFIX}p={_b64e(body)}.{_sign(body, secret)}"


def decode(uri):
    """只做结构拆解，不验签（验签见 verify）。返回 (body 字节, 签名 hex)。"""
    if not isinstance(uri, str):
        raise EnrollError("malformed", "载荷不是字符串")
    u = uri.strip()
    if not u.startswith(PREFIX):
        raise EnrollError("malformed", f"不是入网载荷（应以 {PREFIX} 开头）")
    tok = u[len(PREFIX):]
    if not tok.startswith("p="):
        raise EnrollError("malformed", "载荷缺少 p= 参数")
    tok = tok[2:].split("&", 1)[0]
    if "." not in tok:
        raise EnrollError("malformed", "载荷缺少签名段")
    body_txt, sig = tok.rsplit(".", 1)
    return _b64d(body_txt), sig


def verify(uri, secret=None, now=None, expect_node=None):
    """离线验签。通过返回 payload；任何一项不过抛 EnrollError，不返回半成品。

    顺序有意为之：先验签名再看内容——签名不对时，里面的字段一个都不可信，
    包括它自称的版本号与过期时间。
    """
    secret = secret if secret is not None else load_secret(create=False)
    body, sig = decode(uri)
    if not hmac.compare_digest(_sign(body, secret), sig):
        raise EnrollError("bad_sig", "签名不匹配（载荷被改过，或密钥不是同一份）")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise EnrollError("malformed", f"载荷内容不是 JSON：{e}")
    if not isinstance(payload, dict):
        raise EnrollError("malformed", "载荷内容不是对象")
    miss = [k for k in REQUIRED if k not in payload]
    if miss:
        raise EnrollError("missing_field", f"载荷缺字段 {miss}")
    if int(payload["v"]) != VERSION:
        raise EnrollError("bad_version", f"载荷版本 {payload['v']} 不支持（本端 {VERSION}）")
    now = int(now if now is not None else time.time())
    if int(payload["e"]) <= now:
        raise EnrollError("expired", f"载荷已过期 {now - int(payload['e'])} 秒，重新出示二维码")
    if expect_node and payload["n"] != expect_node:
        raise EnrollError("node_mismatch", f"载荷属于 {payload['n']}，不是 {expect_node}")
    return payload


def describe(payload, now=None):
    """给人看的一行摘要（REPL / dry-run 用）。"""
    now = int(now if now is not None else time.time())
    left = int(payload["e"]) - now
    return (f"节点={payload['n']} 平台={payload['p']} 中心={payload['h']}:{payload['o']} "
            f"剩余={left}s PSK={payload['k'][:6]}… 编号={payload['j']}")


# ---------------------------------------------------------------- 一次性账本

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (ValueError, OSError) as e:
        # 账本坏了不能当空账本放行——那等于把「一次性」降级成「随便重放」。
        raise EnrollError("ledger_corrupt", f"兑换账本不可读（{path}）：{e}")
    return d if isinstance(d, dict) else default


def _save_json(path, data):
    """原子替换：写临时文件再 os.replace，断电不会留半个账本。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def redeem(uri, ledger_path, secret=None, now=None):
    """兑换：验签 + 判重 + 记账。返回 payload。

    只有真正兑换过才记账——验签失败或过期的载荷不占编号，避免被无效载荷刷账本。
    """
    secret = secret if secret is not None else load_secret(create=False)
    payload = verify(uri, secret=secret, now=now)
    now = int(now if now is not None else time.time())
    with _LK:
        d = _load_json(ledger_path, {"v": 1, "used": {}})
        used = d.setdefault("used", {})
        nonce = payload["j"]
        if nonce in used:
            raise EnrollError("replayed", f"该载荷已于 {used[nonce].get('at')} 被兑换过")
        for k in [k for k, v in used.items() if int(v.get("e", 0)) + NONCE_KEEP < now]:
            used.pop(k, None)                 # 过期载荷的重放已无意义，顺手清账
        used[nonce] = {"n": payload["n"], "e": int(payload["e"]), "at": now}
        _save_json(ledger_path, d)
    return payload


def redeem_count(ledger_path):
    try:
        return len(_load_json(ledger_path, {"used": {}}).get("used", {}))
    except EnrollError:
        return -1


# ---------------------------------------------------------------- PSK 账本（中心侧）

def psk_key(psk):
    """账本只存 PSK 的哈希：账本文件泄露不等于交出一把能连控制面的钥匙。"""
    return hashlib.sha256(psk.encode("utf-8")).hexdigest()


def register_psk(ledger_path, psk, node, exp=0, now=None):
    """登记一枚节点凭据。exp=0 表示不过期（重启后仍能认证），撤销靠 revoke。"""
    with _LK:
        d = _load_json(ledger_path, {"v": 1, "psks": {}})
        d.setdefault("psks", {})[psk_key(psk)] = {
            "n": node, "e": int(exp), "iat": int(now if now is not None else time.time()),
            "used_at": None,
        }
        _save_json(ledger_path, d)


def check_psk(ledger_path, psk, node, now=None):
    """节点认证时校验凭据：必须同一枚 PSK、同一个节点名、未过期。"""
    if not psk:
        return False
    d = _load_json(ledger_path, {"v": 1, "psks": {}})
    e = d.get("psks", {}).get(psk_key(psk))
    if not e or e.get("n") != node:
        return False
    exp = int(e.get("e", 0))
    return not exp or exp > int(now if now is not None else time.time())


def mark_used(ledger_path, psk, now=None):
    """记一下「这枚凭据第一次真正用上了」，便于事后审计。已在则不动。"""
    with _LK:
        d = _load_json(ledger_path, {"v": 1, "psks": {}})
        e = d.get("psks", {}).get(psk_key(psk))
        if e and not e.get("used_at"):
            e["used_at"] = int(now if now is not None else time.time())
            _save_json(ledger_path, d)


def revoke(ledger_path, node):
    """撤销某个节点的全部凭据，返回撤销条数（换机/设备送人时用）。"""
    with _LK:
        d = _load_json(ledger_path, {"v": 1, "psks": {}})
        psks = d.setdefault("psks", {})
        gone = [k for k, v in psks.items() if v.get("n") == node]
        for k in gone:
            psks.pop(k, None)
        if gone:
            _save_json(ledger_path, d)
        return len(gone)


def list_psks(ledger_path):
    """审计用：列出已登记的节点凭据（不含 PSK 本身，只有哈希前 8 位）。"""
    d = _load_json(ledger_path, {"v": 1, "psks": {}})
    rows = []
    for k, v in d.get("psks", {}).items():
        rows.append({"node": v.get("n"), "exp": v.get("e", 0),
                     "used_at": v.get("used_at"), "key": k[:8]})
    return sorted(rows, key=lambda r: (r["node"] or "", r["key"]))


# ---------------------------------------------------------------- 自检

def _selftest():
    """离线验证：签发/验签/篡改/过期/换钥/一次性/节点绑定/凭据账本。"""
    tdir = tempfile.mkdtemp(prefix="fleet_enroll_st_")
    secret = b"selftest-secret-0123456789abcdef"
    other = b"another-secret-0123456789abcdefg"
    led = os.path.join(tdir, "enroll_ledger.json")
    psk_led = os.path.join(tdir, "psk_ledger.json")
    now = int(time.time())
    try:
        p = issue("PC-QR", "windows", "192.168.1.10", 9000, ttl=600, secret=secret, now=now)
        uri = encode(p, secret)
        print(f"  载荷长度 {len(uri)} 字符（二维码可承载，无多行/无二进制）")
        assert len(uri) < 400, len(uri)
        assert "changeme" not in uri and "password" not in uri, "载荷不许带用户密码"

        # 1) 正常验签
        got = verify(uri, secret=secret, now=now + 5)
        assert got["n"] == "PC-QR" and got["o"] == 9000 and got["k"] == p["k"], got
        print("  离线验签通过（不联网、不回调控制面） OK")

        # 2) 篡改端口 -> 签名碎
        body, sig = decode(uri)
        tampered = json.loads(body)
        tampered["o"] = 9001
        bad = f"{PREFIX}p={_b64e(_body(tampered))}.{sig}"
        try:
            verify(bad, secret=secret, now=now)
            raise AssertionError("改端口竟然验过了")
        except EnrollError as e:
            assert e.reason == "bad_sig", e.reason
        print("  改一个字节即签名失配（bad_sig） OK")

        # 3) 时间戳也是被签的内容：把过期时间往后改，同样碎
        t2 = json.loads(body)
        t2["e"] = now + 99999
        try:
            verify(f"{PREFIX}p={_b64e(_body(t2))}.{sig}", secret=secret, now=now)
            raise AssertionError("改有效期竟然验过了")
        except EnrollError as e:
            assert e.reason == "bad_sig", e.reason
        print("  改有效期同样签名失配（有效期不可被本地改写） OK")

        # 4) 过期
        try:
            verify(uri, secret=secret, now=now + 601)
            raise AssertionError("过期载荷竟然验过了")
        except EnrollError as e:
            assert e.reason == "expired", e.reason
        print("  过期即废（expired） OK")

        # 5) 换一把密钥（不是同一个中心）
        try:
            verify(uri, secret=other, now=now)
            raise AssertionError("别的密钥竟然验过了")
        except EnrollError as e:
            assert e.reason == "bad_sig", e.reason
        print("  非本中心签发的载荷验不过（bad_sig） OK")

        # 6) 节点绑定：拿 A 的载荷装成 B
        try:
            verify(uri, secret=secret, now=now, expect_node="PC-B")
            raise AssertionError("载荷竟然跨节点通用")
        except EnrollError as e:
            assert e.reason == "node_mismatch", e.reason
        print("  A 的载荷装不成 B（node_mismatch） OK")

        # 7) 一次性：兑换两次
        r1 = redeem(uri, led, secret=secret, now=now)
        assert r1["n"] == "PC-QR"
        try:
            redeem(uri, led, secret=secret, now=now + 1)
            raise AssertionError("同一张码兑换了两次")
        except EnrollError as e:
            assert e.reason == "replayed", e.reason
        print(f"  一次性兑换（第二次 replayed，账本 {redeem_count(led)} 条） OK")

        # 8) 另一张码不受前一张影响（账本不误伤）
        p2 = issue("Box-2", "android", "192.168.1.10", 9000, secret=secret, now=now)
        redeem(encode(p2, secret), led, secret=secret, now=now)
        assert redeem_count(led) == 2, redeem_count(led)
        print("  不同载荷互不影响（账本按编号去重） OK")

        # 9) 验签失败不记账（无效载荷刷不爆账本）
        p3 = issue("Bad-3", secret=secret, now=now)
        u3 = encode(p3, other)          # 用错密钥签
        for _ in range(3):
            try:
                redeem(u3, led, secret=secret, now=now)
            except EnrollError as e:
                assert e.reason == "bad_sig", e.reason
        assert redeem_count(led) == 2, redeem_count(led)
        print("  验签失败不进账本（重放刷号无效） OK")

        # 10) 账本坏档不许放行（fail-closed，否则「一次性」形同虚设）
        bad_led = os.path.join(tdir, "broken.json")
        open(bad_led, "w", encoding="utf-8").write("{不是JSON")
        try:
            redeem(uri, bad_led, secret=secret, now=now)
            raise AssertionError("账本坏了竟然放行")
        except EnrollError as e:
            assert e.reason == "ledger_corrupt", e.reason
        print("  账本损坏即拒（fail-closed） OK")

        # 11) PSK 凭据：绑定/过期/撤销
        psk = p["k"]
        register_psk(psk_led, psk, "PC-QR", exp=0, now=now)
        assert check_psk(psk_led, psk, "PC-QR", now=now + 86400 * 365), "长期凭据不该自己过期"
        assert not check_psk(psk_led, psk, "PC-B", now=now), "凭据不该跨节点通用"
        assert not check_psk(psk_led, "00" * 16, "PC-QR", now=now), "乱猜的凭据不该过"
        register_psk(psk_led, "aa" * 16, "Old-Box", exp=now + 60, now=now)
        assert check_psk(psk_led, "aa" * 16, "Old-Box", now=now + 61) is False, "带过期的凭据该失效"
        mark_used(psk_led, psk, now=now + 10)
        assert [r for r in list_psks(psk_led) if r["node"] == "PC-QR"][0]["used_at"] == now + 10
        assert revoke(psk_led, "PC-QR") == 1, "撤销应命中 1 条"
        assert not check_psk(psk_led, psk, "PC-QR", now=now + 11), "撤销后不该再能认证"
        print("  PSK：长期有效 / 不跨节点 / 可撤销 / 账本只存哈希 OK")

        # 12) 密钥文件首次生成并可复读（不落明文密码，仅本人可读）
        os.environ["FLEET_ENROLL_SECRET_FILE"] = os.path.join(tdir, "sub", "enroll_secret")
        try:
            a = load_secret()
            b = load_secret()
            assert a == b and len(a) >= 32, (a, b)
            assert os.path.exists(os.environ["FLEET_ENROLL_SECRET_FILE"])
            p4 = issue("Win-New", secret=a, now=now)
            verify(encode(p4, a), secret=a, now=now)
        finally:
            os.environ.pop("FLEET_ENROLL_SECRET_FILE", None)
        print("  共享密钥首次自动生成、复读一致、用它签的载荷可验 OK")

        print("[selftest] PASS")
    finally:
        for fn in os.listdir(tdir):
            fp = os.path.join(tdir, fn)
            if os.path.isfile(fp):
                os.remove(fp)
        if os.path.isdir(os.path.join(tdir, "sub")):
            for fn in os.listdir(os.path.join(tdir, "sub")):
                os.remove(os.path.join(tdir, "sub", fn))
            os.rmdir(os.path.join(tdir, "sub"))
        os.rmdir(tdir)


if __name__ == "__main__":
    import sys
    ap_argv = sys.argv[1:]
    if "--selftest" in ap_argv:
        _selftest()
    elif "--issue" in ap_argv:
        i = ap_argv.index("--issue")
        node = ap_argv[i + 1] if len(ap_argv) > i + 1 else "PC-New"
        host = ap_argv[ap_argv.index("--host") + 1] if "--host" in ap_argv else "127.0.0.1"
        port = int(ap_argv[ap_argv.index("--port") + 1]) if "--port" in ap_argv else 9000
        ttl = int(ap_argv[ap_argv.index("--ttl") + 1]) if "--ttl" in ap_argv else DEFAULT_TTL
        pay = issue(node, host=host, port=port, ttl=ttl)
        print(encode(pay))
    elif "--verify" in ap_argv:
        i = ap_argv.index("--verify")
        uri = ap_argv[i + 1] if len(ap_argv) > i + 1 else ""
        try:
            pay = verify(uri)
            print("验签通过:", describe(pay))
        except EnrollError as e:
            raise SystemExit(f"验签失败: {e}")
    else:
        print("用法: enroll.py --selftest | --issue <节点> [--host H] [--port P] [--ttl S] | --verify <uri>")
