#!/usr/bin/env python
"""门禁 t15：角色卡商店（浏览 / 下载 / 体检 / 入库 / 切换）。

**这个门禁钉的是六件真事**（每一条都是本轮实测抓出来的坑）：

  ① **陌生 schema 的卡不许被当成 V1 平铺读空。**
     上轮实测：某社区站的卡 spec="aicc_card"（私有 schema），
     落进「V1 顶层平铺」分支 ⇒ build_system 只吐 47 字 ⇒ **卡内容全丢且不报错**。
     ⇒ 陌生 spec 必须抛明确错误；已知私有 schema 必须走适配器且**内容零丢失**。

  ② **体检器判的是「她到底会不会说话」，不是「字段在不在」。**
     真卡里 personality 可以是空的（作者把性格写进 description 了）——
     把它当否决项会误伤好卡。
     ⇒ 判据是 build_system 拼出来多少字（唯一权威），字段缺失只当提醒。

  ③ **下载必须完整**：本机下载会中途截断（实测 359KB / 256KB 断点），
     半张 PNG 存进 cards/ 之后只会得到「PNG 里没有角色卡块」。
     ⇒ 必须校验走到 IEND；不完整就不许入库（并且不许留半成品）。

  ④ **下载有冷却**：同卡 30 分钟。重复打要如实说「冷却还剩多久」，
     不许静默重打让站点报错。

  ⑤ **内部记账文件不许被当成角色卡。**
     实测踩到：`.downloads.json` 被列进界面，变成一张「（无名）· v1」的假卡。

  ⑥ **下载/入库是副作用 ⇒ 必须过闸门。**
     往 cards/ 写文件 = 改「她是谁」这一层。
     没有 gate 时注册表必须 fail-closed 拒绝（不许「界面能点就算」）。

**全离线**：不连真网络。下载层用假 HTTP 源（本地 file:// 风格的假 _http），
搜索引擎与站点接口全部打桩。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def skip(msg):
    print("  --", msg)
    SKIP.append(msg)


def main():
    import card_store as cs
    import persona_card as pc

    tmp = tempfile.mkdtemp(prefix="fleet-t15-")
    cards = os.path.join(tmp, "cards")
    os.makedirs(cards, exist_ok=True)

    # ---------- ① 陌生 spec 必须报明确原因，不许降级读空 ----------
    bad = os.path.join(cards, "unknown.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"spec": "some_unknown_schema", "name": "X"}')
    try:
        pc.load_card(bad)
        check(False, "陌生 spec 应当抛错（实际没抛）")
    except ValueError as e:
        msg = str(e)
        check("some_unknown_schema" in msg and "只认" in msg,
              "陌生 spec 报错说清了「不认哪个」与「只认哪几种」")
    except Exception as e:
        check(False, "陌生 spec 抛的不是 ValueError：%s" % type(e).__name__)

    # ---------- ② 体检器：判据是「拼得出人设」 ----------
    hollow = os.path.join(cards, "hollow.json")
    with open(hollow, "w", encoding="utf-8") as f:
        json.dump({"name": "空壳"}, f, ensure_ascii=False)
    rep = cs.lint_card(hollow)
    check(rep["ok"] is False, "空壳卡体检不通过")
    check(rep["built_chars"] < cs.MIN_SYSTEM,
          "空壳卡拼出人设只有 %d 字（< %d）" % (rep["built_chars"], cs.MIN_SYSTEM))

    # 真卡：本仓自己的一张，必须通过
    real = os.path.join(ROOT, "cards", "大肥鱼.json")
    if os.path.exists(real):
        rep = cs.lint_card(real)
        check(rep["ok"] is True, "本仓真卡体检通过")
        check(rep["built_chars"] > cs.MIN_SYSTEM,
              "真卡拼出人设 %d 字" % rep["built_chars"])
    else:
        skip("cards/大肥鱼.json 不在，跳过真卡体检")

    # personality 空但 description 厚的卡 ⇒ **不该**被判死
    rich = os.path.join(cards, "rich.json")
    with open(rich, "w", encoding="utf-8") as f:
        json.dump({"name": "性格写在描述里", "description": "她" * 300,
                   "personality": "", "first_mes": "你好"},
                  f, ensure_ascii=False)
    rep = cs.lint_card(rich)
    check(rep["ok"] is True, "personality 空但内容厚的卡仍然通过（不误伤）")
    check(any(i["field"] == "personality" and i["level"] == "warn"
              for i in rep["issues"]),
          "personality 空只当提醒，不当否决项")

    # ---------- ③ PNG 完整性判定 ----------
    ok_png, why = cs._png_complete(b"\x89PNG\r\n\x1a\n")
    check(ok_png is False and why, "假 PNG 被判不完整并说明原因")
    ok_png, why = cs._png_complete(b"hello")
    check(ok_png is False and "PNG" in why, "非 PNG 被判不完整")
    # 真 PNG（有 IEND）必须判完整
    import struct
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + b"\x00\x00\x00\x00"
    good_png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\x00" * 13)
                + chunk(b"IEND", b""))
    ok_png, why = cs._png_complete(good_png)
    check(ok_png is True, "完整 PNG（含 IEND）判为完整")

    # ---------- ④ 下载必须完整：不完整要报错且不留半成品 ----------
    def fake_http_truncated(url, headers=None, timeout=0):
        # 前 100 字节正常，之后假装被截断（没有 IEND）
        return 200, good_png[:12] + b"\x00" * 88, {}

    real_http = cs._http
    try:
        cs._http = fake_http_truncated
        dest = os.path.join(cards, "trunc.png")
        raised = ""
        try:
            cs.download("https://example.invalid/x.png", dest)
        except cs.SourceError as e:
            raised = str(e)
        check(raised != "", "截断的下载被拒绝并报错")
        check(not os.path.exists(dest), "下载失败没留下半成品文件")
        check(not os.path.exists(dest + ".part"), "没留下 .part 临时文件")
    finally:
        cs._http = real_http

    # ---------- ⑤ 冷却：重复下载要如实说冷却多久 ----------
    cs.note_download(cards, "aicc", 42, os.path.join(cards, "x.png"))
    left = cs.cooldown_left(cards, "aicc", 42)
    check(1700 < left <= 1800, "下过的卡冷却约 30 分钟（剩 %d 秒）" % left)
    check(cs.cooldown_left(cards, "aicc", 43) == 0, "没下过的卡没有冷却")

    # ---------- ⑥ 内部记账文件不许被当成卡 ----------
    cs.note_download(cards, "aicc", 7, os.path.join(cards, "y.png"))
    names = [c["file"] for c in pc.list_cards(cards)]
    check(".downloads.json" not in names,
          "下载账本没被列成角色卡（列表=%s）" % names)

    # ---------- ⑦ 投递口：坏卡不许混进库 ----------
    inbox = cs.inbox_dir(cards)
    os.makedirs(inbox, exist_ok=True)
    with open(os.path.join(inbox, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    with open(os.path.join(inbox, "hollow2.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "空壳"}, f, ensure_ascii=False)
    with open(os.path.join(inbox, "good.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "好卡", "description": "她" * 200, "first_mes": "在"},
                  f, ensure_ascii=False)
    res = cs.ingest_inbox(cards)
    # 🔴 `file` 恒为投递口原名，落库名在 `stored_as` —— 断言按这个契约写。
    by = {i["file"]: i for i in res["items"]}
    check(by.get("broken.json", {}).get("ok") is False, "坏 JSON 没入库")
    check(by.get("hollow2.json", {}).get("ok") is False, "空壳卡没入库")
    check(by.get("good.json", {}).get("ok") is True, "好卡入库了")
    check(by.get("good.json", {}).get("stored_as") == cs.STORE_PREFIX + "good.json",
          "好卡的落库名带 store_ 前缀")
    check(by.get("broken.json", {}).get("stored_as") == "",
          "没入库的卡没有落库名")
    lib = os.listdir(cards)
    check(not any(f in ("broken.json", "hollow2.json",
                        cs.STORE_PREFIX + "broken.json",
                        cs.STORE_PREFIX + "hollow2.json")
                  for f in lib),
          "库目录里没混进坏卡")
    # 入库后文件名前缀是 store_（= 外来的卡，git 靠它精确忽略）
    check(cs.STORE_PREFIX + "good.json" in lib,
          "好卡真的落进库目录了（文件名带 store_ 前缀）")
    check(not os.path.exists(os.path.join(inbox, "good.json")),
          "入库后投递口里的原件被移走了（move 默认开）")

    # ---------- ⑧ 副作用能力必须过闸门（fail-closed） ----------
    from fleet.catalog import build
    reg = build()
    caps = [c for c in reg.capabilities() if c.name.startswith("card.")]
    check(len(caps) == 6, "注册表里有 6 项 card.* 能力（实际 %d）" % len(caps))
    fetch = reg.capability("card.fetch")
    check(fetch is not None and fetch.side_effect,
          "card.fetch 是副作用能力（写 cards/）")
    search = reg.capability("card.search")
    check(search is not None and not search.side_effect,
          "card.search 是只读能力（不改任何东西）")

    # 没有闸门 ⇒ 副作用被拒（不许「界面能点就算」）
    r = reg.call("card.fetch", device="cards:store",
                 args={"source": "aicc", "id": "1"})
    check(r.ok is False and "闸门" in (r.err or ""),
          "没有闸门时 card.fetch 被 fail-closed 拒绝：%s" % (r.err or ""))
    r = reg.call("card.search", device="cards:store", args={"query": "x"})
    check(r.ok is True, "只读能力 card.search 不受闸门影响（err=%s）" % (r.err or ""))

    # ---------- ⑨ 切换：坏卡不许切 ----------
    try:
        pc.set_active("broken.json", cards)
        check(False, "坏卡竟然切成功了")
    except Exception:
        check(True, "切到坏卡时报错（不许半路坏掉 active.json）")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if SKIP:
        print("跳过 %d 项：" % len(SKIP))
        for s in SKIP:
            print("  --", s)
    if FAIL:
        print("=== t15 失败 %d 项 ===" % len(FAIL))
        for f in FAIL:
            print("  -", f)
        return 1
    print("=== t15 全通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
