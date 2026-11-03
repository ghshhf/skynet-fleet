"""免费接码站池：测活 + 号码提取 + 短信读取。

用途：给 Telegram 等平台注册时取一个公开号码收验证码。
判据：号码页面近期有短信 = 该号通道还通（否则就是死号）。

不做自动注册，只提供「取号 / 读码」两个动作，注册动作由上层显式执行。
"""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 站点清单：home 首页，号码页多为 /read-sms-<num>.html 或 /<num> 形式，由各站 adapter 处理
SITES = [
    "https://receive-smss.com",
    "https://sms24.me",
    "https://receive-sms-free.cc",
    "https://receivesms.co",
    "https://quackr.io",
    "https://freephonenum.com",
    "https://receive-sms.cc",
    "https://temp-numbers.net",
    "https://smstome.com",
    "https://smsreceivefree.com",
    "https://temporary-phone-number.com",
    "https://temp-number.com",
    "https://receivesms.org",
    "https://oksms.org",
    "https://jiemahao.com",
    "https://7sim.net",
    "https://mobilesms.io",
    "https://getfreesmsnumber.com",
    "https://sms-man.com/free-numbers",
    "https://hs3x.com",
]

PHONE_RE = re.compile(r"(?<!\d)(?:\+?1)?[\s\-.]?\(?(\d{10,11})\)?(?!\d)")
# 页面里能表示「有短信」的信号
SMS_HINTS = ("verification", "code", "验证码", "telegram", "whatsapp", "google",
             "instagram", "facebook", "tiktok", "discord", "twitter")


@dataclass
class SiteReport:
    site: str
    status: int = 0
    ms: int = 0
    numbers: list[str] = field(default_factory=list)
    sms_hits: int = 0
    err: str = ""

    def ok(self) -> bool:
        return self.status == 200 and bool(self.numbers)


def _get(url: str, timeout: int = 15):
    t0 = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                         timeout=timeout)
        return r, int((time.time() - t0) * 1000), ""
    except Exception as e:
        return None, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"[:120]


def extract_numbers(html: str) -> list[str]:
    """从页面里挖出候选号码：优先 11 位（含美国 +1），去重保序。"""
    out, seen = [], set()
    for m in PHONE_RE.finditer(html):
        n = re.sub(r"\D", "", m.group(0))
        if len(n) == 11 and n.startswith("1"):
            n = n[1:]
        if len(n) != 10:
            continue
        # 排除明显不是号码的（连续重复、年份串）
        if n in seen or len(set(n)) <= 3:
            continue
        seen.add(n)
        out.append(n)
    return out


def probe(site: str) -> SiteReport:
    rep = SiteReport(site=site)
    r, ms, err = _get(site)
    rep.ms = ms
    if r is None:
        rep.err = err
        return rep
    rep.status = r.status_code
    if r.status_code != 200:
        return rep
    html = r.text
    rep.numbers = extract_numbers(html)[:12]
    low = html.lower()
    rep.sms_hits = sum(1 for h in SMS_HINTS if h in low)
    return rep


def probe_all(sites=None, workers: int = 8) -> list[SiteReport]:
    sites = sites or SITES
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(probe, sites))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    reps = probe_all()
    live = [r for r in reps if r.ok()]
    print(f"{'站点':<34}{'HTTP':>5}{'耗时':>7}{'号码数':>7}{'信号':>5}")
    print("-" * 66)
    for r in sorted(reps, key=lambda x: (-len(x.numbers), x.site)):
        host = urlparse(r.site).netloc
        num = str(len(r.numbers)) if r.ok() else ("—" if r.status else "ERR")
        print(f"{host:<34}{r.status or '-':>5}{r.ms:>6}ms{num:>7}{r.sms_hits:>5}")
        if r.err:
            print(f"    ! {r.err}")
    print(f"\n可用: {len(live)}/{len(reps)}")
    for r in live:
        print(f"  {urlparse(r.site).netloc:<28} {r.numbers[:4]}")


if __name__ == "__main__":
    main()
