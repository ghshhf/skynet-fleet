"""分权：主号 / 分号（accounts + 一次性授权码）。

设计的出发点（用户定调）：「账号系统本质上就是分权，默认只有我能操控其他」。
所以这里**不是**通用账号体系，只有两种角色，没有中间态：

- owner：唯一。能发令、能发码、能增删分号、能看全部设备。
- sub  ：只能被控。没有发令权，只能看到自己名下节点。

三条硬规矩（写在代码里，不靠调用方自觉）：
1. 权限判定必须是纯函数 `can(role, op)`，且在**控制面侧**调用——客户端过滤等于没过滤。
2. 令牌自带角色与过期时间，且绑定用户当前状态：用户一被吊销，已发出的令牌**立即**失效
   （不是等它自然过期）。为此令牌里带 `pv`（permission version），吊销时版本号 +1。
3. 密码只用于换令牌，不落盘；存的是 scrypt 派生（不可用则退 pbkdf2），每个用户独立盐。

授权码（用户说的「密码生成器或扫码同步权限」）：
同一枚一次性码的两种呈现——终端里显示 6 位短码，手机上画成二维码。
- 6 位码用 Crockford 风格字母表（去掉 0/O/1/I），32^6 ≈ 10 亿，配合 5 分钟寿命 + 一次性。
- 二维码载荷 `fleet://grant?p=<b64url(JSON)>.<HMAC>`，HMAC 用账本自带密钥签，
  所以**伪造载荷在兑换前就被拒**，不必等查账本。
- 与 `enroll` 的入网码刻意分开：入网码离线验签（设备自己判真假，因为装设备时可能没网），
  授权码必须中心验（改权限这种事不能交给客户端自己认）。

诚实边界：
- 没有找回密码。主号忘了密码 = 删 accounts.json 重建（会连带吊销所有分号），
  这是刻意的：找回通道本身就是后门。
- 没有并发写保护之外的审计；变更只记 `updated` 时间戳，详细审计走 journal（后续接）。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time

VERSION = 1
DEFAULT_TOKEN_TTL = 8 * 3600
DEFAULT_GRANT_TTL = 300
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_SCRYPT = dict(n=16384, r=8, p=1, dklen=32)


class AccountError(Exception):
    pass


# ---------------- 权限矩阵 ----------------

# 发令类动作（与 commander._gate 的拦截清单保持同一套名字）
ORDER_OPS = (
    "exec", "broadcast", "push", "pull", "compute", "computemap", "ui",
    "rules_put", "rules_rollback", "enroll", "enroll_revoke", "token",
    "intent", "cap_grant", "accounts", "discover",
)
# 分号唯一允许的动作：看自己名下设备（只读）
SUB_OPS = ("view_own",)


def can(role, op):
    """纯函数权限判定。未知角色一律拒绝（fail-closed）。"""
    if role == "owner":
        return True
    if role == "sub":
        return op in SUB_OPS
    return False


def deny_reason(role, op):
    if role == "sub" and op in ORDER_OPS:
        return f"分号无权发令（{op}），只有主号能操控其他设备"
    return f"未知角色或动作：role={role} op={op}"


# ---------------- 账本读写 ----------------

def _empty():
    return {"version": VERSION, "owner": None, "users": {}, "grants": {},
            "secret": base64.b64encode(secrets.token_bytes(32)).decode()}


def load(path):
    """读账本。文件不存在 -> 空账本；损坏 -> 抛 AccountError（调用方须 fail-closed）。"""
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
    except (OSError, ValueError) as e:
        raise AccountError(f"账号账本不可读：{e}")
    if not isinstance(db, dict) or "users" not in db:
        raise AccountError("账号账本格式不对")
    db.setdefault("owner", None)
    db.setdefault("grants", {})
    db.setdefault("secret", _empty()["secret"])
    return db


def save(path, db):
    """原子替换写（先写临时文件再 rename），避免半截账本把所有人锁在门外。"""
    db["version"] = VERSION
    db["updated"] = int(time.time())
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".accounts.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _hash(password, salt_b64):
    """scrypt 优先；老环境/无 OpenSSL 时退 pbkdf2，并把算法名写进记录。"""
    salt = base64.b64decode(salt_b64)
    pw = password.encode("utf-8")
    try:
        dk = hashlib.scrypt(pw, salt=salt, **_SCRYPT)
        return "scrypt", base64.b64encode(dk).decode()
    except (ValueError, OSError, AttributeError):
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, 200000)
        return "pbkdf2", base64.b64encode(dk).decode()


def _check(password, rec):
    algo, want = rec.get("algo"), rec.get("hash")
    if not algo or not want:
        return False
    got_algo, got = _hash(password, rec["salt"])
    # 算法可能因环境变化而降级，只比结果不比算法名
    return hmac.compare_digest(got.encode(), want.encode())


def create_owner(path, name, password):
    """建主号。已存在主号时拒绝（不做"覆盖"，覆盖=静默夺权）。"""
    db = load(path)
    if db.get("owner"):
        raise AccountError(f"主号已存在：{db['owner']}（要重建请先删除账本）")
    salt = base64.b64encode(secrets.token_bytes(16)).decode()
    algo, h = _hash(password, salt)
    db["owner"] = name
    db["users"][name] = {
        "role": "owner", "salt": salt, "algo": algo, "hash": h,
        "pv": 1, "created": int(time.time()), "revoked": None, "nodes": [],
    }
    save(path, db)
    return name


def add_sub(path, name, password, nodes=None):
    db = load(path)
    if name in db["users"] and not db["users"][name].get("revoked"):
        raise AccountError(f"用户已存在：{name}")
    salt = base64.b64encode(secrets.token_bytes(16)).decode()
    algo, h = _hash(password, salt)
    db["users"][name] = {
        "role": "sub", "salt": salt, "algo": algo, "hash": h, "pv": 1,
        "created": int(time.time()), "revoked": None,
        "nodes": list(nodes or []),
    }
    save(path, db)
    return name


def revoke(path, name):
    """吊销：不清记录，只标记 + 权限版本 +1（已发令牌立刻失效）。主号不能吊销自己。"""
    db = load(path)
    u = db["users"].get(name)
    if not u:
        raise AccountError(f"没有这个用户：{name}")
    if u.get("role") == "owner":
        raise AccountError("主号不能吊销自己（否则没人能发令了）")
    u["revoked"] = int(time.time())
    u["pv"] = int(u.get("pv", 1)) + 1
    save(path, db)
    return True


def bind_node(path, name, node):
    db = load(path)
    u = db["users"].get(name)
    if not u:
        raise AccountError(f"没有这个用户：{name}")
    if node not in u["nodes"]:
        u["nodes"].append(node)
    save(path, db)
    return u["nodes"]


def nodes_of(path, name):
    """分号能看见哪些节点——**控制面用它过滤**，不是客户端自己筛。"""
    db = load(path)
    u = db["users"].get(name)
    if not u:
        return []
    if u.get("role") == "owner":
        return None          # None = 全部可见
    return list(u.get("nodes") or [])


def verify(path, name, password):
    """密码换用户记录；失败返回 None（不回原因，避免用户名枚举）。"""
    try:
        db = load(path)
    except AccountError:
        return None
    u = db["users"].get(name)
    if not u or u.get("revoked"):
        return None
    if not _check(password, u):
        return None
    return {"name": name, "role": u.get("role"), "pv": u.get("pv", 1)}


def list_users(path):
    """脱敏列表：不带盐、不带哈希。账本坏了返回空列表而不是抛（列表页不该把面板打挂）。"""
    try:
        db = load(path)
    except AccountError:
        return []
    out = []
    for name, u in sorted(db["users"].items()):
        out.append({"name": name, "role": u.get("role"),
                    "nodes": list(u.get("nodes") or []),
                    "created": u.get("created"), "revoked": u.get("revoked")})
    return out


# ---------------- 令牌 ----------------

def _b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret_b64, msg):
    key = base64.b64decode(secret_b64)
    return _b64e(hmac.new(key, msg.encode(), hashlib.sha256).digest())


def issue_token(path, name, ttl=DEFAULT_TOKEN_TTL, now=None):
    """令牌 = `u.<payload>.<sig>`。带 pv，所以吊销后旧令牌自动作废。"""
    now = time.time() if now is None else now
    db = load(path)
    u = db["users"].get(name)
    if not u or u.get("revoked"):
        raise AccountError(f"用户不可用：{name}")
    payload = {"u": name, "r": u.get("role"), "pv": u.get("pv", 1),
               "exp": int(now) + int(ttl), "jti": secrets.token_hex(4)}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"u.{body}.{_sign(db['secret'], body)}"


def verify_token(path, token, now=None):
    """校验令牌。任何一步不对都返回 None（fail-closed）。"""
    now = time.time() if now is None else now
    if not isinstance(token, str) or token.count(".") != 2 or not token.startswith("u."):
        return None
    _, body, sig = token.split(".")
    try:
        db = load(path)
    except AccountError:
        return None
    if not hmac.compare_digest(sig, _sign(db["secret"], body)):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) <= int(now):
        return None
    u = db["users"].get(payload.get("u"))
    if not u or u.get("revoked"):
        return None
    # 关键：权限版本对不上 = 吊销过，旧令牌即使没过期也作废
    if int(u.get("pv", 1)) != int(payload.get("pv", 0)):
        return None
    return {"name": payload["u"], "role": u.get("role"), "exp": payload["exp"]}


# ---------------- 一次性授权码（短码 + 二维码同一枚） ----------------

def _gen_code(taken):
    while True:
        c = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if c not in taken:
            return c


def make_grant(path, target, kind="migrate", ttl=DEFAULT_GRANT_TTL, now=None):
    """主号生成一枚授权码。

    kind：
      migrate  —— 把某个节点交出去/迁到另一台设备（迁移权限）
      login    —— 允许该分号在另一台设备上换令牌（扫码同步权限）
    返回 {code, uri, kind, target, exp}：code 供终端里念，uri 供画二维码。
    """
    now = time.time() if now is None else now
    db = load(path)
    if target not in db["users"]:
        raise AccountError(f"没有这个用户：{target}")
    uid = db["users"][target]
    if uid.get("revoked"):
        raise AccountError(f"用户已吊销：{target}")
    # 顺手清过期码，避免账本长霉
    db["grants"] = {k: v for k, v in db["grants"].items()
                    if int(v.get("exp", 0)) > int(now) and not v.get("used")}
    code = _gen_code(db["grants"])
    exp = int(now) + int(ttl)
    db["grants"][code] = {"target": target, "kind": kind, "exp": exp,
                          "used": False, "at": int(now)}
    body = _b64e(json.dumps({"c": code, "t": target, "k": kind, "e": exp},
                            separators=(",", ":")).encode())
    save(path, db)
    return {"code": code, "kind": kind, "target": target, "exp": exp,
            "uri": f"fleet://grant?p={body}.{_sign(db['secret'], body)}"}


def redeem_grant(path, code_or_uri, now=None):
    """兑换。接受 6 位短码或完整 uri（扫码来的）。

    顺序刻意是「先验签、再查账本、最后消耗」：伪造的 uri 连账本都不用读就被拒。
    """
    now = time.time() if now is None else now
    raw = (code_or_uri or "").strip()
    if raw.startswith("fleet://grant?p="):
        body_sig = raw[len("fleet://grant?p="):]
        if body_sig.count(".") != 1:
            raise AccountError("授权码载荷格式不对")
        body, sig = body_sig.split(".")
        try:
            db0 = load(path)
        except AccountError:
            raise AccountError("账号账本不可读，拒绝兑换")
        if not hmac.compare_digest(sig, _sign(db0["secret"], body)):
            raise AccountError("授权码签名不对（不是本账本发出的）")
        try:
            raw = json.loads(_b64d(body)).get("c", "")
        except (ValueError, TypeError):
            raise AccountError("授权码载荷不可解析")
    raw = raw.upper().replace("-", "").strip()
    try:
        db = load(path)
    except AccountError:
        raise AccountError("账号账本不可读，拒绝兑换")
    g = db["grants"].get(raw)
    if not g:
        raise AccountError("授权码无效或已使用")
    if g.get("used"):
        raise AccountError("授权码已使用（一次性）")
    if int(g.get("exp", 0)) <= int(now):
        raise AccountError("授权码已过期")
    u = db["users"].get(g["target"])
    if not u or u.get("revoked"):
        raise AccountError("目标用户不可用")
    g["used"] = True
    g["used_at"] = int(now)
    save(path, db)
    return {"target": g["target"], "kind": g.get("kind"), "role": u.get("role")}


def grant_of(path, code):
    """只读查询（供控制面回执里带上 kind，不消耗）。"""
    try:
        db = load(path)
    except AccountError:
        return None
    g = db["grants"].get((code or "").strip().upper())
    if not g or g.get("used"):
        return None
    return {"target": g.get("target"), "kind": g.get("kind"), "exp": g.get("exp")}


# ---------------- 自测 ----------------

def _selftest():
    d = tempfile.mkdtemp(prefix="fleet_acc_")
    p = os.path.join(d, "accounts.json")

    # ① 建主号 + 密码校验
    create_owner(p, "kou", "主号密码")
    assert verify(p, "kou", "主号密码")["role"] == "owner"
    assert verify(p, "kou", "错密码") is None
    assert verify(p, "不存在", "任意") is None
    print("[selftest] 主号创建/密码校验 OK")

    # ② 主号唯一：重复建被拒（防止静默夺权）
    try:
        create_owner(p, "hacker", "x")
        raise AssertionError("重复建主号应当被拒")
    except AccountError:
        pass
    print("[selftest] 主号唯一 OK")

    # ③ 分号 + 角色
    add_sub(p, "tv", "电视密码", nodes=["TV"])
    assert verify(p, "tv", "电视密码")["role"] == "sub"
    print("[selftest] 分号创建 OK")

    # ④ 权限矩阵：分号不能发令，能看自己
    assert can("owner", "exec") and can("owner", "accounts")
    assert not can("sub", "exec")
    assert not can("sub", "broadcast")
    assert not can("sub", "enroll")
    assert can("sub", "view_own")
    assert not can("nobody", "view_own")        # 未知角色 fail-closed
    assert "只有主号" in deny_reason("sub", "exec")
    print("[selftest] 权限矩阵（分号只能被控）OK")

    # ⑤ 分号可见范围由控制面过滤
    assert nodes_of(p, "tv") == ["TV"]
    assert nodes_of(p, "kou") is None           # None = 全部
    print("[selftest] 节点可见范围 OK")

    # ⑥ 令牌：签发 / 校验 / 篡改 / 过期
    tk = issue_token(p, "kou")
    assert verify_token(p, tk)["role"] == "owner"
    assert verify_token(p, tk[:-2] + "zz") is None          # 篡改签名
    assert verify_token(p, "u.abc.def") is None             # 形状不对
    short = issue_token(p, "kou", ttl=10, now=time.time() - 100)
    assert verify_token(p, short) is None                   # 已过期
    print("[selftest] 令牌签发/校验/篡改/过期 OK")

    # ⑦ 吊销后旧令牌立即失效（pv 机制）
    sub_tk = issue_token(p, "tv")
    assert verify_token(p, sub_tk)["role"] == "sub"
    revoke(p, "tv")
    assert verify_token(p, sub_tk) is None, "吊销后旧令牌必须立刻失效"
    assert verify(p, "tv", "电视密码") is None
    try:
        issue_token(p, "tv")
        raise AssertionError("已吊销用户不应能换令牌")
    except AccountError:
        pass
    try:
        revoke(p, "kou")
        raise AssertionError("主号不应能吊销自己")
    except AccountError:
        pass
    print("[selftest] 吊销即失效 + 主号不可自吊销 OK")

    # ⑧ 授权码：短码 / 二维码同一枚，一次性，过期，伪造
    add_sub(p, "pad", "平板密码")
    g = make_grant(p, "pad", kind="login")
    assert len(g["code"]) == 6 and g["code"].isalnum()
    assert g["uri"].startswith("fleet://grant?p=")
    r = redeem_grant(p, g["code"])
    assert r["target"] == "pad" and r["role"] == "sub"
    try:
        redeem_grant(p, g["code"])
        raise AssertionError("一次性码不能兑换两次")
    except AccountError:
        pass
    print("[selftest] 短码兑换 + 一次性 OK")

    g2 = make_grant(p, "pad", kind="migrate")
    r2 = redeem_grant(p, g2["uri"])             # 扫码路径
    assert r2["kind"] == "migrate"
    print("[selftest] 二维码载荷兑换 OK")

    g3 = make_grant(p, "pad", ttl=10, now=time.time() - 100)
    try:
        redeem_grant(p, g3["code"])
        raise AssertionError("过期码应当被拒")
    except AccountError as e:
        assert "过期" in str(e)
    fake = "fleet://grant?p=eyJjIjoiQUJDREVGIiwidCI6InBhZCIsImsiOiJsb2dpbiIsImUiOjk5OTk5OTk5OTl9.AAAA"
    try:
        redeem_grant(p, fake)
        raise AssertionError("伪造签名应当被拒")
    except AccountError as e:
        assert "签名" in str(e)
    print("[selftest] 过期码/伪造签名 OK")

    g4 = make_grant(p, "pad", kind="login")
    assert grant_of(p, g4["code"])["kind"] == "login"   # 只读查询不消耗
    assert grant_of(p, g4["code"]) is not None
    print("[selftest] 只读查询不消耗 OK")

    # ⑨ 坏账本 fail-closed（不崩、不放行）
    bad = os.path.join(d, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{这不是 json")
    assert verify(bad, "kou", "主号密码") is None
    assert verify_token(bad, issue_token(p, "kou")) is None
    assert list_users(bad) == []
    try:
        redeem_grant(bad, "ABCDEF")
        raise AssertionError("坏账本不应能兑换")
    except AccountError:
        pass
    print("[selftest] 坏账本 fail-closed OK")

    # ⑩ 脱敏列表不带盐和哈希
    for u in list_users(p):
        assert "hash" not in u and "salt" not in u
    print("[selftest] 列表脱敏 OK")
    print("[selftest] PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
