#!/usr/bin/env python
"""门禁 t26：证据分级 authority——「读到的」和「猜的」必须是数据结构。

**这个门禁钉的是三件真事**（每条都对应一次真实误判或一次静默失败）：

  ① **同一结论必须同一 id。** 证据 id 对 (facet, claim, value, authority)
     取 sha256 前 16 位 ⇒ 跨调用/跨机器同一条事实指向同一 id，去重与引用
     才有锚点。id 抖动 ⇒ 去重失效，同一结论被当成两条独立证据。

  ② **每条 web.* 结果都必须带 authority，且取值合法。** 缺这个字段 ⇒
     agent 分不清哪句是库里的原文、哪句是它润色出来的（recall 的沉疴：
     只会说「不要编造」，但引用物本身没有分级）。取值非法 ⇒ 四级悄悄
     退化成一锅粥，比没有分级更坏 ⇒ mk_evidence 对非法值必须抛 ValueError。

  ③ **discovery 的端口投票必须标 analyst-inference。** 两个真实误判：
     开 554 的 Windows 被判摄像头（实际 mDNS 自报 iVCam）、5555 被判手机。
     根因是端口投票赢过设备自报，而 README 铁律「设备自报远比端口组合可靠」
     只活在注释里 ⇒ 这里把它钉成代码：自报出现即 shipped-artifact，
     纯端口投票必须 analyst-inference。

**全离线**：不连真网络，全部用假数据驱动判定助手。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(name, cond, detail=""):
    if cond:
        print("  ok", name)
    else:
        FAIL.append(f"{name}: {detail}")
        print("  FAIL", name, "-", detail)


def skip(name, why):
    SKIP.append(f"{name}: {why}")
    print("  --", name, "-", why)


def main():
    from fleet import authority as AUTH

    print("[1] 证据 id：同一结论同一 id，不同结论不同 id")
    a = AUTH.mk_evidence("web.search", "搜索引擎命中", "https://x.com", "historical-reference")
    b = AUTH.mk_evidence("web.search", "搜索引擎命中", "https://x.com", "historical-reference")
    c = AUTH.mk_evidence("web.search", "搜索引擎命中", "https://y.com", "historical-reference")
    check("同一结论两次生成 id 相同", a.id == b.id, f"{a.id} != {b.id}")
    check("id 是 sha256 前 16 位", len(a.id) == 16 and a.id.isalnum(), a.id)
    check("不同结论 id 不同", a.id != c.id)
    d = AUTH.mk_evidence("web.search", "搜索引擎命中", "https://x.com", "analyst-inference")
    check("authority 参与 id（同内容不同级 id 不同）", a.id != d.id)

    print("[2] 四级取值合法性")
    try:
        AUTH.mk_evidence("f", "c", "v", "made-up-authority")
        check("非法 authority 必须抛 ValueError", False, "竟然没抛")
    except ValueError:
        check("非法 authority 必须抛 ValueError", True)
    check("AUTHORITY 恰好四级", sorted(AUTH.AUTHORITY) == [
        "analyst-inference", "controlled-replay", "historical-reference",
        "shipped-artifact"], str(sorted(AUTH.AUTHORITY)))

    print("[3] web.search 命中：引擎摘要外部生成 ⇒ historical-reference")
    rec = {"url": "https://x.com", "cross": 2}
    AUTH.attest(rec, "web.search", "搜索引擎命中（2 引擎交叉）",
                subject="query", authority=AUTH.authority_of_hit(rec), value=rec["url"],
                limitations=["摘要与相关度为搜索方生成，非目标站原文"])
    check("search 标 historical-reference", rec["authority"] == "historical-reference",
          rec["authority"])
    check("attest 落了 evidence 字段", "evidence" in rec and rec["evidence"]["id"])
    check("limitations 说明摘要非原文",
          any("摘要" in x for x in rec["evidence"]["limitations"]))

    print("[4] web.fetch：抓成功=实物，抓失败=推断")
    ok_rec = {"ok": True, "final_url": "https://x.com/a"}
    AUTH.attest(ok_rec, "web.fetch", "取正文结果", subject="u",
                authority=AUTH.authority_of_fetch(ok_rec), value="https://x.com/a")
    bad_rec = {"ok": False, "final_url": ""}
    AUTH.attest(bad_rec, "web.fetch", "取正文结果", subject="u",
                authority=AUTH.authority_of_fetch(bad_rec), value="u",
                limitations=["失败原因（墙/超时/截断）为推断"])
    check("抓成功标 shipped-artifact", ok_rec["authority"] == "shipped-artifact",
          ok_rec["authority"])
    check("抓失败标 analyst-inference", bad_rec["authority"] == "analyst-inference",
          bad_rec["authority"])
    check("抓失败必须写 limitations（失败原因是推断）",
          bool(bad_rec["evidence"]["limitations"]))

    print("[5] web.probe：主动探测=controlled-replay，没探到=推断")
    alive = {"alive": True, "verdict": "real", "score": 80}
    dead = {"alive": False, "verdict": "unknown", "score": 0}
    check("探到实质标 controlled-replay",
          AUTH.authority_of_probe(alive) == "controlled-replay")
    check("没探到标 analyst-inference",
          AUTH.authority_of_probe(dead) == "analyst-inference")
    check("verdict=unknown 但 alive 也算没探到",
          AUTH.authority_of_probe({"alive": True, "verdict": "unknown"}) == "analyst-inference")

    print("[6] web.structure：站点主动发布 ⇒ shipped-artifact（声明≠存在已写 limitations）")
    st = {"sitemap_urls": 100}
    AUTH.attest(st, "web.structure", "站结构", subject="u",
                authority=AUTH.authority_of_structure(st), value="100",
                limitations=["站点声明不等于真实存在，sitemap 内可有死链"])
    check("structure 标 shipped-artifact", st["authority"] == "shipped-artifact")
    check("structure 必须声明「声明不等于真实存在」",
          any("死链" in x or "不等于" in x for x in st["evidence"]["limitations"]))

    print("[7] discovery 端口投票：纯端口必须 analyst-inference（两个真实误判的根）")
    ev_ports = {"ssdp": "", "mdns": "", "http": "", "oui": "", "ports": [554, 23]}
    buckets = AUTH.score_by_source(ev_ports)
    auth = AUTH.category_authority(ev_ports, "camera", scores=buckets)
    check("纯端口投票（554+23）标 analyst-inference", auth == "analyst-inference", auth)
    check("分桶里端口分进了 ports 桶", buckets.get("camera", {}).get("ports", 0) > 0,
          str(buckets))
    check("分桶里自报桶为 0", buckets.get("camera", {}).get("self", 0) == 0)

    print("[8] discovery 设备自报：自报出现即压过端口投票（README 铁律代码化）")
    ev_self = {"ssdp": "", "mdns": "HIKVISION DS-2CD2T10FD-I3 _http._tcp",
               "http": "", "oui": "", "ports": [554, 23]}
    buckets2 = AUTH.score_by_source(ev_self)
    auth2 = AUTH.category_authority(ev_self, "camera", scores=buckets2)
    check("自报+端口同时出现仍标 shipped-artifact", auth2 == "shipped-artifact", auth2)
    check("自报分进了 self 桶", buckets2.get("camera", {}).get("self", 0) > 0, str(buckets2))

    print("[9] discovery 纯 OUI：外部注册库 ⇒ historical-reference")
    ev_oui = {"ssdp": "", "mdns": "", "http": "", "oui": "Hikvision", "ports": []}
    auth3 = AUTH.category_authority(ev_oui, "camera")
    check("纯 OUI 命中标 historical-reference", auth3 == "historical-reference", auth3)

    print("[10] recall 往事：账本原文 ⇒ historical-reference")
    import recall
    import memory as mem_mod
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    try:
        ctx = mem_mod.SharedContext(db)
        now = 1_800_000_000.0
        ctx.put("her", "game_lastnight", "昨晚那局还没玩完，说到第3关", ts=now - 26 * 3600)
        got = recall.candidates(ctx, now=now)
        check("候选条目带 authority 字段",
              got and all(g.get("authority") == "historical-reference" for g in got),
              str([g.get("authority") for g in got]))
        hint = recall.prompt_hint(got)
        check("提示词写明 authority 两级区分（原文 vs 推断）",
              "historical-reference" in hint and "analyst-inference" in hint)
        ctx.close()
    finally:
        if os.path.exists(db):
            os.remove(db)

    print()
    if SKIP:
        print("跳过 %d 项：" % len(SKIP))
        for s in SKIP:
            print("  --", s)
    if FAIL:
        print("=== t26 失败 %d 项 ===" % len(FAIL))
        for f in FAIL:
            print("  -", f)
        return 1
    print("=== t26 全通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
