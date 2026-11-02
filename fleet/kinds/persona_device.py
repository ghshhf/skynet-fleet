"""角色设备：把角色卡从「一段提示词素材」变成「一局能演下去的世界」。

为什么要有这一台（2026-11-03，主人要求融合 ElecKoi）：

天网原来有 `persona_card.py`（读卡、世界书、拼 prompt）和 `card_store`（找卡、下卡、
体检），但**没有一个能力能维持一局演出的状态**：
  - 卡里 `[InitVar]` 写的初始好感/背包/时间线，读出来就丢；
  - 世界书是「命中就全塞」，没有扫描深度也没有预算；
  - 作者写的正则脚本（去标记、折叠状态栏）完全不生效；
  - 想让角色「记得上一轮发生了什么」，只能靠把整段对话塞回去。

ElecKoi 在 TS 侧把这四件事都解了（MVU 变量 + 事务提交 + 世界书扫描深度 + 正则层），
本设备把那套语义接进天网总线，让 agent 能：
  读卡 → 起局（变量落地）→ 每轮选设定 → 改状态（可回滚）→ 存/读快照。

🔴 边界：
  - **不跑 LLM**。这一台只负责「上下文怎么拼、状态怎么变」，生成还是走 llm 设备。
  - **不执行 JS/EJS**。卡里需要跑 JS 的部分（EJS 模板、Zod schema）显式标记不支持。
  - **状态写在 `var/persona_states/`**，是可重建的运行态，不走审批闸门。
    改 `cards/`（她是谁）才走闸门——这个边界跟 card_store 一致。
"""
import json
import os
import sys
import time

from ..core import BaseDevice, DeviceResult as R

#: 仓库根：persona_* 四个纯算法模块与 persona_card 都在这层。
#: 不管从哪个工作目录导入 fleet，都先把根塞进 sys.path——否则设备会静默降级。
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import persona_book
    import persona_macro
    import persona_regex
    import persona_vars
except Exception:  # pragma: no cover - 缺模块时设备仍要能注册，只是能力降级
    persona_book = persona_macro = persona_regex = persona_vars = None

try:  # 角色卡加载器在仓库根，不在 fleet 包里
    import persona_card
except Exception:  # pragma: no cover - 极端情况下设备仍要能注册
    persona_card = None

DEFAULT_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "var", "persona_states")

#: 创作工具清单：ElecKoi 的 creatorCapabilities 摊平后的语义清单。
#: 天网没有它那套 UI，所以这里给的是「要做一张卡，哪些动作是必须的」——agent 照着做。
AUTHOR_TOOLS = (
    ("character.create", "新建一张空卡（只有名字与分组）"),
    ("character.read", "读出卡的全部资料"),
    ("character.update", "改名字/分组/年龄/性别/身高/生日/喜好"),
    ("character.read_openings", "读全部开场白与默认选择"),
    ("character.save_openings", "保存开场白与变量绑定"),
    ("settings.list", "列设定条目（必读 / 选读关键词 / 选读工具）"),
    ("settings.read", "读一条完整设定"),
    ("settings.create", "新增设定条目"),
    ("settings.update", "改设定：正文、关键词、读取策略、EJS 开关"),
    ("settings.delete", "删设定条目"),
    ("variables.read", "读变量结构与初始态"),
    ("variables.write", "改变量结构（对象 / 字段 / 更新规则）"),
    ("regex.list", "列正则规则"),
    ("regex.upsert", "新增或修改正则（find / replace / placement）"),
    ("regex.delete", "删正则规则"),
    ("assets.image", "给角色挂立绘 / 表情素材"),
    ("frontend.preview", "预览角色前端（HTML/CSS/JS 卡片界面）"),
)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class PersonaDevice(BaseDevice):
    kind = "persona"
    via = "local"

    def __init__(self, name="persona:card", card_dir=None, state_dir=None, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "persona")
        self._card_dir = card_dir
        self._state_dir = state_dir
        self._states = {}

    # ---------------- 路径 ----------------

    def card_dir(self):
        if self._card_dir:
            return self._card_dir
        return getattr(persona_card, "DEFAULT_CARD_DIR", "cards")

    def state_dir(self):
        d = self._state_dir or os.environ.get("FLEET_PERSONA_STATE") or DEFAULT_STATE_DIR
        os.makedirs(d, exist_ok=True)
        return d

    # ---------------- 卡 ----------------

    def _cards(self):
        if persona_card is None:
            return []
        return persona_card.list_cards(self.card_dir())

    @staticmethod
    def _prefer(items):
        """同 id 有多份时选一份：.json/.png（结构化）优先于 .md（源卡）。

        卡库里 md 是作者写的源，json 是编译产物，两者 id 相同。设备如果不去重，
        「26 张卡」里有一半是同一张卡的两种形态——列出来只会让人以为卡库翻倍了。
        """
        def rank(c):
            f = str(c.get("file") or "").lower()
            return 0 if f.endswith((".json", ".png")) else 1
        return sorted(items, key=rank)[0]

    def _find(self, key):
        """按 id / 文件名 / 'active' 找一张卡，返回 (记录, data)。"""
        key = str(key or "active").strip()
        cards = self._cards()
        if key in ("", "active", "current"):
            if persona_card is None:
                raise KeyError("角色卡加载器不可用")
            cur = persona_card.active_card(self.card_dir())
            if cur is None:
                raise KeyError("还没有选中任何角色卡")
            return {"id": "active", "file": "active"}, cur
        hits = [c for c in cards
                if key in (c.get("id"), c.get("file"), os.path.splitext(str(c.get("file")))[0])]
        if not hits:
            raise KeyError("卡库里没有 %r（可用 persona.cards 看有哪些）" % key)
        rec = self._prefer(hits)
        if not rec.get("ok"):
            raise KeyError("这张卡读不出来：%s" % rec.get("error"))
        return rec, persona_card.load_card(rec["path"])

    def _state_path(self, card_id):
        return os.path.join(self.state_dir(), "%s.json" % (card_id or "active"))

    def _state(self, card_id, card_data=None, opening=0):
        st = self._states.get(card_id)
        if st is not None:
            return st
        p = self._state_path(card_id)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    st = persona_vars.StoryState.from_dict(json.load(f))
                self._states[card_id] = st
                return st
            except Exception:
                pass  # 坏文件当没存过，重新起局（别让一个坏 json 卡死整台设备）
        st = persona_vars.StoryState.from_card(card_data or {}, opening)
        self._states[card_id] = st
        return st

    def _save_state(self, card_id, st):
        p = self._state_path(card_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    # ---------------- 契约 ----------------

    def probe(self):
        """恒可用：卡库空着也如实返回空，不让整台设备下线。"""
        if persona_card is None:
            return True, "persona_card 模块不可导入（只影响读卡相关能力）"
        try:
            os.makedirs(self.state_dir(), exist_ok=True)
        except OSError as e:
            return True, "状态目录不可写：%s" % e
        return True, ""

    def caps(self):
        return ("persona.cards", "persona.card", "persona.lint", "persona.system",
                "persona.state", "persona.patch", "persona.rollback",
                "persona.snapshot", "persona.regex", "persona.tools",
                "persona.lore", "persona.export", "persona.import")

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "persona.cards":
                return self._cards_cap()
            if cap == "persona.card":
                return self._card_cap(args)
            if cap == "persona.lint":
                return self._lint(args)
            if cap == "persona.system":
                return self._system(args)
            if cap == "persona.state":
                return self._state_cap(args)
            if cap == "persona.patch":
                return self._patch(args)
            if cap == "persona.rollback":
                return self._rollback(args)
            if cap == "persona.snapshot":
                return self._snapshot(args)
            if cap == "persona.regex":
                return self._regex(args)
            if cap == "persona.tools":
                return self._tools()
            if cap == "persona.lore":
                return self._lore(args)
            if cap == "persona.export":
                return self._export(args)
            if cap == "persona.import":
                return self._import(args)
        except KeyError as e:
            return R.error(cap, str(e).strip("'\""), device=self.name, via=self.via)
        except persona_vars.StateError as e:
            return R.error(cap, "状态操作失败：%s" % e, device=self.name, via=self.via)
        except Exception as e:  # 一台设备不许因为一张坏卡把总线带崩
            return R.error(cap, "%s: %s" % (type(e).__name__, e), device=self.name, via=self.via)
        return super().invoke(cap, args)

    # ---------------- 读卡 ----------------

    def _cards_cap(self):
        cards = self._cards()
        # 同 id 归并：卡库里 .md（源）与 .json（编译产物）是同一张卡
        merged = {}
        for c in cards:
            cid = str(c.get("id"))
            item = merged.setdefault(cid, {"id": cid, "files": [], "ok": False})
            item["files"].append(c.get("file"))
            if c.get("ok"):
                item["ok"] = True
                item["name"] = c.get("name")
                item.setdefault("file", c.get("file"))
            else:
                item.setdefault("error", c.get("error"))
        for cid, item in merged.items():
            ok_files = [x for x in item["files"] if str(x).lower().endswith((".json", ".png"))]
            if ok_files:
                item["file"] = ok_files[0]
        out = list(merged.values())
        for c in cards:
            # 坏卡：merged 里已经建了条目（ok=False），别再追加一份
            if not c.get("ok") and str(c.get("id")) not in merged:
                out.append({"id": c.get("id"), "files": [c.get("file")], "ok": False,
                            "error": c.get("error")})
        return R.success("persona.cards", self.name, via=self.via, data={
            "dir": self.card_dir(),
            "total": len([x for x in out if x.get("ok")]),
            "broken": len([x for x in out if not x.get("ok")]),
            "cards": out,
        })

    def _card_cap(self, args):
        rec, data = self._find(args.get("card"))
        keys = persona_book.entries_of(data)
        rules = persona_regex.rules_of(data)
        state, update_rules = persona_vars.initial_state(data, _int(args.get("opening")))
        stat = state.get("stat_data") if isinstance(state, dict) else {}
        return R.success("persona.card", self.name, via=self.via, data={
            "id": rec.get("id"),
            "file": rec.get("file"),
            "name": data.get("name"),
            "creator": data.get("creator") or "",
            "tags": data.get("tags") or [],
            "fields": {k: len(str(data.get(k) or "")) for k in
                       ("description", "personality", "scenario", "mes_example",
                        "system_prompt", "post_history_instructions")},
            "openings": 1 + len(data.get("alternate_greetings") or []),
            "world_book": {"entries": len(keys),
                           "constant": sum(1 for e in keys if e["constant"]),
                           "selective": sum(1 for e in keys if e["selective"]),
                           "regex_keys": sum(1 for e in keys if e["use_regex"])},
            "regex_rules": len(rules),
            "state": {"keys": list(stat.keys())[:20] if isinstance(stat, dict) else [],
                      "update_rules": len(update_rules)},
            "has_ejs": persona_macro.looks_like_ejs(json.dumps(data, ensure_ascii=False)),
        })

    def _lint(self, args):
        rec, data = self._find(args.get("card"))
        book = persona_book.lint(data)
        rx = persona_regex.lint(data)
        warn = [i for i in book["issues"] + rx["issues"] if i.get("level") in ("warn", "error")]
        return R.success("persona.lint", self.name, via=self.via, data={
            "id": rec.get("id"), "name": rec.get("name"),
            "world_book": book, "regex": rx,
            "verdict": "可用" if not warn else "%d 处需要修" % len(warn),
        })

    # ---------------- 起局：拼上下文 ----------------

    def _system(self, args):
        """拼这一轮的 system prompt：人设 + 命中设定 + 世界状态。

        这是本设备的主菜。天网原来的 build_system 只拼静态字段，这里补三件动态的：
        世界书按预览深度命中、变量树按 YAML 摊平、正则规则的「只改提示词」档也跑。
        """
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        opening = _int(args.get("opening"))
        st = self._state(card_id, data, opening)

        messages = args.get("messages")
        if messages is None:
            messages = args.get("text") or ""
        budget = _int(args.get("budget"), 1500) or 1500
        sel = persona_book.select(data, messages, budget_tokens=budget,
                                  per_entry_depth=True)
        user_name = str(args.get("user_name") or "主人")
        name = str(data.get("name") or "她")

        # 世界状态：只放标量摘要 + 一层的树，别把整个 state 糊上去
        stat = st.data.get("stat_data") if isinstance(st.data, dict) else {}
        state_block = ""
        if isinstance(stat, dict) and stat:
            state_block = "【世界状态（事实，不是台词）】\n" + persona_macro.yaml_like(stat, 0)
            rules = list(st.rules.items())[:20]
            if rules:
                state_block += "\n\n【状态更新规则】\n" + "\n".join(
                    "- %s：%s" % (k, v) for k, v in rules)

        extra_parts = []
        if sel["before"]:
            extra_parts.append("【设定（本轮命中）】\n" + "\n\n".join(sel["before"]))
        if state_block:
            extra_parts.append(state_block)
        if args.get("extra"):
            extra_parts.append(str(args["extra"]))

        base = persona_card.build_system(data, state=None, extra="\n\n".join(extra_parts),
                                        user_name=user_name)
        text = base + ("\n\n" + "\n\n".join(sel["after"]) if sel["after"] else "")

        # 提示词方向的正则（prompt_only）
        text, rx_report = persona_regex.apply(text, data, placement=persona_regex.PLACEMENT_AI_OUTPUT,
                                             for_prompt=True)
        # 宏最后跑：char/user + 变量 + 显示标记
        text, env = persona_macro.render(text, char=name, user=user_name, state=st.data)

        return R.success("persona.system", self.name, via=self.via, data={
            "id": card_id,
            "chars": len(text),
            "approx_tokens": persona_book.estimate_tokens(text),
            "system": text,
            "world_book": {"used": sel["used"], "dropped": sel["dropped"],
                           "tokens": sel["tokens"], "budget": sel["budget"]},
            "state_keys": len(stat) if isinstance(stat, dict) else 0,
            "vars": env,
            "regex": rx_report,
        })

    # ---------------- 状态 ----------------

    def _state_cap(self, args):
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        st = self._state(card_id, data, _int(args.get("opening")))
        path = str(args.get("path") or "")
        if path:
            return R.success("persona.state", self.name, via=self.via, data={
                "id": card_id, "path": path,
                "value": persona_vars.get_path(st.data, path),
            })
        return R.success("persona.state", self.name, via=self.via, data={
            "id": card_id,
            "summary": st.summary(),
            "tree": persona_macro.yaml_like(st.data.get("stat_data") or {}),
            "update_rules": len(st.rules),
            "history": len(st.history),
            "in_txn": st._txn is not None,
        })

    def _patch(self, args):
        """改状态：事务语义——要么全成，要么一点不动，失败自动回滚。"""
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        st = self._state(card_id, data, _int(args.get("opening")))
        ops = args.get("ops")
        if isinstance(ops, str):
            ops = persona_vars.lenient_json(ops)
        if not isinstance(ops, list):
            return R.error("persona.patch", "ops 要传数组（[{op,path,value}]）",
                           device=self.name, via=self.via)
        before = json.dumps(st.data, ensure_ascii=False, sort_keys=True)
        try:
            st.patch(ops, note=str(args.get("note") or ""))
        except persona_vars.StateError as e:
            return R.error("persona.patch", "补丁被拒（状态未改动）：%s" % e,
                           device=self.name, via=self.via)
        after = json.dumps(st.data, ensure_ascii=False, sort_keys=True)
        self._save_state(card_id, st)
        return R.success("persona.patch", self.name, via=self.via, data={
            "id": card_id, "ops": len(ops), "changed": before != after,
            "tree": persona_macro.yaml_like(st.data.get("stat_data") or {}),
        })

    def _rollback(self, args):
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        st = self._state(card_id, data, _int(args.get("opening")))
        note = str(args.get("note") or "")
        target = None
        for h in reversed(st.history):
            if h.get("before") is None:
                continue
            if not note or note in str(h.get("note") or ""):
                target = h
                break
        if target is None:
            return R.error("persona.rollback", "没有可回退的记录%s"
                           % ("（note=%r）" % note if note else ""),
                           device=self.name, via=self.via)
        st.data = target["before"]
        st.history.remove(target)
        self._save_state(card_id, st)
        return R.success("persona.rollback", self.name, via=self.via, data={
            "id": card_id, "rolled_back": target.get("note"),
            "tree": persona_macro.yaml_like(st.data.get("stat_data") or {}),
        })

    def _snapshot(self, args):
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        st = self._state(card_id, data, _int(args.get("opening")))
        action = str(args.get("action") or "save")
        if action == "list":
            snaps = [{"index": i, "note": h.get("note")}
                     for i, h in enumerate([x for x in st.history if x.get("snapshot")])]
            return R.success("persona.snapshot", self.name, via=self.via, data={
                "id": card_id, "snapshots": snaps, "total": len(snaps)})
        if action == "restore":
            st.restore(_int(args.get("index"), -1))
            self._save_state(card_id, st)
            return R.success("persona.snapshot", self.name, via=self.via, data={
                "id": card_id, "restored": _int(args.get("index"), -1),
                "tree": persona_macro.yaml_like(st.data.get("stat_data") or {})})
        st.snapshot(note=str(args.get("note") or ""))
        self._save_state(card_id, st)
        snaps = [x for x in st.history if x.get("snapshot")]
        return R.success("persona.snapshot", self.name, via=self.via, data={
            "id": card_id, "saved": snaps[-1].get("note") if snaps else "",
            "total": len(snaps), "path": self._state_path(card_id)})

    # ---------------- 正则 ----------------

    def _regex(self, args):
        text = str(args.get("text") or "")
        placement = _int(args.get("placement"), persona_regex.PLACEMENT_AI_OUTPUT)
        for_prompt = bool(args.get("for_prompt", True))
        rules = None
        card_id = args.get("card")
        if card_id:
            rec, data = self._find(card_id)
            rules = persona_regex.rules_of(data)
        elif args.get("rules") is not None:
            payload = args.get("rules")
            if isinstance(payload, str):
                payload = persona_vars.lenient_json(payload)
            rules = persona_regex.normalize(payload)
        out, report = persona_regex.apply(text, placement=placement,
                                          for_prompt=for_prompt, rules=rules)
        return R.success("persona.regex", self.name, via=self.via, data={
            "before": len(text), "after": len(out), "text": out, "report": report,
        })

    def _tools(self):
        return R.success("persona.tools", self.name, via=self.via, data={
            "source": "ElecKoi creatorStudio 能力矩阵（语义对照，不是代码搬运）",
            "tools": [{"id": t, "desc": d} for t, d in AUTHOR_TOOLS],
            "note": "天网侧落地哪些：settings.*/variables.*/regex.* 已由本设备与 persona_* 模块提供；"
                    "frontend.preview 需要浏览器，天网只给成品 HTML 不跑前端",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # ---------------- 设定库（角色自己查） ----------------

    def _lore(self, args):
        """让角色**自己查设定**，而不是等关键词命中。

        为什么这条重要（ElecKoi 的第一卖点就是它）：
          世界书是「被动触发」——作者得猜观众会说什么词。但真演起来，角色需要的是
          「我现在想提一下那个组织，它叫什么来着」这种**主动检索**。
          把设定拆成「目录（只给标题与关键词，几十 token）」+「单条正文（用到才取）」，
          长卡的设定可以几百条而不炸上下文。

        action：
          list   给目录（标题 / 关键词 / 是否常驻 / 预计 token），默认
          search 按词搜标题+关键词+正文，给命中的轻量摘要
          read   取一条的完整正文（给 name 精确或包含匹配）
        """
        rec, data = self._find(args.get("card"))
        entries = persona_book.entries_of(data)
        action = str(args.get("action") or "list")
        limit = max(1, min(_int(args.get("limit"), 30), 200))

        if action == "list":
            rows = [{"name": e["name"], "keys": e["keys"][:8],
                     "constant": e["constant"], "enabled": e["enabled"],
                     "order": e["insertion_order"],
                     "tokens": persona_book.estimate_tokens(e["content"])}
                    for e in entries if e["enabled"]][:limit]
            return R.success("persona.lore", self.name, via=self.via, data={
                "id": rec.get("id"), "action": "list",
                "total": len(entries), "shown": len(rows), "entries": rows,
                "hint": "先用目录挑，再用 action=read + name 取正文；别一次全读",
            })

        if action == "search":
            q = str(args.get("q") or args.get("query") or "").strip()
            if not q:
                return R.error("persona.lore", "search 要给 q", device=self.name, via=self.via)
            terms = [t.lower() for t in q.replace("，", " ").replace(",", " ").split() if t]
            hits = []
            for e in entries:
                if not e["enabled"]:
                    continue
                name, keys, body = e["name"].lower(), [k.lower() for k in e["keys"]], e["content"].lower()
                score = 0
                for t in terms:
                    if t in name:
                        score += 5
                    if any(t in k for k in keys):
                        score += 3
                    if t in body:
                        score += 1
                if score:
                    hits.append((score, e))
            hits.sort(key=lambda x: (-x[0], x[1]["insertion_order"]))
            return R.success("persona.lore", self.name, via=self.via, data={
                "id": rec.get("id"), "action": "search", "q": q,
                "total": len(hits),
                "results": [{"name": e["name"], "keys": e["keys"][:6], "score": s,
                             "tokens": persona_book.estimate_tokens(e["content"]),
                             "preview": e["content"][:120]}
                            for s, e in hits[:limit]],
            })

        if action == "read":
            key = str(args.get("name") or args.get("id") or "").strip()
            if not key:
                return R.error("persona.lore", "read 要给 name（条目名或其中一段）",
                               device=self.name, via=self.via)
            exact = [e for e in entries if e["name"] == key]
            cand = exact or [e for e in entries if key in e["name"]]
            if not cand:
                return R.error("persona.lore", "设定库里没有 %r" % key,
                               device=self.name, via=self.via)
            e = cand[0]
            max_chars = max(200, min(_int(args.get("max_chars"), 4000), 40000))
            body = e["content"]
            return R.success("persona.lore", self.name, via=self.via, data={
                "id": rec.get("id"), "action": "read", "name": e["name"],
                "keys": e["keys"], "constant": e["constant"],
                "chars": len(body), "truncated": len(body) > max_chars,
                "content": body[:max_chars],
                "matches": [x["name"] for x in cand[:5]] if len(cand) > 1 else [],
            })

        return R.error("persona.lore", "不认识的 action：%r（list / search / read）" % action,
                       device=self.name, via=self.via)

    # ---------------- 作品包 ----------------

    def _export(self, args):
        """把一张卡连同当前状态打成可迁移的作品包。

        为什么需要：这是「内容生成」的交付物。单给卡 JSON 只交出人设，
        这一局已经演到什么程度（好感、背包、时间线）全丢了；下次接着演等于重开。
        """
        rec, data = self._find(args.get("card"))
        card_id = rec.get("id") or "active"
        st = self._state(card_id, data, _int(args.get("opening")))
        include_state = args.get("include_state", True) is not False
        pkg = {
            "kind": "fleet.persona.package",
            "version": 1,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"device": self.name, "card_id": card_id,
                       "file": rec.get("file"), "engine": "skynet-fleet/persona"},
            "card": data,
            "world_book": {"entries": persona_book.entries_of(data)},
            "regex": persona_regex.rules_of(data),
            "summary": {
                "name": data.get("name"),
                "world_book_entries": len(persona_book.entries_of(data)),
                "regex_rules": len(persona_regex.rules_of(data)),
                "state_keys": len(st.data.get("stat_data") or {}),
            },
        }
        if include_state:
            pkg["state"] = st.to_dict()
        payload = json.dumps(pkg, ensure_ascii=False)
        return R.success("persona.export", self.name, via=self.via, data={
            "id": card_id,
            "bytes": len(payload.encode("utf-8")),
            "package": pkg if args.get("inline", True) is not False else None,
            "json": payload,
            "hint": "要用真实文件就让 file.write 落盘；本能力只打包，不替你选路径",
        })

    def _import(self, args):
        """读一个作品包，校验后返回结构；**不落盘**（写 cards/ 要走 card.ingest 的闸门）。"""
        raw = args.get("package") or args.get("json") or args.get("path")
        if isinstance(raw, dict):
            pkg = raw
        elif isinstance(raw, str) and raw.strip().startswith("{"):
            pkg = persona_vars.lenient_json(raw)
        elif isinstance(raw, str) and raw.strip():
            with open(raw, encoding="utf-8") as f:
                pkg = json.load(f)
        else:
            return R.error("persona.import", "缺 package（JSON 字符串 / 对象 / 文件路径）",
                           device=self.name, via=self.via)
        if not isinstance(pkg, dict):
            return R.error("persona.import", "作品包不是合法 JSON 对象",
                           device=self.name, via=self.via)
        if pkg.get("kind") != "fleet.persona.package":
            return R.error("persona.import", "不是天网作品包（kind=%r）" % pkg.get("kind"),
                           device=self.name, via=self.via)
        card = pkg.get("card")
        if not isinstance(card, dict) or not str(card.get("name") or "").strip():
            return R.error("persona.import", "作品包里没有可用的卡（缺 name）",
                           device=self.name, via=self.via)
        state = pkg.get("state") if isinstance(pkg.get("state"), dict) else None
        st = None
        if state:
            st = persona_vars.StoryState.from_dict(state)
            if not isinstance(st.data.get("stat_data"), dict):
                return R.error("persona.import", "作品包的 state 缺 stat_data",
                               device=self.name, via=self.via)
        return R.success("persona.import", self.name, via=self.via, data={
            "name": card.get("name"),
            "version": pkg.get("version"),
            "exported_at": pkg.get("exported_at"),
            "world_book_entries": len(persona_book.entries_of(card)),
            "regex_rules": len(persona_regex.rules_of(card)),
            "state_keys": len((st.data.get("stat_data") if st else {}) or {}),
            "card": card,
            "state": st.to_dict() if st else None,
            "hint": "要真入库就交给 card.ingest（那一步过审批闸门）；本能力只解析校验",
        })
