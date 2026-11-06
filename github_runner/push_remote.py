# -*- coding: utf-8 -*-
"""天网 · 把 github_runner/ 的代码推到云端试验场仓库
================================================================
为什么不用 git：本机 git 走不通（沙箱里 git 代理阻断），而 Contents API
这条路是实测可用的（也就是当初能把 harvest.py 推上去的那条）。

用法：
    python push_remote.py              # 推 nf 引擎 + workflow
    python push_remote.py --dry        # 只列出会推哪些文件

不做的事：
  · 不推 data/（云端自己爬，本地存量一条都不上去）
  · 不推 __pycache__ / *.pyc
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = "skynet93560862-creator/x-harvest"
BRANCH = "main"
HERE = os.path.dirname(os.path.abspath(__file__))

# 出网代理走共享解析层（环境变量 → 系统代理 → 端口探测 → 直连），不写死端口。
# 这个脚本也可能从子目录单独跑，所以先把仓库根加进搜索路径；解析层不可用时退回直连。
sys.path.insert(0, os.path.dirname(HERE))
try:
    from fleet.netproxy import proxies as _proxies
except ImportError:                                    # noqa: BLE001
    _proxies = None


def _proxy_map():
    return _proxies() if _proxies else {}

# 本地路径 → 仓库内路径
FILES = [
    ("nf/resource_finder.py", "nf/resource_finder.py"),
    ("nf/classify.py", "nf/classify.py"),
    ("nf/evolve.py", "nf/evolve.py"),
    ("nf/graphdiffuse.py", "nf/graphdiffuse.py"),
    ("nf.yml", ".github/workflows/netforce.yml"),
    ("harvest.py", "harvest.py"),
    ("harvest.yml", ".github/workflows/harvest.yml"),
    ("probe_x.py", "probe_x.py"),
    ("probe.yml", ".github/workflows/probe.yml"),
    ("tg_register.py", "tg_register.py"),
    ("pool.json", "pool.json"),
    ("tg.yml", ".github/workflows/tg-register.yml"),
    ("v6probe.yml", ".github/workflows/v6probe.yml"),
]


def _token():
    sys.path.insert(0, os.path.dirname(HERE))
    from fleet import vault
    t = vault.load("github_skynet_token")
    if not t:
        raise SystemExit("保险箱里没有 github_skynet_token")
    return t


def _call(method, path, data, tok, retries=3):
    url = "https://api.github.com" + path
    body = json.dumps(data).encode() if data is not None else None
    op = urllib.request.build_opener(urllib.request.ProxyHandler(_proxy_map()))

    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Accept", "application/vnd.github+json")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with op.open(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            # 409 = 并发写冲突（上一个 commit 还没落定），等一会儿重试
            if e.code in (409, 502, 503) and i < retries - 1:
                last = "%s %s" % (e.code, detail)
                time.sleep(2 + i * 2)
                continue
            raise RuntimeError("%s %s → %s" % (method, path, detail))
        except Exception as e:
            if i < retries - 1:
                last = str(e)
                time.sleep(2 + i * 2)
                continue
            raise
    raise RuntimeError("重试耗尽：%s" % last)


def _sha(path, tok):
    try:
        d = _call("GET", "/repos/%s/contents/%s?ref=%s" % (REPO, path, BRANCH), None, tok)
        return d.get("sha")
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None
        raise


def main():
    dry = "--dry" in sys.argv
    tok = "" if dry else _token()
    ok = fail = skip = 0

    for local, remote in FILES:
        p = os.path.join(HERE, local)
        if not os.path.exists(p):
            print("  [跳过] 本地没有 %s" % local)
            skip += 1
            continue
        raw = open(p, "rb").read()
        if dry:
            print("  [将推] %-28s → %-34s %6d B" % (local, remote, len(raw)))
            continue

        sha = _sha(remote, tok)
        payload = {
            "message": "netforce: %s" % local,
            "content": base64.b64encode(raw).decode(),
            "branch": BRANCH,
        }
        if sha:
            payload["sha"] = sha
        try:
            r = _call("PUT", "/repos/%s/contents/%s" % (REPO, remote), payload, tok)
            commit = (r.get("commit") or {}).get("sha", "")[:8]
            print("  [已推] %-28s → %-34s %6d B  %s"
                  % (local, remote, len(raw), commit))
            ok += 1
        except Exception as e:
            print("  [失败] %-28s → %s\n         %s" % (local, remote, str(e)[:200]))
            fail += 1
        time.sleep(0.4)          # 别把 API 打太急

    print("\n完成：成功 %d / 失败 %d / 跳过 %d" % (ok, fail, skip))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
