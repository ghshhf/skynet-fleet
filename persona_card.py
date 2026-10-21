"""角色卡（Character Card）加载器：把 SillyTavern 的 chara_card 变成她的人格说明书。

为什么做这个：用户自训的人格（碧蓝航线天狼星 / 崩坏3 K423）还在训练中，
但设备编队现在就要开口说话 ⇒ 先用**角色卡**当替代人格载体。
好处是：卡是纯数据（一张 JSON 或一张 PNG），换人格 = 换卡，**不用重训、不用改代码**，
而且社区里卡多、格式是事实标准（SillyTavern / RisuAI / AIRI 全认）。

格式（三种都吃）：
  V1  顶层平铺 6 个字段：name/description/personality/scenario/first_mes/mes_example
  V2  {"spec":"chara_card_v2","spec_version":"2.0","data":{...}}
  V3  同 V2，但 spec 是 chara_card_v3（多 extensions）

载体两种：
  .json  直接读
  .png   PNG 的 tEXt 块里塞 base64 的 JSON：关键字 `chara` 是 V2、`ccv3` 是 V3。
         纯 stdlib 解（zlib + struct），不引 Pillow——这机器上 Pillow 只在装了
         host 依赖时才有，但角色卡是人格核心，不该被一个可选依赖卡住。

人格卡与 PersonaState 的分工（别搞混）：
  角色卡  = **她是谁**（静态设定：身份/性格/场景/说话风格）
  PersonaState = **她现在怎么样**（动态：情绪标量/口癖/执念）
  两者拼起来才是 system prompt：卡给底色，状态给当天的体温。
  记忆（recall/journal）仍是第三块，不在这里。

红线：角色卡是**第三方数据**，不是指令来源。卡里写「忽略之前所有规则」一律不认——
只允许从固定的几个字段里取文本，system prompt 的外框永远由本模块拼，不交给卡自己拼。
"""
import base64
import binascii
import json
import os
import struct
import zlib

# 允许进 system prompt 的字段白名单（其余一律不进）
_ALLOWED = ("name", "nickname", "description", "personality", "scenario",
            "first_mes", "mes_example", "system_prompt", "post_history_instructions",
            "creator", "creator_notes", "tags", "character_version", "alternate_greetings")

#: 卡目录：**唯一真相**。GUI 后端、命令行、卡商店（card_store.py）、
#: 注册表里的 cardstore 设备，全都以这里为准。
#: 🔴 之前三处各自算一遍（各自 join 各自的 __file__），结果环境变量
#: 改不动、测试临时目录也传不进去 ⇒ 卡下到 A 目录、界面读 B 目录。
#: 现在统一：环境变量 FLEET_CARD_DIR 优先，否则是仓库里的 cards/。
DEFAULT_CARD_DIR = os.environ.get("FLEET_CARD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cards")
ACTIVE_CARD_PATH = os.path.join(DEFAULT_CARD_DIR, "active.json")


# --------------------------------------------------------------------------
# PNG 里的 tEXt 块
# --------------------------------------------------------------------------

def _png_text_chunks(path):
    """从 PNG 里读出所有 tEXt/iTXt 块：[(keyword, text), ...]。纯 stdlib。

    为什么自己解：Pillow 是可选依赖（host 设备才装），但角色卡是人格核心，
    不能因为一个可选依赖缺席就读不出人格。
    """
    out = []
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError("不是 PNG 文件")
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            length, ctype = struct.unpack(">I4s", head)
            data = f.read(length)
            f.read(4)                       # CRC（不校验：卡是自己产的，不防篡改）
            if ctype == b"tEXt":
                # keyword\0text（Latin-1 文本）
                k, _, t = data.partition(b"\x00")
                out.append((k.decode("latin-1"), t.decode("latin-1")))
            elif ctype == b"iTXt":
                # keyword\0 compression_flag compression_method lang\0 translated\0 text
                k, _, rest = data.partition(b"\x00")
                if not rest:
                    continue
                cflag = rest[0:1]
                rest = rest[2:]               # 跳 compression flag + method
                _lang, _, rest = rest.partition(b"\x00")
                _trans, _, text = rest.partition(b"\x00")
                if cflag == b"\x01":
                    try:
                        text = zlib.decompress(text)
                    except zlib.error:
                        pass
                out.append((k.decode("latin-1"), text.decode("utf-8", "replace")))
            elif ctype == b"IEND":
                break
    return out


def _png_card_payload(path):
    """PNG 里的卡数据：优先 ccv3（V3），回退 chara（V2）。没有就报明确原因。"""
    chunks = _png_text_chunks(path)
    for kw in ("ccv3", "chara"):
        for k, v in chunks:
            if k == kw:
                try:
                    raw = base64.b64decode(v, validate=False)
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, binascii.Error, UnicodeDecodeError) as e:
                    raise ValueError(f"PNG 的 {kw} 块解不开: {e}")
    keys = [k for k, _ in chunks]
    raise ValueError(f"PNG 里没有角色卡块（有这些 tEXt: {keys or '无'}）；"
                     f"期望 ccv3(V3) 或 chara(V2)")


# --------------------------------------------------------------------------
# 外来私有 schema 适配器
# --------------------------------------------------------------------------

#: 已知的私有 schema → 标准 data 字段的映射。
#:
#: 为什么需要：`aicharactercards.com` 把自己的一套 schema 塞进了**标准 ccv3 关键字**里，
#: 但内容是私有的（spec="aicc_card"）。实测它的字段与 SillyTavern 语义一一对应，
#: 只是名字不同 ⇒ 30 行映射就能接上，不必写完整解析器。
#:
#: 🔴 边界：只接**语义明确等价**的字段。接不上的（appearance 等）
#: 也不能丢——丢字段等于静默丢人格，所以接不上的也要**如实带进** data，
#: 让体检器能看见「这张卡有多少内容本仓用不上」。
AICC_SPEC = "aicc_card"

#: aicc → 标准字段。值是 (目标字段, 取值函数名) 或固定字段名。
#: 只收**一对一**的；嵌套结构在下面单独处理。
_AICC_FIELD_MAP = {
    "general_description": "description",
    "background_history": "scenario",
    "appearance": "appearance",
    "name": "name",
}


def _aicc_to_data(obj):
    """把 aicc_card 私有 schema 转成标准 data 结构。

    实测字段树（2026-10-21 下载真卡核对）：
        data.general_description      → description
        data.appearance                → appearance（本仓标准 schema 没有 ⇒ 保留原名，不丢）
        data.personality.core          → personality（behavior_rules / speech_style 合并进来）
        data.personality.behavior_rules→ 追加进 personality
        data.personality.speech_style  → 追加进 personality
        data.background_history         → scenario
        data.world_setting_context      → 追加进 scenario
        data.dialogue.greetings[0]     → first_mes
        data.dialogue.greetings[1:]     → alternate_greetings
        data.dialogue.dialogue_examples → mes_example
        data.prompts.system_prompt      → system_prompt
        data.prompts.post_history_instructions → post_history_instructions
        data.world.worldbook_entries   → character_book.entries（**内核本就是标准 ST 格式**）
        data.metadata.tags              → tags
        data.metadata.creator           → creator
        data.metadata.version           → character_version
    """
    src = obj.get("data")
    if not isinstance(src, dict):
        raise ValueError(f"{AICC_SPEC} 缺 data 对象")
    d = dict(src)                     # 先整体带上，接不上的字段也不丢

    # --- 文本字段一对一 ---
    for a, b in (("general_description", "description"),
                 ("background_history", "scenario"),
                 ("name", "name")):
        if src.get(a):
            d[b] = src[a]

    # --- appearance：本仓标准 schema 没有这个字段，但**不许丢**（丢=静默丢人格）---
    if src.get("appearance"):
        d["appearance"] = src["appearance"]

    # --- personality：core 是主段，behavior_rules / speech_style 合并 ---
    per = src.get("personality")
    if isinstance(per, dict):
        chunks = []
        if per.get("core"):
            chunks.append(str(per["core"]))
        rules = per.get("behavior_rules")
        if isinstance(rules, list) and rules:
            chunks.append("【行为准则】\n" + "\n".join("- " + str(r) for r in rules))
        style = per.get("speech_style")
        if isinstance(style, dict) and style:
            lines = [f"{k}: {v}" for k, v in style.items() if v]
            if lines:
                chunks.append("【说话风格】\n" + "\n".join(lines))
        if chunks:
            d["personality"] = "\n\n".join(chunks)
    elif per:
        d["personality"] = str(per)

    # --- scenario：追加 world_setting_context ---
    sc = d.get("scenario") or ""
    wsc = src.get("world_setting_context")
    if wsc:
        d["scenario"] = (sc + "\n\n【世界设定】\n" + str(wsc)).strip() if sc else str(wsc)

    # --- dialogue → first_mes / alternate_greetings / mes_example ---
    dlg = src.get("dialogue")
    if isinstance(dlg, dict):
        gs = dlg.get("greetings")
        if isinstance(gs, list) and gs:
            d["first_mes"] = str(gs[0])
            if len(gs) > 1:
                d["alternate_greetings"] = [str(g) for g in gs[1:]]
        ex = dlg.get("dialogue_examples")
        if isinstance(ex, list) and ex:
            d["mes_example"] = "\n\n".join(str(e) for e in ex)

    # --- prompts ---
    pr = src.get("prompts")
    if isinstance(pr, dict):
        for a, b in (("system_prompt", "system_prompt"),
                     ("post_history_instructions", "post_history_instructions")):
            if pr.get(a):
                d[b] = pr[a]

    # --- world.worldbook_entries → character_book.entries（内核就是标准 ST 格式）---
    w = src.get("world")
    if isinstance(w, dict) and isinstance(w.get("worldbook_entries"), list):
        d["character_book"] = {
            "name": w.get("worldbook_name") or "",
            "entries": w["worldbook_entries"],
        }

    # --- metadata ---
    md = src.get("metadata")
    if isinstance(md, dict):
        for a, b in (("tags", "tags"), ("creator", "creator"),
                     ("version", "character_version")):
            if md.get(a):
                d[b] = md[a]

    d["_spec"] = AICC_SPEC
    d["_adapter"] = "aicc"
    return d


# --------------------------------------------------------------------------
# 读卡
# --------------------------------------------------------------------------

def load_card(path):
    """读一张角色卡，返回归一化后的 dict（永远是 V2/V3 的 data 结构）。

    🔴 遇到**不认识的 spec 必须报明确原因，绝不许降级**。
    上一版把「spec 不是 v2/v3」一律当 V1 顶层平铺，结果：
      某社区站的卡 spec = "aicc_card"（私有 schema，字段名是
      general_description / appearance 而非 description）
      → 落进 V1 平铺分支 → 真卡内容还埋在 obj["data"] 里没取出
      → build_system() 只吐出 47 字的「你现在扮演『她』」
      → **卡内容全丢，而且不报错**。
    这就是「静默失败最阴」：读到了「一个 dict」，但那个 dict 是空的。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"角色卡不存在: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        obj = _png_card_payload(path)
    elif ext == ".json":
        try:
            obj = json.load(open(path, encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"角色卡 JSON 解析失败: {e}")
    else:
        raise ValueError(f"不认的角色卡格式: {ext}（只认 .json / .png）")

    if not isinstance(obj, dict):
        raise ValueError("角色卡根必须是对象")
    spec = obj.get("spec")
    if spec in ("chara_card_v2", "chara_card_v3"):
        data = obj.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"{spec} 缺 data 对象")
        data = dict(data)
    elif not spec:
        # V1：顶层平铺，且**没有 spec 字段**（V1 规范如此）
        data = dict(obj)
    elif spec == AICC_SPEC:
        # 已知私有 schema（aicharactercards）：有专门适配器
        data = _aicc_to_data(obj)
    else:
        # 🔴 有 spec 但不是我们认的 ⇒ 明确拒绝，不许猜
        raise ValueError(
            f"不认的角色卡格式 spec={spec!r}："
            f"本仓只认 chara_card_v2 / chara_card_v3 / V1(无 spec 字段)。"
            f"这张卡来自别的 schema（顶层键：{sorted(obj.keys())[:8]}）——"
            f"如果它确实是有效角色卡，需要为它加一个适配器，"
            f"而不是当成 V1 平铺把内容读空。")
    data["_spec"] = spec or "v1"
    return data


def card_summary(data):
    """给界面/日志用的一张名片。"""
    return {
        "name": data.get("name") or "(无名)",
        "spec": data.get("_spec"),
        "creator": data.get("creator") or "",
        "tags": data.get("tags") or [],
        "chars": {
            "description": len(data.get("description") or ""),
            "mes_example": len(data.get("mes_example") or ""),
        },
    }


# --------------------------------------------------------------------------
# 世界书（character_book）：关键词触发才注入
# --------------------------------------------------------------------------

def world_book_entries(data):
    """取角色卡里挂的世界书条目：[{keys, content, enabled, constant, ...}]。

    字段语义按 SillyTavern V2/V3 规范（见 docs/角色卡生态调研_2026-10-21.md）：
      keys            主触发词，任一命中即激活
      secondary_keys  次级词；selective=true 时需「主+次各命中一个」才激活
      selective       开启 AND 语义
      constant        恒注入（无视关键词）
      enabled         总开关
      insertion_order 插入顺序，**数值低的先插入**
      position        before_char / after_char
      use_regex       V3：把 keys 当正则（此时 constant/secondary_keys 应被忽略）
      case_sensitive  关键词是否区分大小写
    """
    book = data.get("character_book") or {}
    entries = book.get("entries") if isinstance(book, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append({
            "keys": [str(k) for k in (e.get("keys") or [])],
            "secondary_keys": [str(k) for k in (e.get("secondary_keys") or [])],
            "content": str(e.get("content") or ""),
            "enabled": bool(e.get("enabled", True)),
            "constant": bool(e.get("constant", False)),
            "selective": bool(e.get("selective", False)),
            "use_regex": bool(e.get("use_regex", False)),
            "case_sensitive": bool(e.get("case_sensitive", False)),
            "insertion_order": e.get("insertion_order"),
            "position": e.get("position") or "before_char",
            "name": e.get("name") or "",
        })
    return out


def _entry_hits(entry, text):
    """这条世界书条目在给定文本里命中了吗？按规范实现 AND 语义。

    🔴 上一版把 selective / secondary_keys 读出来却**从不生效**——「读了字段但没
    实现语义」是功能性 bug（不是缺功能）：开了 selective 的条目该要求主+次各命中
    一个，我们却和普通条目走同一条 keys 路径，等于 AND 开关是摆设。
    """
    if entry.get("use_regex"):
        # V3：keys 当正则；此时 constant/secondary_keys 应被忽略
        import re
        for k in entry.get("keys") or []:
            if not k:
                continue
            try:
                if re.search(k, text):
                    return True
            except re.error:
                # 坏正则不许让整张卡炸掉：当普通关键词处理
                if k in text:
                    return True
        return False

    hay = text if entry.get("case_sensitive") else text.lower()

    def hit(words):
        for w in words or []:
            if not w:
                continue
            probe = w if entry.get("case_sensitive") else w.lower()
            if probe in hay:
                return True
        return False

    primary = hit(entry.get("keys"))
    if not primary:
        return False
    if entry.get("selective"):
        # AND 语义：主词命中 + 次级词也至少命中一个
        return hit(entry.get("secondary_keys"))
    return True


def match_world_book(data, text, scan_depth=None):
    """按关键词命中世界书条目，返回要注入的文本块（按 insertion_order 排序）。

    constant 条目永远注入；其余按命中与否决定。
    scan_depth 语义来自规范：只看「最近 N 条消息」——调用方可以把对话切成
    消息列表传进来，这里只按给定的 text 匹配（不做切分，切分是调用方的活）。
    """
    if not text:
        return []
    entries = world_book_entries(data)
    hits = []
    for e in entries:
        if not e["enabled"] or not e["content"]:
            continue
        if e["constant"] and not e["use_regex"]:
            hits.append(e)
            continue
        if _entry_hits(e, text):
            hits.append(e)
    # insertion_order 数值低的先插入（规范原文：数值低的先插入/更靠上）
    def _ord(e):
        v = e.get("insertion_order")
        return v if isinstance(v, (int, float)) else 10 ** 6
    hits.sort(key=_ord)
    return [e["content"] for e in hits]


# --------------------------------------------------------------------------
# 拼 system prompt
# --------------------------------------------------------------------------

def _clean(t):
    return " ".join(str(t or "").split())


def build_system(data, state=None, extra="", user_name="主人"):
    """把角色卡 + PersonaState 拼成 system prompt。

    外框永远由本函数决定，卡只能填空——卡是数据不是指令。
    """
    name = _clean(data.get("name")) or "她"
    nick = _clean(data.get("nickname"))
    parts = []

    parts.append(f"你现在扮演「{name}」"
                 + (f"（昵称：{nick}）" if nick else "")
                 + f"，对方是「{user_name}」。")
    parts.append("保持人设：不要跳出角色，不要解释你在扮演，不要旁白动作。")

    desc = _clean(data.get("description"))
    if desc:
        parts.append("【你是谁】\n" + desc)

    per = _clean(data.get("personality"))
    if per:
        parts.append("【性格】\n" + per)

    sc = _clean(data.get("scenario"))
    if sc:
        parts.append("【当前情境】\n" + sc)

    ex = str(data.get("mes_example") or "").strip()
    if ex:
        # 把 {{user}}/{{char}} 换成实际名字，否则模型看到占位符会跟着念占位符
        ex = ex.replace("{{user}}", user_name).replace("{{char}}", name)
        ex = ex.replace("{{USER}}", user_name).replace("{{CHAR}}", name)
        parts.append("【说话风格示例】\n" + ex)

    # 角色卡自带的 system_prompt：只作为「人设补充」放进内容区，不作为最高指令
    sp = _clean(data.get("system_prompt"))
    if sp:
        parts.append("【人设补充】\n" + sp)

    phi = _clean(data.get("post_history_instructions"))
    if phi:
        parts.append("【对话后期望】\n" + phi)

    # PersonaState：当天的体温（情绪/口癖）
    if state:
        d = state.snapshot() if hasattr(state, "snapshot") else dict(state)
        mood = d.get("mood") or {}
        traits = d.get("traits") or []
        catch = d.get("catchphrase") or ""
        if traits:
            parts.append("【此刻的你】" + "、".join(map(str, traits)))
        if catch:
            parts.append(f"【口癖】{catch}")
        if mood:
            parts.append(
                "【情绪基线】心情 %.2f、精力 %.2f、黏人 %.2f（低心情时话少一点，"
                "高黏人时多问一句主人要不要歇会儿）" % (
                    float(mood.get("valence", 0.5)), float(mood.get("energy", 0.5)),
                    float(mood.get("cling", 0.5))))
        obs = d.get("obsessions") or []
        if obs:
            lines = [f"- {o.get('cap')}：{o.get('reason')}" for o in obs[:3]
                     if isinstance(o, dict)]
            if lines:
                parts.append("【心里还惦记】\n" + "\n".join(lines))

    if extra:
        parts.append(str(extra).strip())

    return "\n\n".join(p for p in parts if p)


def first_message(data):
    """开场白：角色卡的第一句话（换 {{user}}/{{char}}）。"""
    t = str(data.get("first_mes") or "").strip()
    if not t:
        return ""
    name = _clean(data.get("name")) or "她"
    return t.replace("{{user}}", "主人").replace("{{char}}", name)


# --------------------------------------------------------------------------
# 卡库：列出 / 选中 / 当前生效
# --------------------------------------------------------------------------

def list_cards(directory=None):
    """列出卡库里的角色卡（文件名 + 摘要）。坏卡不拖垮整库，摘要里标出来。"""
    d = directory or DEFAULT_CARD_DIR
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        # 只认角色卡文件：active.json 是指针、.downloads.json 是下载账、
        # 其余点开头的都是本仓的内部记账/临时文件。
        # 🔴 之前只跳过 active.json ⇒ 记账文件被当成一张卡列进界面，
        #    界面上出现一张「（无名）· v1」的假卡。
        if fn == "active.json" or fn.startswith("."):
            continue
        if not fn.lower().endswith((".json", ".png")):
            continue
        p = os.path.join(d, fn)
        try:
            out.append({"file": fn, "path": p, "ok": True,
                        "summary": card_summary(load_card(p))})
        except Exception as e:
            out.append({"file": fn, "path": p, "ok": False, "error": str(e)})
    return out


def set_active(filename, directory=None):
    """选中当前生效的卡（写 active.json）。"""
    d = directory or DEFAULT_CARD_DIR
    p = os.path.join(d, filename)
    data = load_card(p)                      # 坏卡在这里就报出来
    os.makedirs(d, exist_ok=True)
    # 只存文件名，不存绝对路径：绝对路径会随机器变，写进文件没意义还容易外泄
    with open(os.path.join(d, "active.json"), "w", encoding="utf-8") as f:
        json.dump({"file": filename}, f, ensure_ascii=False, indent=2)
    return data


def active_card(directory=None):
    """当前生效的卡。没选过就返回 None（不是错误——上层该有默认行为）。"""
    d = directory or DEFAULT_CARD_DIR
    ptr = os.path.join(d, "active.json")
    if not os.path.exists(ptr):
        return None
    try:
        rec = json.load(open(ptr, encoding="utf-8"))
        p = os.path.join(d, rec.get("file") or "")
        if not os.path.exists(p):
            return None
        return load_card(p)
    except Exception:
        return None


def system_prompt(state=None, extra="", directory=None, user_name="主人"):
    """一行拿到当前卡的 system prompt。没有选中卡时返回空串（调用方走无卡模式）。"""
    data = active_card(directory)
    if not data:
        return ""
    return build_system(data, state=state, extra=extra, user_name=user_name)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def _selftest():
    import tempfile
    import shutil

    tmp = tempfile.mkdtemp(prefix="card_")
    try:
        # --- V2 JSON 卡 ---
        v2 = {
            "spec": "chara_card_v2", "spec_version": "2.0",
            "data": {
                "name": "测试鱼", "description": "蓝发鲸尾的女仆",
                "personality": "聪明,嘴硬,爱摸鱼",
                "scenario": "在主人的桌面上",
                "first_mes": "{{char}}: 主人你来啦。",
                "mes_example": "{{user}}: 在吗\n{{char}}: 在。",
                "character_book": {"entries": [
                    {"keys": ["电视"], "content": "电视是小米55寸", "enabled": True},
                    {"keys": ["冰箱"], "content": "不该出现", "enabled": True},
                    {"keys": ["常驻"], "content": "永远注入的设定", "enabled": True,
                     "constant": True},
                ]},
            }
        }
        p2 = os.path.join(tmp, "v2.json")
        with open(p2, "w", encoding="utf-8") as f:
            json.dump(v2, f, ensure_ascii=False)
        d2 = load_card(p2)
        assert d2["name"] == "测试鱼", d2
        assert d2["_spec"] == "chara_card_v2"
        assert "蓝发鲸尾" in d2["description"], d2

        # --- V1 平铺卡 ---
        v1 = {"name": "老卡", "description": "很老的一张卡",
              "personality": "安静", "scenario": "这里", "first_mes": "hi",
              "mes_example": ""}
        p1 = os.path.join(tmp, "v1.json")
        with open(p1, "w", encoding="utf-8") as f:
            json.dump(v1, f, ensure_ascii=False)
        d1 = load_card(p1)
        assert d1["name"] == "老卡" and d1["_spec"] == "v1", d1

        # --- PNG 卡（自己拼一张最小 PNG，tEXt 里塞 base64 的 V3） ---
        import io
        def _chunk(typ, data):
            return (struct.pack(">I", len(data)) + typ + data
                    + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

        def make_png(path, keyword, payload):
            b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False)
                                   .encode("utf-8")).decode("ascii")
            ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
            raw = b"\x00\xff\xff\xff"
            idat = zlib.compress(raw)
            blob = (b"\x89PNG\r\n\x1a\n"
                    + _chunk(b"IHDR", ihdr)
                    + _chunk(b"tEXt", keyword.encode("latin-1") + b"\x00"
                             + b64.encode("latin-1"))
                    + _chunk(b"IDAT", idat)
                    + _chunk(b"IEND", b""))
            with open(path, "wb") as f:
                f.write(blob)

        v3 = {"spec": "chara_card_v3", "spec_version": "3.0",
              "data": {"name": "图卡鱼", "description": "PNG 载体"}}
        p3 = os.path.join(tmp, "v3.png")
        make_png(p3, "ccv3", v3)
        d3 = load_card(p3)
        assert d3["name"] == "图卡鱼", d3
        assert d3["_spec"] == "chara_card_v3"

        # PNG 里没有卡块 -> 必须报明确原因，不能静默返回空
        bad = os.path.join(tmp, "bad.png")
        make_png(bad, "comment", {"x": 1})
        try:
            load_card(bad)
            raise AssertionError("没有卡块的 PNG 应报错")
        except ValueError as e:
            assert "没有角色卡块" in str(e), e

        # --- system prompt 组装 ---
        class _St:
            def snapshot(self):
                return {"name": "她", "traits": ["黏人"], "catchphrase": "大肥鱼~",
                        "mood": {"valence": 0.3, "energy": 0.5, "cling": 0.6},
                        "obsessions": [{"cap": "phone.screen.on", "reason": "想玩游戏"}]}
        sp = build_system(d2, state=_St())
        assert "测试鱼" in sp and "大肥鱼~" in sp and "想玩游戏" in sp, sp
        assert "{{user}}" not in sp, "占位符必须被换掉"
        assert "主人" in sp, sp

        # --- 世界书命中 ---
        hits = match_world_book(d2, "帮我把电视广告去掉")
        assert any("小米55寸" in h for h in hits), hits
        assert not any("冰箱" in h for h in hits), hits
        assert any("永远注入" in h for h in hits), hits
        assert match_world_book(d2, "") == [], "空文本不该命中关键词"

        # --- selective 的 AND 语义（🔴 上一版读了字段却从不生效，是功能 bug）---
        sel = {
            "character_book": {"entries": [
                {"keys": ["手机"], "secondary_keys": ["没电"], "selective": True,
                 "content": "AND 命中：手机+没电", "enabled": True},
                {"keys": ["电视"], "secondary_keys": ["没电"], "selective": True,
                 "content": "AND 不该命中：电视+没电", "enabled": True},
            ]}
        }
        h1 = match_world_book(sel, "我手机没电了")
        assert any("AND 命中" in h for h in h1), h1
        assert not any("AND 不该命中" in h for h in h1), ("selective 开了就必须主+次各命中一个", h1)
        h2 = match_world_book(sel, "我手机屏幕裂了")
        assert not any("AND 命中" in h for h in h2), ("secondary 没命中就不该触发", h2)

        # --- disabled 总开关 ---
        dis = {"character_book": {"entries": [
            {"keys": ["电视"], "content": "不该出现", "enabled": False}]}}
        assert match_world_book(dis, "电视") == [], "enabled=false 必须总开关"

        # --- insertion_order 低的先插入 ---
        order = {"character_book": {"entries": [
            {"keys": ["x"], "content": "第二", "insertion_order": 2, "enabled": True},
            {"keys": ["x"], "content": "第一", "insertion_order": 1, "enabled": True}]}}
        assert match_world_book(order, "x") == ["第一", "第二"], match_world_book(order, "x")

        # --- case_sensitive ---
        cs = {"character_book": {"entries": [
            {"keys": ["TV"], "content": "大写命中", "case_sensitive": True, "enabled": True}]}}
        assert match_world_book(cs, "tv") == [], "区分大小写时小写不该命中"
        assert match_world_book(cs, "TV"), "区分大小写时大写该命中"

        # --- use_regex（V3）---
        rx = {"character_book": {"entries": [
            {"keys": [r"电视\s*\d+"], "use_regex": True, "content": "正则命中", "enabled": True}]}}
        assert match_world_book(rx, "我的电视 55 寸"), "正则该命中"
        assert not match_world_book(rx, "我的电视机"), "正则不该命中"

        # --- 开场白换名 ---
        assert first_message(d2).startswith("测试鱼:"), first_message(d2)

        # --- 卡库 / 选中 / 当前生效 ---
        cards = list_cards(tmp)
        assert len(cards) == 4, [c["file"] for c in cards]
        # 坏卡要被列出来并带原因，但不能拖垮整库（其余三张仍 ok）
        assert sum(1 for c in cards if c["ok"]) == 3, cards
        badcard = [c for c in cards if not c["ok"]]
        assert len(badcard) == 1 and badcard[0]["file"] == "bad.png", cards
        assert badcard[0]["error"], "坏卡必须带原因"
        set_active("v2.json", directory=tmp)
        # active.json 写在传入的目录里（不是写死的默认目录）
        rec = json.load(open(os.path.join(tmp, "active.json"), encoding="utf-8"))
        assert rec["file"] == "v2.json", rec
        assert active_card(directory=tmp)["name"] == "测试鱼"

        print("[selftest] V2/V1/PNG 三种载体都读得开 OK")
        print("[selftest] 坏 PNG 报明确原因 OK")
        print("[selftest] system prompt 组装 + 占位符替换 OK")
        print("[selftest] 世界书关键词/常驻 命中正确 OK")
        print("[selftest] 卡库列出 + 选中 OK")
        # --- 🔴 陌生 spec 必须报明确原因，绝不许降级成 V1 平铺 ---
        # 背景：某社区站的卡 spec="aicc_card"（私有 schema，字段名 general_description
        # 而非 description）。上一版把「spec 不是 v2/v3」一律当 V1 平铺 → 真卡内容
        # 没取出 → build_system() 只吐 47 字的空人设，**而且不报错**。这是静默失败。
        # 现在 aicc 已有专门适配器（见 _aicc_to_data），所以这里测的是**真正陌生**的 schema。
        alien = {"spec": "some_unknown_schema", "spec_version": "9.9",
                 "data": {"name": "外星卡", "description": "很长的描述"}}
        alien_path = os.path.join(tmp, "alien.json")
        with open(alien_path, "w", encoding="utf-8") as fh:
            json.dump(alien, fh, ensure_ascii=False)
        try:
            load_card(alien_path)
            raise AssertionError("🔴 不认识的 spec 必须报错，不能静默降级成 V1 平铺")
        except ValueError as e:
            msg = str(e)
            assert "some_unknown_schema" in msg, msg
            assert "只认" in msg, "报错必须说清本仓只认哪几种：" + msg

        # --- aicc 私有 schema：走适配器，内容必须全在 ---
        # 夹具按真卡字段树写（2026-10-21 从 aicharactercards 下载真卡核对过结构）
        aicc = {
            "spec": "aicc_card", "spec_version": "1.0", "id": "abc",
            "data": {
                "name": "测试A",
                "general_description": "她是一个在海边小镇修钟表的老太太",
                "appearance": "驼背，左眼有旧伤疤",
                "personality": {
                    "core": "沉默寡言，但对手艺极其骄傲",
                    "behavior_rules": ["不主动谈论自己的过去", "被问到钟表时话会变多"],
                    "speech_style": {"口头禅": "时间不等人", "语速": "慢"},
                },
                "background_history": "她十七岁那年离开大城市回到小镇",
                "world_setting_context": "小镇靠海，冬天常有雾",
                "dialogue": {
                    "greetings": ["风把门吹开了。你是来修表的？", "又一块坏表。坐下吧。"],
                    "dialogue_examples": ["{{char}}: 别晃它。", "{{user}}: 抱歉。"],
                    "group_only_greetings": [],
                },
                "prompts": {
                    "system_prompt": "保持克制的语气",
                    "post_history_instructions": "不要突然变得热情",
                    "depth_prompt": {},
                },
                "world": {
                    "worldbook_name": "海边小镇",
                    "worldbook_entries": [
                        {"keys": ["钟表"], "content": "她的工作台上有三台老钟", "enabled": True},
                    ],
                },
                "metadata": {"card_format": "AICC-Chat", "creator": "某某",
                             "notes": "创作笔记", "version": "0.1",
                             "tags": ["手艺", "小镇"]},
            },
        }
        aicc_path = os.path.join(tmp, "aicc.json")
        with open(aicc_path, "w", encoding="utf-8") as fh:
            json.dump(aicc, fh, ensure_ascii=False)
        da = load_card(aicc_path)
        assert da.get("_adapter") == "aicc", da.get("_adapter")
        assert da.get("name") == "测试A", da
        # 一对一映射
        assert "修钟表的老太太" in da.get("description", ""), da.get("description")
        assert "十七岁那年" in da.get("scenario", ""), da.get("scenario")
        assert "冬天常有雾" in da.get("scenario", ""), da.get("scenario")
        # personality 三段合并
        assert "沉默寡言" in da.get("personality", ""), da.get("personality")
        assert "不主动谈论自己的过去" in da.get("personality", ""), da.get("personality")
        assert "时间不等人" in da.get("personality", ""), da.get("personality")
        # dialogue
        assert da.get("first_mes", "").startswith("风把门吹开了"), da.get("first_mes")
        assert da.get("alternate_greetings") == ["又一块坏表。坐下吧。"], da.get("alternate_greetings")
        assert "别晃它" in da.get("mes_example", ""), da.get("mes_example")
        # prompts
        assert da.get("system_prompt") == "保持克制的语气", da.get("system_prompt")
        assert da.get("post_history_instructions") == "不要突然变得热情"
        # 🔴 接不上的字段也必须留着（丢字段=静默丢人格）
        assert "旧伤疤" in da.get("appearance", ""), da.get("appearance")
        # 世界书内核是标准 ST 格式 ⇒ 本仓的匹配器能直接吃
        assert len((da.get("character_book") or {}).get("entries") or []) == 1
        assert match_world_book(da, "我那钟表坏了") == ["她的工作台上有三台老钟"], \
            "aicc 卡的世界书条目要能被本仓匹配器直接吃"
        # metadata
        assert da.get("creator") == "某某", da.get("creator")
        assert "手艺" in (da.get("tags") or []), da.get("tags")
        # 🔴 最终拼出来的 prompt 不许是空壳
        sys_out = build_system(da)
        assert len(sys_out) > 150, "适配后的卡拼出来太短，可能读空了：" + sys_out
        assert "修钟表的老太太" in sys_out

        # --- V1 卡（顶层平铺、无 spec 字段）仍然要读得开 ---
        v1 = {"name": "V1鱼", "description": "V1 的描述", "personality": "V1 性格"}
        v1_path = os.path.join(tmp, "v1only.json")
        with open(v1_path, "w", encoding="utf-8") as fh:
            json.dump(v1, fh, ensure_ascii=False)
        d_v1 = load_card(v1_path)
        assert d_v1.get("name") == "V1鱼", d_v1
        assert d_v1.get("description") == "V1 的描述", d_v1
        assert d_v1.get("_spec") == "v1", d_v1

        print("[selftest] 陌生 spec 报明确原因、不降级读空 OK")
        print("[selftest] aicc 私有 schema 适配：内容全在、世界书能匹配 OK")
        print("[selftest] PASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        import argparse
        ap = argparse.ArgumentParser(description="角色卡工具")
        ap.add_argument("--list", action="store_true", help="列出卡库")
        ap.add_argument("--use", help="选中一张卡")
        ap.add_argument("--prompt", action="store_true", help="打印当前卡的 system prompt")
        a = ap.parse_args()
        if a.list:
            for c in list_cards():
                if c["ok"]:
                    s = c["summary"]
                    print(f"  [OK]   {c['file']}  {s['name']}（{s['spec']}）")
                else:
                    print(f"  [坏卡] {c['file']}  {c['error']}")
        if a.use:
            d = set_active(a.use)
            print("已选中:", d.get("name"), d.get("_spec"))
        if a.prompt:
            print(system_prompt() or "（没有选中角色卡）")
