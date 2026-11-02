"""T_自建发现后端（selfindex）：离线门禁。

全离线：monkeypatch 掉网络层，喂罐头 JSON 验三类解析（CT 日志 / Common Crawl /
互联网档案馆 CDX），并验「网络异常一律降级返回 []」。不碰真实网络。

跑法：python evals/t_selfindex.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github_runner", "nf"))

import skynet.selfindex as SI


def _boom(*a, **k):
    raise RuntimeError("boom: 网络不可用")


def test_ct_parse():
    crt = json.dumps([
        {"name_value": "*.a.com\nB.COM\nb.com"},
        {"name_value": "c.org"},
    ])
    SI._get = lambda *a, **k: crt
    doms = SI.ct_subdomains("example.com")
    assert "a.com" in doms, doms
    assert "b.com" in doms, doms          # 小写 + 去重
    assert "c.org" in doms, doms
    assert "*.a.com" not in doms, "前缀 *. 未剥离"
    assert len(doms) == 3, doms           # 去重后只剩 3 个
    print("  ✓ ct_subdomains 解析（去 *. / 小写 / 去重）:", doms)


def test_cc_parse():
    cc = '{"url":"https://x.example.com/p"}\n{"url":"http://y.test.org/q"}\n'
    SI._get = lambda *a, **k: cc
    doms = SI.ct_keyword("dataset")
    assert "x.example.com" in doms, doms
    assert "y.test.org" in doms, doms
    print("  ✓ ct_keyword(CommonCrawl) 解析:", doms)


def test_wayback_parse():
    cdx = json.dumps([
        ["urlkey", "original", "mimetype"],
        ["k", "https://old.site.org/p", "text/html"],
        ["k2", "https://new.site.org/q", "text/html"],
    ])
    SI._get = lambda *a, **k: cdx
    doms = SI.archive_hosts("site.org")
    assert doms == ["new.site.org", "old.site.org"], doms
    print("  ✓ archive_hosts(Wayback CDX 带表头) 解析:", doms)


def test_degrade():
    SI._get = _boom
    assert SI.ct_subdomains("x") == []
    assert SI.ct_keyword("x") == []
    assert SI.archive_hosts("x") == []
    print("  ✓ 网络异常 → 全部降级返回 []")


def main():
    test_ct_parse()
    test_cc_parse()
    test_wayback_parse()
    test_degrade()
    print("t_selfindex：4 组断言全过（CT / CC / Wayback 解析 + 降级）")


if __name__ == "__main__":
    main()
