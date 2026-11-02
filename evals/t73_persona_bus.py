"""门禁 t73：角色设备（persona.*）接总线的验收。

为什么需要这条门禁
------------------
2026-11-03 融合 ElecKoi（电子爱）时，天网的角色卡能力只有「读字段 + 拼提示词」：
卡里的 `[InitVar]` 开局状态、世界书扫描深度、正则脚本全被丢掉。补完这四层之后，
没有门禁就等于「下次重构会不会悄悄退回只读卡」没人知道。

这条门禁守六件事：

  一、接上了       persona.* 十条能力在总表里，设备在注册表里，参数表归一成元组
  二、宏算得对     {{char}}/{{user}} 中文边界不留空格；变量宏能寻址；显示标记被清掉
  三、设定选得准   constant 恒在 / scan_depth 只看最近 N 条 / NOT_ANY 与 AND_ALL 分得开
                   / 整词匹配不误中 / 超预算的条目被丢且说得出原因
  四、状态不撒谎   InitVar → 开场白 initvar 的覆盖顺序对；事务失败一点不改；
                   回退能回到上一个快照
  五、正则分档     $1 与 {{match}} 能展开；markdownOnly 不进提示词；EJS 显式跳过不半执行
  六、坏环境不崩   空卡库 / 坏 JSON 卡 / 找不到的卡 —— 一律结构化回答，不抛异常

    python evals/t73_persona_bus.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_OK = _FAIL = 0


def check(cond, msg):
    global _OK, _FAIL
    if cond:
        _OK += 1
    else:
        _FAIL += 1
        print("  x %s" % msg)


def sec(t):
    print("\n== %s" % t)


# --------------------------------------------------------------------------
# 测试卡：一张把四层都用上的 V3 卡
# --------------------------------------------------------------------------

def build_card(path):
    first = ("……你来了。<initvar>{\"stat_data\": {\"好感\": 3, \"背包\": [\"钥匙\"]}}</initvar>")
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "测试角色",
            "description": "{{char}} 是测试用的角色。{{user}} 的朋友。",
            "personality": "冷静",
            "scenario": "黄昏的车站",
            "mes_example": "<START>\n{{user}}: 你好\n{{char}}: 你也好，{{user}}。",
            "first_mes": first,
            "alternate_greetings": ["第二开场，没有变量块。"],
            "character_book": {
                "name": "测试世界书",
                "scan_depth": 4,
                "entries": [
                    {"name": "[InitVar]", "keys": [], "constant": True, "enabled": True,
                     "content": "{\"stat_data\": {\"好感\": 0, \"时间\": \"黄昏\", \"背包\": []}}"},
                    {"name": "常驻设定", "keys": [], "constant": True, "enabled": True,
                     "content": "这是常驻设定，每轮都在。", "insertion_order": 1},
                    {"name": "下雨", "keys": ["下雨"], "enabled": True, "insertion_order": 20,
                     "content": "下雨时站台会漏水。",
                     "extensions": {"scan_depth": 1}},
                    {"name": "排除线", "keys": ["苹果"], "secondary_keys": ["红", "甜"],
                     "selective": True, "selectiveLogic": 2, "enabled": True,
                     "insertion_order": 30, "content": "NOT_ANY：次键一个都没中才注入。"},
                    {"name": "全中线", "keys": ["苹果"], "secondary_keys": ["红", "甜"],
                     "selective": True, "selectiveLogic": 3, "enabled": True,
                     "insertion_order": 31, "content": "AND_ALL：次键全中才注入。"},
                    {"name": "整词", "keys": ["art"], "match_whole_words": True,
                     "enabled": True, "insertion_order": 40, "content": "整词 art 才注入。"},
                    {"name": "长条目", "keys": ["长"], "enabled": True, "insertion_order": 50,
                     "content": "长" * 400},
                ],
            },
            "extensions": {
                "regex_scripts": [
                    {"scriptName": "去星号", "findRegex": "/\\*(.+?)\\*/g",
                     "replaceString": "$1", "placement": [2], "disabled": False},
                    {"scriptName": "换匹配", "findRegex": "/好感(\\d+)/g",
                     "replaceString": "亲密度{{match}}", "placement": [2], "disabled": False},
                    {"scriptName": "只显示", "findRegex": "/秘密/g",
                     "replaceString": "***", "placement": [2], "markdownOnly": True},
                    {"scriptName": "EJS规则", "findRegex": "/xx/g",
                     "replaceString": "<%= 1 %>", "placement": [2], "disabled": False},
                ]
            },
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return card


def main():
    from fleet import catalog
    from fleet.kinds.persona_device import PersonaDevice
    from persona_macro import render, resolve_identity, yaml_like
    from persona_book import select, estimate_tokens
    from persona_vars import StoryState, StateError
    from persona_regex import apply as rx_apply, PLACEMENT_AI_OUTPUT

    tmp = tempfile.mkdtemp(prefix="t73_")
    cdir = os.path.join(tmp, "cards")
    sdir = os.path.join(tmp, "states")
    os.makedirs(cdir, exist_ok=True)
    card_path = os.path.join(cdir, "test.json")
    card = build_card(card_path)
    # 一张坏卡：证明坏数据不拖垮整库
    with open(os.path.join(cdir, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")

    try:
        # ------------------------------------------------------------------
        sec("一、接上了：能力进总表 + 设备在册 + 参数是元组")
        caps = {"persona.cards", "persona.card", "persona.lint", "persona.system",
                "persona.state", "persona.patch", "persona.rollback",
                "persona.snapshot", "persona.regex", "persona.tools",
                "persona.lore", "persona.export", "persona.import"}
        names = {c.name for c in catalog.CAPABILITIES}
        missing = sorted(caps - names)
        check(not missing, "能力没进总表：%s" % missing)
        reg = catalog.build(with_nodes=False)
        check(reg.device("persona:card") is not None, "persona:card 设备没注册")
        for n in sorted(caps):
            c = reg.capability(n)
            check(c is not None and isinstance(c.params, tuple),
                  "%s 的参数表没归一成元组" % n)
        for n in ("persona.patch", "persona.rollback", "persona.snapshot"):
            c = reg.capability(n)
            check(c is not None and c.side_effect is True,
                  "%s 应标 side_effect（它写 var/ 状态文件）" % n)
        for n in ("persona.cards", "persona.card", "persona.lint", "persona.state"):
            c = reg.capability(n)
            check(c is not None and c.side_effect is False,
                  "%s 是只读能力，不该标副作用" % n)

        # ------------------------------------------------------------------
        sec("二、宏：中文边界 / 变量寻址 / 显示标记")
        check(resolve_identity("{{char}} 说", char="她") == "她说",
              "中文替换后应吃掉空格：%r" % resolve_identity("{{char}} 说", char="她"))
        check(resolve_identity("{{char}} said", char="Alice") == "Alice said",
              "英文替换不该破坏原有空格")
        check(resolve_identity("{{USER}}你好", user="主人") == "主人你好",
              "大写宏名也要认")
        st = {"stat_data": {"好感": 15, "背包": ["钥匙", "地图"]}}
        text, _ = render("好感 {{get_message_variable::stat_data.好感}}，"
                         "第二件 {{get_message_variable::stat_data.背包.1}}",
                         char="她", user="主人", state=st)
        check("15" in text and "地图" in text, "变量宏取不到值：%r" % text)
        fmt, _ = render("{{format_message_variable::stat_data}}", state=st)
        check("好感: 15" in fmt and "地图" in fmt, "format 宏没输出 YAML 块：%r" % fmt[:120])
        cleaned, _ = render("<FINAL>正文</FINAL><StatusPlaceHolderImpl/>")
        check(cleaned == "正文", "显示标记没清干净：%r" % cleaned)
        _, env = render("{{setvar::心情::好}}{{getvar::心情}}")
        check(env.get("心情") == "好", "setvar 没写进变量表")

        # ------------------------------------------------------------------
        sec("三、世界书：常驻 / 扫描深度 / 逻辑模式 / 整词 / 预算")
        sel = select(card, ["第一句", "第二句 下雨"], budget_tokens=4000)
        used = {u["name"] for u in sel["used"]}
        check("常驻设定" in used, "constant 条目应恒注入")
        check("[InitVar]" in used, "[InitVar] 是 constant，也该在（由状态层消费）")
        check("长条目" not in used, "没命中关键词的条目不该进")
        # scan_depth=1：只看最近一条。把「下雨」放在更早的消息里，不该命中
        sel_old = select(card, ["下雨", "别的话"], budget_tokens=4000)
        check("下雨" not in {u["name"] for u in sel_old["used"]},
              "scan_depth=1 的条目不该被 2 条前的消息触发")
        # 书级 scan_depth=4：默认条目能看到 4 条内
        sel_deep = select(card, ["苹果 红", "别的话"], budget_tokens=4000)
        sel_deep_names = {u["name"] for u in sel_deep["used"]}
        check("全中线" not in sel_deep_names, "AND_ALL 次键只中 1 个，不该注入")
        check("排除线" not in sel_deep_names,
              "NOT_ANY 次键命中了，不该注入却注入了")
        sel_all = select(card, ["苹果 红 甜"], budget_tokens=4000)
        sel_all_names = {u["name"] for u in sel_all["used"]}
        check("全中线" in sel_all_names, "AND_ALL 次键全中却没注入")
        check("排除线" not in sel_all_names, "NOT_ANY 次键全中却注入了")
        sel_word = select(card, ["partial"], budget_tokens=4000)
        check("整词" not in {u["name"] for u in sel_word["used"]},
              "整词匹配被 partial 误触发")
        sel_word2 = select(card, ["art is fine"], budget_tokens=4000)
        check("整词" in {u["name"] for u in sel_word2["used"]}, "整词 art 应命中")
        sel_tight = select(card, ["长"], budget_tokens=60)
        dropped = {d["name"] for d in sel_tight["dropped"]}
        check("长条目" in dropped, "预算不够时应丢掉长条目并记录")
        check(sel_tight["tokens"] <= 60, "注入总量超过预算：%r" % sel_tight["tokens"])
        check(any("预算" in d["reason"] for d in sel_tight["dropped"]),
              "丢弃原因里要写明超预算")
        check(estimate_tokens("中" * 100) > estimate_tokens("a" * 100),
              "同样字符数下中文的 token 估算应更高")

        # ------------------------------------------------------------------
        sec("四、状态：初始化顺序 / 事务 / 回退")
        s = StoryState.from_card(card)
        check(s.get("stat_data.好感") == 3,
              "开场白 initvar 应覆盖 [InitVar]（0 → 3），实得 %r" % s.get("stat_data.好感"))
        check(s.get("stat_data.背包") == ["钥匙"], "initvar 的数组没写进去：%r"
              % s.get("stat_data.背包"))
        check(s.get("stat_data.时间") == "黄昏", "[InitVar] 的非覆盖字段应保留")
        before = json.dumps(s.data, sort_keys=True, ensure_ascii=False)
        try:
            s.patch([{"op": "increment", "path": "stat_data.好感", "value": 5},
                     {"op": "remove", "path": "stat_data.根本没有"}])
            check(False, "坏补丁应抛 StateError")
        except StateError:
            check(True, "")
        after = json.dumps(s.data, sort_keys=True, ensure_ascii=False)
        check(before == after, "事务失败后状态必须一点不动（现在变了）")
        s.patch([{"op": "increment", "path": "stat_data.好感", "value": 5}], note="加好感")
        check(s.get("stat_data.好感") == 8, "增量补丁没生效")
        s.snapshot("第八点")
        s.patch([{"op": "replace", "path": "stat_data.好感", "value": 99}], note="改坏")
        check(s.get("stat_data.好感") == 99, "替换补丁没生效")
        s.restore(-1)
        check(s.get("stat_data.好感") == 8, "快照没回到 8：%r" % s.get("stat_data.好感"))

        # ------------------------------------------------------------------
        sec("五、正则：捕获组 / {{match}} / 两档 / EJS 显式跳过")
        out, rep = rx_apply("*她笑了* 好感12 秘密 xx", card, placement=PLACEMENT_AI_OUTPUT,
                            for_prompt=True)
        check("她笑了" in out and "*" not in out, "$1 没展开：%r" % out)
        check("亲密度好感12" in out, "{{match}} 没展开：%r" % out)
        check("秘密" in out, "markdownOnly 规则不该改提示词：%r" % out)
        check("xx" in out, "EJS 规则应被跳过（不执行），实际改了：%r" % out)
        skipped = {x["name"]: x["why"] for x in rep["skipped"]}
        check("EJS规则" in skipped and "EJS" in skipped["EJS规则"], "EJS 跳过原因没报告")
        out2, _ = rx_apply("秘密", card, placement=PLACEMENT_AI_OUTPUT, for_prompt=False)
        check("***" in out2, "显示档应执行 markdownOnly 规则")

        # ------------------------------------------------------------------
        sec("六、设备：卡库 / 拼上下文 / 状态能力 / 坏环境")
        D = PersonaDevice(card_dir=cdir, state_dir=sdir)
        check(D.probe()[0] is True, "卡库存在时设备应可用")
        r = D.invoke("persona.cards")
        check(r.ok and r.data["total"] == 1, "应只数出 1 张好卡，实得 %r"
              % (r.data.get("total") if r.ok else r.err))
        check(r.ok and r.data["broken"] == 1, "坏卡应计入 broken 而不是被吞掉")
        r = D.invoke("persona.card", {"card": "test"})
        check(r.ok and r.data["world_book"]["entries"] == 7,
              "卡档案里的世界书条数不对：%r" % (r.data if r.ok else r.err))
        check(r.ok and r.data["regex_rules"] == 4, "正则条数不对")
        r = D.invoke("persona.system", {"card": "test", "messages": ["你好"], "budget": 800})
        check(r.ok, "persona.system 失败：%r" % (r.err if not r.ok else ""))
        sys_text = r.data.get("system") or ""
        check("常驻设定" in sys_text, "system prompt 里没有世界书内容")
        check("{{" not in sys_text, "system prompt 里还留着没渲染的宏")
        check("好感: 3" in sys_text, "system prompt 里没有世界状态：%r"
              % [l for l in sys_text.splitlines() if "好感" in l][:3])
        check(r.data["approx_tokens"] > 0, "token 估算没给")
        r = D.invoke("persona.state", {"card": "test", "path": "stat_data.好感"})
        check(r.ok and r.data["value"] == 3, "按路径取状态失败：%r"
              % (r.data if r.ok else r.err))
        r = D.invoke("persona.patch", {"card": "test", "ops": [
            {"op": "increment", "path": "stat_data.好感", "value": 2}], "note": "设备加好感"})
        check(r.ok and "好感: 5" in r.data["tree"], "设备 patch 没生效：%r"
              % (r.data if r.ok else r.err))
        r = D.invoke("persona.patch", {"card": "test", "ops": [
            {"op": "remove", "path": "stat_data.没有这个"}]})
        check((not r.ok) and "状态未改动" in (r.err or ""), "坏补丁应结构化拒绝")
        r = D.invoke("persona.snapshot", {"card": "test", "action": "save", "note": "设备快照"})
        check(r.ok and os.path.exists(r.data["path"]), "快照没落盘")
        r = D.invoke("persona.snapshot", {"card": "test", "action": "list"})
        check(r.ok and r.data["total"] >= 1, "快照列表为空")
        r = D.invoke("persona.rollback", {"card": "test", "note": "设备加好感"})
        check(r.ok and "好感: 3" in r.data["tree"], "设备回退没生效：%r"
              % (r.data if r.ok else r.err))
        r = D.invoke("persona.lint", {"card": "test"})
        check(r.ok, "体检失败：%r" % (r.err if not r.ok else ""))
        r = D.invoke("persona.regex", {"card": "test", "text": "*动作*"})
        check(r.ok and r.data["text"].strip() == "动作", "设备正则没跑通：%r"
              % (r.data if r.ok else r.err))
        r = D.invoke("persona.tools")
        check(r.ok and len(r.data["tools"]) >= 10, "创作工具清单太短")
        r = D.invoke("persona.card", {"card": "不存在的卡"})
        check((not r.ok) and "卡库里没有" in (r.err or ""), "找不到的卡应结构化回答")

        # ------------------------------------------------------------------
        sec("七、设定库分层：目录 → 搜索 → 读单条（角色自己查，不等关键词）")
        r = D.invoke("persona.lore", {"card": "test"})
        check(r.ok and r.data["total"] == 7, "目录应列出 7 条：%r"
              % (r.data.get("total") if r.ok else r.err))
        check(r.ok and all("tokens" in e for e in r.data["entries"]),
              "目录里要带每条预计 token，否则没法估预算")
        check(r.ok and len(r.data["entries"][0]["keys"]) <= 8, "目录不该把关键词全吐出来")
        r = D.invoke("persona.lore", {"card": "test", "action": "search", "q": "下雨"})
        check(r.ok and r.data["total"] >= 1 and r.data["results"][0]["name"] == "下雨",
              "搜「下雨」应命中同名条目：%r" % (r.data if r.ok else r.err))
        check(r.ok and "content" not in r.data["results"][0],
              "搜索结果不该带全文（目录层只给摘要）")
        r = D.invoke("persona.lore", {"card": "test", "action": "read", "name": "常驻设定"})
        check(r.ok and "常驻设定" in r.data["content"], "读单条失败：%r"
              % (r.data if r.ok else r.err))
        r = D.invoke("persona.lore", {"card": "test", "action": "read", "name": "没有这条"})
        check((not r.ok) and "设定库里没有" in (r.err or ""), "读不存在的条目应结构化回答")
        r = D.invoke("persona.lore", {"card": "test", "action": "别闹"})
        check((not r.ok) and "不认识的 action" in (r.err or ""), "坏 action 应被拒")

        # ------------------------------------------------------------------
        sec("八、作品包：导得出、读得回、坏包不认")
        r = D.invoke("persona.export", {"card": "test"})
        check(r.ok, "导出失败：%r" % (r.err if not r.ok else ""))
        pkg = r.data["package"] if r.ok else {}
        check(pkg.get("kind") == "fleet.persona.package", "作品包 kind 不对")
        check(pkg.get("card", {}).get("name") == "测试角色", "作品包里没有卡")
        check("stat_data" in (pkg.get("state") or {}).get("data", {}),
              "作品包里没带状态")
        check(pkg.get("summary", {}).get("world_book_entries") == 7,
              "作品包的世界书条数不对")
        check(len(pkg.get("regex") or []) == 4, "作品包的正则条数不对")
        r2 = D.invoke("persona.import", {"package": pkg})
        check(r2.ok and r2.data["name"] == "测试角色", "导出的包读不回：%r"
              % (r2.err if not r2.ok else ""))
        check(r2.ok and r2.data["state_keys"] >= 1, "读回后状态键丢了")
        r3 = D.invoke("persona.import", {"package": {"kind": "别的东西"}})
        check((not r3.ok) and "不是天网作品包" in (r3.err or ""), "非作品包应被拒")
        r4 = D.invoke("persona.import", {"package": {"kind": "fleet.persona.package",
                                                    "card": {}}})
        check((not r4.ok) and "缺 name" in (r4.err or ""), "缺卡名的包应被拒")

        # 空卡库：设备不能因为没卡就下线
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        D2 = PersonaDevice(card_dir=empty, state_dir=sdir)
        check(D2.probe()[0] is True, "空卡库时设备仍应在线")
        r = D2.invoke("persona.cards")
        check(r.ok and r.data["total"] == 0, "空卡库应返回 0 张")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== t73: %d 过 / %d 败 ===" % (_OK, _FAIL))
    return _FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
