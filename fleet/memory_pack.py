"""人格下放：把「人格」从权重里拆出来，做成可增量同步的外部记忆包。

主人在 2026-10-23 定的判断，本模块就是它的实现：
  权重里应该全是知识；情绪、记忆、口吻这些不该烧进参数。

三条理由（不是审美，是工程约束）：
  1) 增长性 — 记忆每天都在涨，权重不可能每天重训一次；
  2) 隔离性 — 重训会连带搅乱知识（灾难性遗忘），且没有回滚点；
  3) 并行性 — 同一份知识要能同时配多套人格；烧进权重就只剩一套。

于是：人格 = 记忆的子集 + 渲染规则。同一份 .gguf，换包就换人。

设计约束（决定了下面每条实现细节）：
  * 纯标准库。手机上是 Termux 的 python3，装不了第三方包；PC 与手机
    必须跑同一份代码，否则渲染结果会漂，人格就不一致了。
  * 内容寻址。分片 id 由「说什么」决定，不由「什么时候写」决定：
    同一句话被再提一次不该算新记忆，也不该让整包 digest 变。
  * 增量同步。手机内存和流量都紧张，只传缺的分片。
  * 可审计。每片都是明文 JSON，能 grep、能 diff、能单删、能回滚。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time

KINDS = ("identity", "value", "skill", "relation", "episodic")

# 分层优先级：identity/value 是「我是谁」，永远不裁；episodic 是「我记得什么」，
# 按预算竞争。数值只是相对权重，具体分配见 render()。
BASE_PRIORITY = {"identity": 100.0, "value": 80.0, "skill": 60.0,
                 "relation": 50.0, "episodic": 20.0}

# 这几层不允许被预算裁掉：宁可超预算，也不能让模型不知道自己在扮演谁
UNCUTTABLE = ("identity", "value")

LABELS = {"identity": "身份", "value": "原则", "skill": "技能",
          "relation": "关系", "episodic": "经历"}

_HSPACE = re.compile(r"[ \t\u3000]+")
_HBLANK = re.compile(r"\n{3,}")


# ---------------------------------------------------------------- 基础工具

def _canon(obj) -> str:
    """稳定序列化：同一份内容永远同一串字节，才能做内容寻址。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean(text) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    t = _HSPACE.sub(" ", t)
    return _HBLANK.sub("\n\n", t)


def _norm_tags(tags) -> list:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [x for x in re.split(r"[,，\s]+", tags) if x]
    out = []
    for t in tags:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return sorted(out)


def est_tokens(text: str) -> int:
    """粗估 token 数（不引第三方分词器）。

    中文按 1 字≈1 token（偏保守，实际多在 0.6~0.9），
    其余按 4 字符≈1 token。宁可高估：低估会撑爆上下文，
    高估只是少塞一条记忆。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff"
              or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    rest = len(text) - cjk
    return int(math.ceil(cjk * 1.0 + rest / 4.0))


# ---------------------------------------------------------------- 分片

def shard(kind, text, tags=(), weight=1.0, ts=None, source="") -> dict:
    """造一片记忆。kind 必须合法；text 空则抛 ValueError（空记忆没有意义）。"""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"未知记忆类型 {kind!r}（可用 {KINDS}）")
    body = _clean(text)
    if not body:
        raise ValueError("记忆内容为空")
    return {
        "id": "",                                  # 由 shard_id() 填，见下
        "kind": kind,
        "text": body,
        "tags": _norm_tags(tags),
        "weight": float(weight or 1.0),
        "ts": float(ts) if ts else None,
        "source": (source or "").strip(),
    }


def shard_id(s: dict) -> str:
    """分片指纹：只看 (kind, text, tags)。

    有意排除 ts 与 weight —— 同一条「主人喜欢结论前置」被写到第二次，
    是同一片记忆，不该变成两条把预算挤掉别的。更新口吻靠 weight，
    改内容才产生新 id（旧的留着，由 merge/渲染时按 weight 决胜负）。
    """
    return "ms-" + _sha16(_canon({
        "k": s.get("kind"), "t": s.get("text"), "g": sorted(s.get("tags") or [])}))


def _finalize(s: dict) -> dict:
    s = dict(s)
    s["id"] = s.get("id") or shard_id(s)
    s["tags"] = _norm_tags(s.get("tags"))
    s["weight"] = float(s.get("weight") or 1.0)
    s["ts"] = float(s["ts"]) if s.get("ts") else None
    return s


# ---------------------------------------------------------------- 记忆包

class Pack:
    """一个「人格」= 一堆分片。包身份是成员集合的指纹，不是版本的序号。"""

    def __init__(self, name="main", shards=(), meta=None):
        self.name = name or "main"
        self.meta = dict(meta or {})
        self.shards = {}
        for s in shards:
            self.add(s)

    # ---- 增删

    def add(self, s: dict):
        """加一片。返回 (id, 是否新片)。同 id 重复加时只提 weight，不新增。"""
        s = _finalize(s)
        old = self.shards.get(s["id"])
        if old:
            merged = dict(old)
            merged["weight"] = max(old["weight"], s["weight"])
            merged["ts"] = min(x for x in (old["ts"], s["ts"]) if x) if (
                old["ts"] or s["ts"]) else None
            self.shards[s["id"]] = merged
            return s["id"], False
        self.shards[s["id"]] = s
        return s["id"], True

    def remove(self, sid):
        return self.shards.pop(sid, None) is not None

    def __len__(self):
        return len(self.shards)

    # ---- 身份

    def digest(self) -> str:
        """包指纹：成员 id 排序后的哈希。加/删/换片都变，改 weight 不变。"""
        ids = sorted(self.shards)
        return "mem-" + _sha16(_canon({"v": 1, "ids": ids}))

    def manifest(self) -> dict:
        """给对端做增量用的清单：只有 id/kind/chars/tags，不带正文。"""
        rows = []
        for sid in sorted(self.shards):
            s = self.shards[sid]
            rows.append({"id": sid, "kind": s["kind"], "chars": len(s["text"]),
                         "tags": s["tags"], "ts": s["ts"]})
        return {"pack": self.digest() if self.shards else "mem-empty",
                "name": self.name, "count": len(rows), "shards": rows,
                "by_kind": self.by_kind()}

    def by_kind(self) -> dict:
        out = {}
        for s in self.shards.values():
            out[s["kind"]] = out.get(s["kind"], 0) + 1
        return out

    def diff(self, remote_manifest) -> dict:
        """算我该发什么、对端多什么。

        对端多出来的不算错误（它可能自己长出了记忆，比如手机上的对话），
        所以 separate 成 local_extra，交 caller 决定要不要回收。
        """
        remote_ids = set()
        for row in (remote_manifest or {}).get("shards") or []:
            rid = row.get("id")
            if rid:
                remote_ids.add(rid)
        local_ids = set(self.shards)
        need = sorted(local_ids - remote_ids)
        extra = sorted(remote_ids - local_ids)
        return {"need": need, "local_extra": extra,
                "in_sync": not need and not extra,
                "remote_pack": (remote_manifest or {}).get("pack", "")}

    # ---- 渲染

    def render(self, budget_tokens=1200, query="", now=None, header="") -> dict:
        """把记忆渲染成一段 system prompt 文本。

        排序分三段：
          1) identity/value 无条件先放（不可裁）；
          2) 剩下按 score 竞争 —— score = 层优先级 × weight × 时间衰减 × 相关度；
          3) 填到预算为止，超出的如实报告 dropped（不静默丢）。
        """
        now = float(now or time.time())
        qgrams = _grams(query) if (query or "").strip() else set()

        def score(s):
            base = BASE_PRIORITY.get(s["kind"], 10.0) * max(0.1, s["weight"])
            dec = 1.0
            if s["ts"]:
                days = max(0.0, (now - s["ts"]) / 86400.0)
                # 半衰期 30 天：两个月前的事还在，但排在近事后面
                dec = 0.5 ** (days / 30.0)
            rel = 0.0
            if qgrams:
                st = _grams(s["text"])
                rel = len(qgrams & st) / float(len(qgrams) or 1)
            return base * (0.5 + 1.5 * rel) * dec

        must = [s for s in self.shards.values() if s["kind"] in UNCUTTABLE]
        rest = [s for s in self.shards.values() if s["kind"] not in UNCUTTABLE]
        must.sort(key=lambda s: (-BASE_PRIORITY[s["kind"]], s["id"]))
        rest.sort(key=lambda s: (-score(s), s["id"]))

        head = header or f"以下是{self.name}的记忆。这些是你自己的经历与判断，不是外部资料。"
        lines = [head] if head else []
        used = est_tokens(head) if head else 0
        kept, dropped = [], []
        over = False

        for group, is_must in ((must, True), (rest, False)):
            cur_kind = None
            for s in group:
                line = self._line(s)
                cost = est_tokens(line) + 1
                if not is_must and used + cost > budget_tokens:
                    dropped.append(s)
                    continue
                if is_must and used + cost > budget_tokens:
                    over = True                    # 不可裁的塞爆了预算：如实标记
                if s["kind"] != cur_kind:
                    lines.append(f"[{LABELS.get(s['kind'], s['kind'])}]")
                    used += 3
                    cur_kind = s["kind"]
                lines.append(line)
                used += cost
                kept.append(s)

        text = "\n".join(lines)
        return {"text": text, "pack": self.digest() if self.shards else "mem-empty",
                "name": self.name, "tokens": used, "budget": budget_tokens,
                "kept": len(kept), "dropped": len(dropped),
                "over_budget": over,
                "by_kind": {k: sum(1 for s in kept if s["kind"] == k) for k in KINDS}}

    @staticmethod
    def _line(s) -> str:
        tag = f"（{'/'.join(s['tags'])}）" if s["tags"] else ""
        when = ""
        if s["kind"] == "episodic" and s["ts"]:
            when = time.strftime("%Y-%m-%d ", time.localtime(s["ts"]))
        return f"- {when}{s['text']}{tag}"

    # ---- 序列化

    def to_dict(self) -> dict:
        return {"v": 1, "name": self.name, "meta": self.meta,
                "pack": self.digest() if self.shards else "mem-empty",
                "count": len(self.shards),
                "shards": [self.shards[k] for k in sorted(self.shards)]}

    @classmethod
    def from_dict(cls, d) -> "Pack":
        p = cls(name=(d or {}).get("name") or "main", meta=(d or {}).get("meta") or {})
        for s in (d or {}).get("shards") or []:
            try:
                p.add(s)
            except (ValueError, TypeError):
                continue        # 坏片跳过而不是整包失败：记忆是长期资产，容错优先
        return p

    def save(self, root) -> dict:
        """落盘：pack.json（含正文）+ shards/<id>.json（单片可 diff/可删）。

        两份都写是有意的：pack.json 方便整包拷贝，分片目录方便增量与审计。
        """
        os.makedirs(root, exist_ok=True)
        sdir = os.path.join(root, "shards")
        os.makedirs(sdir, exist_ok=True)
        for sid, s in self.shards.items():
            with open(os.path.join(sdir, sid + ".json"), "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        d = self.to_dict()
        with open(os.path.join(root, "pack.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return {"root": root, "pack": d["pack"], "count": d["count"]}

    @classmethod
    def load(cls, root) -> "Pack":
        path = os.path.join(root, "pack.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        p = cls(name=os.path.basename(root) or "main")
        sdir = os.path.join(root, "shards")
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(sdir, fn), encoding="utf-8") as f:
                        p.add(json.load(f))
                except (OSError, ValueError, TypeError):
                    continue
        return p


# ---------------------------------------------------------------- 相关度

def _grams(text, n=3) -> set:
    """字符 n-gram 集合。中文不分词也能用，代价是短文本区分度低，
    但记忆条目普遍是整句，够用且零依赖（与 memory_store 同套路）。"""
    t = re.sub(r"\s+", "", (text or "").lower())
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def merge(a: Pack, b: Pack) -> Pack:
    """合并两包（双向同步用）。同 id 取 weight 大者；不同 id 全留。

    记忆允许互相矛盾 —— 「主人说要 A」和「主人后来说不要 A」都是真发生过的事，
    该由 ts 与 weight 在渲染时决胜负，而不是在合并时把一条抹掉。
    """
    out = Pack(name=a.name or b.name, meta={**a.meta, **b.meta})
    for s in list(a.shards.values()) + list(b.shards.values()):
        out.add(s)
    return out


# ---------------------------------------------------------------- 外部数据接入

_CARD_FIELDS = (("name", "identity"), ("description", "identity"),
                ("personality", "value"), ("scenario", "identity"),
                ("system_prompt", "value"), ("mes_example", "skill"))


def from_card(card: dict, source="persona_card") -> Pack:
    """把角色卡（SillyTavern 系 v2/v3）拆成 identity/value/skill 分片。

    为什么不用整段 system_prompt 一把塞进去：拆开才能单独改。
    「口吻变冷一点」只动 value 层，不用重写整张卡。
    """
    d = card or {}
    if isinstance(d.get("data"), dict):
        d = d["data"]
    name = _clean(d.get("name")) or "persona"
    p = Pack(name=name, meta={"origin": source})
    for key, kind in _CARD_FIELDS:
        body = _clean(d.get(key))
        if not body:
            continue
        for para in [x.strip() for x in body.split("\n") if x.strip()]:
            try:
                p.add(shard(kind, para, tags=(key,), weight=3.0 if kind == "identity" else 2.0,
                            source=source))
            except ValueError:
                continue
    for i, alt in enumerate(d.get("alternate_greetings") or []):
        body = _clean(alt)
        if body:
            try:
                p.add(shard("skill", body, tags=("greeting", f"alt{i}"),
                            weight=0.5, source=source))
            except ValueError:
                pass
    return p


def from_memory_store(store, limit=500, default_kind="episodic") -> Pack:
    """把 fleet 记忆库（SQLite）里的历史对话转成记忆包。

    role 是 user 的记成 relation（"主人说过什么"），assistant 的记成
    identity（"我说过什么"）—— 这条区分很关键：前者塑造对主人的认知，
    后者塑造自我一致性，混成一锅会让模型把自己的话当成主人的要求。
    """
    p = Pack(name="main", meta={"origin": "memory_store"})
    try:
        items = store.export_all() or []
    except Exception:
        return p
    for it in items[-limit:]:
        body = _clean(it.get("content"))
        if not body:
            continue
        role = (it.get("role") or "user").lower()
        kind = "relation" if role == "user" else "identity"
        if kind not in KINDS:
            kind = default_kind
        tags = [t for t in _norm_tags(it.get("tags"))]
        try:
            p.add(shard(kind, body, tags=tags or (role,), weight=1.0,
                        ts=it.get("ts"), source="memory_store"))
        except ValueError:
            continue
    return p
