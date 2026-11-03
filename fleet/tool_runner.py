# -*- coding: utf-8 -*-
"""天网 · 渗透工具调用层（工具优先，自研兜底）
====================================================
2026-11-02：先把现成开源工具（ProjectDiscovery 工具链 + ffuf）下载下来用，补齐
自研引擎的短板（nuclei 有几千模板 vs 自研规则引擎十几条）。工具统一落
`G:/DevTools/pentest/bin/*.exe`，模板库在 `G:/DevTools/pentest/nuclei-templates/`。

设计原则：
  · 工具优先：工具装好了就 subprocess 调工具（无窗启动、不弹窗）。
  · 自研兜底：工具不在场时回退 recon_engine（纯 Python，恒在线）。
  · 诚实 check：有工具就报「工具在线」，只有自研就报「自研兜底」，都没有才离线。

授权边界：仅对「已授权范围」目标使用；本层是侦察/测绘，不内置主动利用。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PENTEST_BIN = Path("G:/DevTools/pentest/bin")
NUCLEI_TEMPLATES = Path("G:/DevTools/pentest/nuclei-templates")

# 工具名 -> 能力名（用于 check 报告）
TOOLS = {
    "subfinder": "pentest.subdomain_enum",
    "dnsx": "pentest.dns_resolve",
    "httpx": "pentest.live_probe",
    "naabu": "pentest.port_scan",
    "nuclei": "pentest.vuln_scan",
    "ffuf": "pentest.content_discovery",
}

# 工具联网需走代理的开关（subfinder 很多源被墙；直连快的走直连，按需开）
PROXY = os.environ.get("PENTEST_PROXY", "")  # 形如 http://127.0.0.1:7890


def _hidden_startupinfo():
    """无窗启动三件套：SW_HIDE + CREATE_NO_WINDOW，照搬 netforce 范式，后台不弹窗。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def tool_path(name: str) -> str | None:
    """返回工具 exe 的绝对路径；未安装返回 None。"""
    p = PENTEST_BIN / f"{name}.exe"
    return str(p) if p.exists() else None


def has_tool(name: str) -> bool:
    return tool_path(name) is not None


def installed() -> dict[str, bool]:
    """全部工具安装状态。"""
    return {t: has_tool(t) for t in TOOLS}


def run_tool(name: str, args: list[str], timeout: int = 180,
             proxy: bool = False) -> dict:
    """无窗跑一个工具，返回 {ok, returncode, stdout, stderr, err}。"""
    exe = tool_path(name)
    if not exe:
        return {"ok": False, "err": f"{name} 未安装（缺 {PENTEST_BIN}/{name}.exe）"}
    full = [exe] + list(args)
    env = dict(os.environ)
    if proxy and PROXY:
        # 各工具的代理参数由调用方显式传入 args，这里只在需要时兜底 http_proxy
        env.setdefault("HTTPS_PROXY", PROXY)
        env.setdefault("HTTP_PROXY", PROXY)
    try:
        p = subprocess.run(
            full, capture_output=True, text=True, timeout=timeout,
            startupinfo=_hidden_startupinfo(), env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "stdout": p.stdout or "", "stderr": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"{name} 超时（>{timeout}s）", "stdout": "", "stderr": ""}
    except Exception as e:
        return {"ok": False, "err": str(e), "stdout": "", "stderr": ""}


# ---------------------------------------------------------------- 工具封装

def subfinder_enum(domain: str, limit: int = 0) -> dict:
    """subfinder 被动子域枚举。"""
    args = ["-d", domain, "-silent"]
    if limit:
        args += ["-max-time", "120"]
    r = run_tool("subfinder", args, timeout=240, proxy=True)
    subs = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    subs = list(dict.fromkeys(subs))
    if limit:
        subs = subs[:limit]
    r["subdomains"] = subs
    r["count"] = len(subs)
    return r


def dnsx_resolve(domain: str) -> dict:
    """dnsx 解析 A/AAAA/CNAME/MX/NS/TXT。"""
    r = run_tool("dnsx", ["-d", domain, "-a", "-aaaa", "-cname", "-mx", "-ns", "-txt",
                          "-resp", "-silent"], timeout=120, proxy=True)
    r["records"] = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    return r


def httpx_probe(targets: list[str]) -> dict:
    """httpx 存活探测 + 状态码/标题/技术栈。"""
    # -l 读文件；目标少时用 -u 逐个。这里用 stdin 喂列表。
    payload = "\n".join(targets)
    r = run_tool("httpx", ["-silent", "-status-code", "-title", "-tech-detect",
                           "-follow-redirects"], timeout=240, proxy=True)
    # httpx 无 stdin 参数时，把目标写临时文件
    if not targets:
        return {"ok": True, "alive": []}
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(payload)
        tmp = f.name
    try:
        r = run_tool("httpx", ["-l", tmp, "-silent", "-status-code", "-title",
                               "-tech-detect", "-follow-redirects"],
                     timeout=240, proxy=True)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    r["lines"] = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    return r


def naabu_scan(host: str, ports: str = "") -> dict:
    """naabu 端口扫描。"""
    args = ["-host", host, "-silent"]
    if ports:
        args += ["-p", ports]
    else:
        args += ["-top-ports", "1000"]
    r = run_tool("naabu", args, timeout=300)
    r["ports"] = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    return r


def nuclei_scan(target: str, tags: str = "") -> dict:
    """nuclei 漏洞扫描（需要模板库）。"""
    if not NUCLEI_TEMPLATES.exists():
        return {"ok": False, "err": f"模板库缺失：{NUCLEI_TEMPLATES}"}
    args = ["-u", target, "-silent", "-nc",
            "-t", str(NUCLEI_TEMPLATES)]
    if tags:
        args += ["-tags", tags]
    r = run_tool("nuclei", args, timeout=600, proxy=False)
    r["findings"] = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    return r


def ffuf_fuzz(url: str, wordlist: str, match_codes: str = "",
              threads: int = 40) -> dict:
    """ffuf 目录 fuzz。wordlist 必须是本地字典文件路径。"""
    args = ["-u", url, "-w", wordlist, "-t", str(threads), "-mc",
            match_codes or "200,204,301,302,307,401,403,405,500"]
    r = run_tool("ffuf", args, timeout=300)
    r["lines"] = [ln.strip() for ln in r.get("stdout", "").splitlines() if ln.strip()]
    return r


# ---------------------------------------------------------------- 离线自测

def _selftest() -> int:
    ok = fail = 0

    def check(cond, name):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"    [失败] {name}")

    check(isinstance(PENTEST_BIN, Path), "PENTEST_BIN 路径对象")
    st = installed()
    print(f"    工具安装状态：{st}")
    check(sum(st.values()) >= 1, "至少一个工具已安装")

    # 工具版本探测（有就测，无就跳过）
    for name in TOOLS:
        p = tool_path(name)
        if not p:
            continue
        r = run_tool(name, ["-version"], timeout=30)
        # 有的工具用 -version，有的打印 banner 但 returncode 非 0，也算「能跑」
        ran = r.get("ok") or bool(r.get("stdout")) or bool(r.get("stderr"))
        check(ran, f"{name} 能启动（{p}）")

    print(f"[tool_runner] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
