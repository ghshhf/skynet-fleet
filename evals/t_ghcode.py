"""T_GitHub 代码挖域名后端（ghcode）：离线门禁。

全离线：monkeypatch 掉 _get（GitHub 代码搜索 API），喂罐头 JSON 验
从 text_matches[].fragment 里抽域名，并验「无 token 也能跑 / 异常降级返回 []」。

跑法：python evals/t_ghcode.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github_runner", "nf"))

import skynet.ghcode as GH


def _boom(*a, **k):
    raise RuntimeError("boom: 网络不可用")


def test_parse():
    body = json.dumps({
        "items": [
            {"text_matches": [
                {"fragment": "see https://foo.duckdns.org and http://bar.pages.dev/x"},
            ]},
            {"text_matches": [
                {"fragment": "see https://baz.onrender.com:8080 与 https://qux.workers.dev"},
            ]},
        ],
    })
    GH._get = lambda *a, **k: body
    doms = GH.gh_code_domains("duckdns.org")
    for want in ("foo.duckdns.org", "bar.pages.dev", "baz.onrender.com", "qux.workers.dev"):
        assert want in doms, (want, doms)
    print("  ✓ gh_code_domains 解析（多 fragment / 多端口 / 多后缀）:", doms)


def test_no_token_ok():
    # _token 读环境变量；测试环境无 token 也应能跑（限流更严，但解析逻辑一致）
    saved = {k: os.environ.pop(k, "") for k in ("NF_PAT", "GITHUB_TOKEN", "GH_TOKEN", "OFM_KEY")}
    try:
        body = json.dumps({"items": [{"text_matches": [{"fragment": "https://no.token.here.dev"}]}]})
        GH._get = lambda *a, **k: body
        doms = GH.gh_code_server_names()
        assert "no.token.here.dev" in doms, doms
        print("  ✓ 无 token 仍能解析:", doms)
    finally:
        os.environ.update(saved)


def test_degrade():
    GH._get = _boom
    assert GH.gh_code_domains("x") == []
    assert GH.gh_code_server_names() == []
    print("  ✓ 网络异常 → 全部降级返回 []")


def main():
    test_parse()
    test_no_token_ok()
    test_degrade()
    print("t_ghcode：3 组断言全过（fragment 抽域名 / 无 token / 降级）")


if __name__ == "__main__":
    main()
