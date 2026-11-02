"""T_云厂商资产测绘后端（cloud）：离线门禁。

全离线：monkeypatch 掉 _get（URLScan 搜索 API），喂罐头 JSON 验从
results[].page.domain 抽域名；并验「无 key / 异常 → 降级返回 []」。

跑法：python evals/t_cloud.py
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github_runner", "nf"))

import skynet.cloud as CL

ORIG_KEY = CL._key          # 原函数：读环境变量 URLSCAN_KEY
ORIG_GET = CL._get          # 原网络函数


def _boom(*a, **k):
    raise RuntimeError("boom: 网络不可用")


def test_parse():
    body = json.dumps({
        "results": [
            {"page": {"domain": "a.example.com"}},
            {"page": {"domain": "b.test.org"}},
            {"page": {}},                     # 缺 domain 字段 → 跳过
        ],
    })
    CL._get = lambda *a, **k: body
    CL._key = lambda: "fake-key"
    doms = CL.cloud_asn_domains("Alibaba")
    assert "a.example.com" in doms, doms
    assert "b.test.org" in doms, doms
    assert len(doms) == 2, doms
    print("  ✓ cloud_asn_domains 解析（含缺字段跳过）:", doms)


def test_degrade_no_key():
    # 无 URLSCAN_KEY → _get 在发请求前抛 RuntimeError → 上层 catch 返回 []
    # （先还原 _key 为读环境变量的原函数，避免被 test_parse 的 monkeypatch 污染）
    saved = os.environ.pop("URLSCAN_KEY", "")
    CL._key = ORIG_KEY
    CL._get = ORIG_GET
    try:
        assert CL.cloud_asn_domains("X") == []
        assert CL.urlscan_server_domains("Y") == []
        print("  ✓ 无 key → 全部降级返回 []（不发真实请求）")
    finally:
        if saved:
            os.environ["URLSCAN_KEY"] = saved


def test_degrade_network():
    CL._key = lambda: "fake-key"
    CL._get = _boom
    assert CL.cloud_asn_domains("X") == []
    assert CL.urlscan_server_domains("Y") == []
    print("  ✓ 网络异常 → 全部降级返回 []")


def main():
    test_parse()
    test_degrade_no_key()
    test_degrade_network()
    print("t_cloud：3 组断言全过（URLScan 解析 / 无 key / 网络异常 降级）")


if __name__ == "__main__":
    main()
