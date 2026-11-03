#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tg_register.py —— Telegram 免费接码号端到端注册器（两阶段并行版）

================================ 为什么是两阶段 ================================
串行版：发码 → 等 180s → 注册 → 下一个号      ⇒ 最坏 32×190s ≈ 100 分钟
并行版：全量发码（只记录，不等）→ 可注册号**同时**等码（180s 重叠）→ 串行注册
                                              ⇒ 最坏 32×8s + 180s + 少量注册 ≈ 8 分钟

================================ 号码判据（2026-11-02 实测确立） ================================
  SentCodeTypeSms        → 号未注册（TG 没别的设备可推，只能发短信）  → 可注册
  SentCodeTypeApp        → 号已注册（推给已有设备 / 另一台手机）      → 废号
  SentCodeTypeCall / FlashCall / MissedCall → 仅语音验证，接码站接不到 → 跳过
  PhoneNumberBanned      → 被封禁                                   → 废号
  PhoneNumberInvalid     → 号码格式无效                              → 废号

================================ 安全边界 ================================
· 绝不使用用户自己账号的 ~/.tg-mcp/user.session
· 只用 StringSession 匿名 connect()，全程不 sign_in 任何既有会话
· 只对"号码"发 auth.sendCode / auth.signUp，不碰任何群、频道、真人

输出：
  ~/.tg-mcp/accounts/registered.json   已注册账号清单（含 StringSession）
  ~/.tg-mcp/accounts/probe.json        本轮探测结果
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import ssl
import time
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".tg-mcp" / "config.json"
OUT_DIR = Path(os.environ.get("TG_OUT") or (Path.home() / ".tg-mcp" / "accounts"))
PROXY_HOST, PROXY_PORT = "127.0.0.1", 7890
HTTP_PROXY = f"http://{PROXY_HOST}:{PROXY_PORT}"
SITE = "https://receive-sms.cc"

# 云端（GitHub Actions / Azure）直连，不需要代理；本机必须走 7890。
# 由环境变量 TG_NOPROXY=1 切换，避免把本机代理硬编码带进云端。
NO_PROXY = os.environ.get("TG_NOPROXY") == "1"
if NO_PROXY:
    HTTP_PROXY = ""
API_ID_ENV = os.environ.get("TG_API_ID", "")
API_HASH_ENV = os.environ.get("TG_API_HASH", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_2FA = "Nav1gator#TB2026"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg() -> dict:
    """本机读 ~/.tg-mcp/config.json；云端走环境变量（仓库里不放任何凭据）。"""
    if CONFIG.exists():
        with CONFIG.open(encoding="utf-8") as f:
            return json.load(f)
    if API_ID_ENV and API_HASH_ENV:
        return {"api_id": API_ID_ENV, "api_hash": API_HASH_ENV}
    raise SystemExit("缺少 api_id/api_hash：既无 config.json，也无 TG_API_ID/TG_API_HASH")


# ---------------------------------------------------------------- 接码页
_OPENER = (urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))
           if NO_PROXY else
           urllib.request.build_opener(
               urllib.request.ProxyHandler({"http": HTTP_PROXY, "https": HTTP_PROXY}),
               urllib.request.HTTPSHandler(context=CTX)))


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def page_url(country: str, number: str) -> str:
    return f"{SITE}/{country.replace(' ', '-')}-Phone-Number/{number}"


def html_to_text(h: str) -> str:
    h = re.sub(r"<script[\s\S]*?</script>", " ", h)
    h = re.sub(r"<style[\s\S]*?</style>", " ", h)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</(?:div|p|li|tr|h\d)>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        h = h.replace(a, b)
    return "\n".join(ln.strip() for ln in h.split("\n") if ln.strip())


CODE_PAT = re.compile(
    r"(?:Telegram|t\.me/login)[^\n]{0,80}?(?<![\d])(\d{5,6})(?![\d])"
    r"|(?<![\d])(\d{5,6})(?![\d])[^\n]{0,60}?(?:Telegram|t\.me/login)",
    re.I,
)


def snapshot(url: str) -> dict:
    """返回 {code: 上下文}；出错返回 {'__err__': ...}"""
    try:
        txt = html_to_text(fetch(url))
    except Exception as e:
        return {"__err__": f"{type(e).__name__}: {e}"}
    out = {}
    for m in CODE_PAT.finditer(txt):
        code = m.group(1) or m.group(2)
        out[code] = txt[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
    return out


def wait_code(url: str, before: dict, budget: int) -> str | None:
    t0 = time.time()
    while time.time() - t0 < budget:
        now = snapshot(url)
        if "__err__" not in now:
            fresh = [c for c in now if c not in before]
            if fresh:
                log(f"    收到新码 {fresh[0]}  ← {url.split('/')[-1]}")
                return fresh[0]
        time.sleep(5)
    return None


# ---------------------------------------------------------------- 主流程
async def run(args) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.auth import SendCodeRequest, SignUpRequest, SignInRequest
    from telethon.tl.types import CodeSettings
    from telethon.errors import (
        PhoneNumberBannedError, PhoneNumberInvalidError, PhoneNumberOccupiedError,
        SessionPasswordNeededError, FloodWaitError, AuthRestartError,
    )

    cfg = load_cfg()
    api_id, api_hash = int(cfg["api_id"]), cfg["api_hash"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reg_path, probe_path = OUT_DIR / "registered.json", OUT_DIR / "probe.json"

    registered = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else []
    done = {r["phone"] for r in registered}
    # 已下定论的号不再重复消耗配额；FloodWait/超时这类没结论的允许重试
    if probe_path.exists():
        try:
            for p in json.loads(probe_path.read_text(encoding="utf-8")):
                if p.get("verdict") in ("已注册", "封禁", "号码无效", "已注册且有 2FA"):
                    done.add(p.get("phone", ""))
        except Exception:
            pass

    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    targets = [t for t in pool if ("+" + t["n"]) not in done]
    if args.limit:
        targets = targets[:args.limit]
    log(f"待探测 {len(targets)} 个号（跳过已注册 {len(done)} 个）")

    def new_client():
        kw = {}
        if not NO_PROXY:
            kw["proxy"] = ("socks5", PROXY_HOST, PROXY_PORT)
        return TelegramClient(
            StringSession(), api_id, api_hash,
            connection_retries=3, timeout=25, request_retries=3, **kw
        )

    client = new_client()
    await client.connect()
    log(f"匿名连接 DC{client.session.dc_id} @ {client.session.server_address}")

    probes: list[dict] = []

    # ---------------- 阶段 A：全量发码（只记录，不等待） ----------------
    log("── 阶段 A：批量探测（不复位等待） ──")
    aborted = False
    for i, item in enumerate(targets, 1):
        country, number = item["c"], item["n"]
        phone = "+" + number
        # 号池里带了 url 就直接用（跨站号池 URL 规则不同）；否则按 receive-sms.cc 规则拼
        url = item.get("url") or page_url(country, number)
        rec = {"country": country, "phone": phone, "number": number,
               "url": url, "type": "", "verdict": "",
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 基线：先看页面已有几个疑似码，供阶段 B 做差集
        base = snapshot(url)
        rec["base"] = list(base.keys()) if "__err__" not in base else []
        try:
            sent = await client(SendCodeRequest(
                phone_number=phone, api_id=api_id, api_hash=api_hash,
                settings=CodeSettings(),
            ))
            rec["type"] = type(sent.type).__name__
            rec["hash"] = sent.phone_code_hash
            rec["verdict"] = {"SentCodeTypeSms": "可注册",
                              "SentCodeTypeApp": "已注册"}.get(
                rec["type"], f"非短信({rec['type']})")
            log(f"[{i}/{len(targets)}] {country} {phone} → {rec['type']} / {rec['verdict']}")
        except FloodWaitError as e:
            rec["verdict"] = f"FloodWait {e.seconds}s"
            log(f"[{i}/{len(targets)}] {phone} → FloodWait {e.seconds}s")
            probes.append(rec)
            probe_path.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
            if e.seconds > args.max_flood:
                log(f"FloodWait 超过 {args.max_flood}s，终止阶段 A")
                aborted = True
                break
            await asyncio.sleep(e.seconds + 3)
            continue
        except PhoneNumberBannedError:
            rec["verdict"] = "封禁"
            log(f"[{i}/{len(targets)}] {phone} → 封禁")
        except PhoneNumberInvalidError:
            rec["verdict"] = "号码无效"
            log(f"[{i}/{len(targets)}] {phone} → 号码无效")
        except Exception as e:
            rec["verdict"] = f"异常 {type(e).__name__}"
            rec["error"] = str(e)[:160]
            log(f"[{i}/{len(targets)}] {phone} → {type(e).__name__}: {str(e)[:70]}")
        probes.append(rec)
        probe_path.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
        await asyncio.sleep(random.uniform(args.gap_min, args.gap_max))

    cands = [p for p in probes if p["type"] == "SentCodeTypeSms"]
    log(f"── 阶段 A 结束：可注册 {len(cands)} 个 / 探测 {len(probes)} 个 ──")

    # ---------------- 阶段 B：并行等码（180s 重叠，不累加） ----------------
    if cands:
        log(f"── 阶段 B：{len(cands)} 个号并行等码，预算 {args.wait}s ──")
        sem = asyncio.Semaphore(args.concurrency)

        async def grab(rec):
            async with sem:
                base = {c: "" for c in rec.get("base", [])}
                code = await asyncio.to_thread(wait_code, rec["url"], base, args.wait)
                rec["code"] = code
                return rec, code

        got = await asyncio.gather(*[grab(r) for r in cands])
        probe_path.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---------------- 阶段 C：串行注册 ----------------
        log("── 阶段 C：注册 ──")
        for rec, code in got:
            if not code:
                rec["verdict"] = "超时未取到码"
                log(f"  {rec['phone']} → 未取到码，跳过")
                continue
            phone = rec["phone"]
            # 每个号用独立 client，避免 auth_key 互相顶掉
            await client.disconnect()
            client = new_client()
            await client.connect()
            try:
                ok = False
                try:
                    await client(SignUpRequest(
                        phone_number=phone, phone_code_hash=rec["hash"],
                        first_name=args.first_name, last_name="",
                    ))
                    ok = True
                    log(f"  {phone} signUp 成功")
                except PhoneNumberOccupiedError:
                    try:
                        await client(SignInRequest(
                            phone_number=phone, phone_code_hash=rec["hash"],
                            phone_code=code,
                        ))
                        ok = True
                        log(f"  {phone} signIn 成功")
                    except SessionPasswordNeededError:
                        rec["verdict"] = "已注册且有 2FA"
                        log(f"  {phone} 有两步验证，进不去")
                if not ok:
                    continue

                try:
                    await client.edit_2fa(new_password=args.password, hint="nav", email=None)
                    rec["twofa"] = True
                    log(f"  {phone} 2FA 已设（号已锁）")
                except Exception as e:
                    rec["twofa"] = False
                    log(f"  {phone} 2FA 失败：{type(e).__name__}")

                me = await client.get_me()
                rec.update({"verdict": "注册成功", "user_id": me.id,
                            "username": me.username,
                            "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
                            "session": client.session.save()})
                registered.append(rec)
                reg_path.write_text(json.dumps(registered, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                log(f"  ✅ {phone} 注册成功 uid={me.id} @{me.username}")
            except FloodWaitError as e:
                rec["verdict"] = f"注册 FloodWait {e.seconds}s"
                log(f"  {phone} FloodWait {e.seconds}s")
            except Exception as e:
                rec["verdict"] = f"注册失败 {type(e).__name__}"
                rec["error"] = str(e)[:160]
                log(f"  {phone} 失败 {type(e).__name__}: {str(e)[:90]}")
            probe_path.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        log("没有可注册的号，跳过阶段 B/C")

    try:
        await client.disconnect()
    except Exception:
        pass

    from collections import Counter
    log("=" * 62)
    log(f"汇总：{dict(Counter(p.get('verdict', '?') for p in probes))}")
    log(f"注册成功累计 {len(registered)} 个 → {reg_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram 免费号端到端注册（两阶段并行）")
    ap.add_argument("--pool", default=str(Path(__file__).resolve().parent.parent /
                                          "skynet-fleet/tmp/scan_precise.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wait", type=int, default=180, help="等码预算秒数（并行重叠）")
    ap.add_argument("--gap-min", type=float, default=6.0)
    ap.add_argument("--gap-max", type=float, default=10.0)
    ap.add_argument("--concurrency", type=int, default=4, help="并行抓码并发数")
    ap.add_argument("--max-flood", type=int, default=300)
    ap.add_argument("--first-name", default="Navigator")
    ap.add_argument("--password", default=DEFAULT_2FA)
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
