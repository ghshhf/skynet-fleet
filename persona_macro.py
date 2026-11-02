"""角色卡宏引擎：把 {{char}} / {{user}} / {{getvar}} 这类占位符算成真值。

为什么要单开一层（2026-11-03，融合 ElecKoi 的能力）：

1）**中文边界**。天网原来在 mes_example 里直接
   `text.replace("{{user}}", user_name)`。英文没问题，中文会留下多余空格：
   「{{char}} 说」→「她 说」。ElecKoi 的 resolveCharacterCardMacros 在替换值是
   CJK 时吃掉相邻水平空白。这不是排版洁癖——模型看到「她 说」会跟着断句。

2）**变量宏**。MVU 生态的卡把世界状态写进正文：
   `{{get_message_variable::stat_data.好感}}` / `{{format_message_variable::stat_data}}`。
   只换 char/user 等于把宏原样喂给模型，模型会照着念「大括号 get_message_variable」。

3）**显示标记**。卡用 `<FINAL>`、`<StatusPlaceHolderImpl/>` 当内部锚点，这些是给
   前端的指令，不该出现在人眼前。

边界：**不执行 JavaScript / EJS**。ElecKoi 用 worker + vm 隔离跑作者模板，天网是
Python 侧、不引 JS 运行时；这里只做「纯文本宏 + 变量寻址」，遇到 EJS 交给
persona_regex 显式报「不支持」而不是静默算出错的字符串。
"""
import json
import re

#: 水平空白：CJK 排版里这些空隙应当被吃掉
_HSPACE = " \t\u00a0\u3000"

_IDENTITY_RE = re.compile(r"\{\{\s*(user|char)\s*\}\}", re.I)
_GET_MSG_VAR_RE = re.compile(r"\{\{get_message_variable::(.*?)\}\}", re.I)
_FMT_MSG_VAR_RE = re.compile(r"\{\{format_message_variable::(.*?)\}\}", re.I)
_GETVAR_RE = re.compile(r"\{\{getvar::\s*([^}]+?)\s*\}\}", re.I)
_SETVAR_RE = re.compile(r"\{\{setvar::\s*([^:}]+?)\s*::\s*([^}]*)\}\}", re.I)
_ADDVAR_RE = re.compile(r"\{\{addvar::\s*([^:}]+?)\s*::\s*([^}]*)\}\}", re.I)
_INCVAR_RE = re.compile(r"\{\{incvar::\s*([^}]+?)\s*\}\}", re.I)
_COMMENT_RE = re.compile(r"\{\{//.*?\}\}", re.S)


# --------------------------------------------------------------------------
# CJK 边界（精确移植 ElecKoi shared/foundation/characterCardMacros.ts）
# --------------------------------------------------------------------------

def is_cjk(ch):
    """这个字符算不算「中文排版意义上的字」。"""
    if not ch:
        return False
    c = ord(ch[0])
    return (0x3000 <= c <= 0x30FF or 0x3400 <= c <= 0x4DBF
            or 0x4E00 <= c <= 0x9FFF or 0xAC00 <= c <= 0xD7AF
            or 0xF900 <= c <= 0xFAFF or 0xFF01 <= c <= 0xFF65)


def _trim_cjk_spacing(out):
    """替换值以中文开头：把前面已经攒下的水平空白吃掉。"""
    b = len(out)
    while b > 0 and out[b - 1] in _HSPACE:
        b -= 1
    if b < len(out) and b > 0 and is_cjk(out[b - 1]):
        return out[:b]
    return out


def _skip_hspace(text, i):
    """替换值以中文结尾：跳过紧跟的水平空白，但只在下一个字符也是中文时才跳。"""
    j = i
    while j < len(text) and text[j] in _HSPACE:
        j += 1
    return j


def resolve_identity(text, char=None, user=None):
    """把 {{char}} / {{user}} 换成真名，并按中文边界修剪空格。

    char/user 为 None 时保留原样（不知道就别瞎换）。
    """
    text = str(text or "")
    if not text or ("{{" not in text):
        return text
    matches = list(_IDENTITY_RE.finditer(text))
    if not matches:
        return text
    out = []
    src = 0
    for m in matches:
        out.append(text[src:m.start()])
        repl = user if m.group(1).lower() == "user" else char
        if repl is None:
            out.append(m.group(0))
            src = m.end()
            continue
        repl = str(repl)
        head = "".join(out)
        if repl and is_cjk(repl[0]):
            head = _trim_cjk_spacing(head)
        out = [head, repl]
        src = m.end()
        if repl and is_cjk(repl[-1]):
            nxt = _skip_hspace(text, src)
            if nxt > src and nxt < len(text) and is_cjk(text[nxt]):
                src = nxt
    out.append(text[src:])
    return "".join(out)


def resolve_identity_in_json(source, char=None, user=None):
    """卡里的 JSON 字符串（变量初始态等）也要能认宏。

    解析失败就原样返回——宁可少一次替换，也不要把 JSON 弄坏。
    """
    source = str(source or "")
    if "{{" not in source:
        return source
    try:
        parsed = json.loads(source)
    except Exception:
        return source
    changed = [False]

    def walk(v):
        if isinstance(v, str):
            nv = resolve_identity(v, char, user)
            if nv != v:
                changed[0] = True
            return nv
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    out = walk(parsed)
    return json.dumps(out, ensure_ascii=False) if changed[0] else source


# --------------------------------------------------------------------------
# 变量寻址 + YAML 风格序列化（移植 MVU 的 get/format_message_variable）
# --------------------------------------------------------------------------

def message_path(state, raw_path):
    """按 MVU 的路径语义取值：`stat_data.a.b` / `stat_data.a.0.b`。

    根特例：path 为空或 `stat_data` 本身 → 整个 state。
    取不到返回 None（不是报错——卡里的变量名经常写错，模型要能继续演）。

    ⚠️ 这里**不剥 stat_data 前缀**：state 的根就是 {"stat_data": {...}}，
    剥了再从根遍历等于去根上找「好感」，永远取不到。兼容裸 stat_data 传进来
    的情况（path 不带前缀），所以先按完整路径走，走不通再退到 stat_data 下找。
    """
    if state is None:
        return None
    segs = [s for s in str(raw_path or "").strip().split(".") if s]
    if not segs:
        return state

    def walk(root, parts):
        cur = root
        for seg in parts:
            if isinstance(cur, list):
                try:
                    idx = int(seg)
                except ValueError:
                    return None
                cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            elif isinstance(cur, dict):
                cur = cur.get(seg)
            else:
                return None
            if cur is None:
                return None
        return cur

    got = walk(state, segs)
    if got is not None:
        return got
    if segs[0] == "stat_data" and isinstance(state, dict):
        # state 没有 stat_data 这层（直接就是 stat 内容）时退一步
        return walk(state.get("stat_data", state), segs[1:])
    return None


def _yaml_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    text = "" if v is None else str(v)
    if not text:
        return '""'
    ambiguous = (text != text.strip() or "\n" in text or ": " in text
                 or text.startswith("#")
                 or re.match(r"^(null|true|false)$", text, re.I)
                 or (text.strip() != "" and _is_number(text)))
    return json.dumps(text, ensure_ascii=False) if ambiguous else text


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def _yaml_key(k):
    return k if re.match(r"^[\w\u4e00-\u9fff\-]+$", str(k)) else json.dumps(str(k), ensure_ascii=False)


def yaml_like(value, indent=0):
    """把变量树写成人类可读的缩进块。

    为什么不用 json.dumps：变量块是要塞进 prompt 的，YAML 风格比 JSON 省 token
    （不用引号、不用逗号），模型也更容易顺着读。
    """
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            if isinstance(item, (list, dict)):
                body = yaml_like(item, indent + 2).replace("\n", "\n" + pad + "  ")
                lines.append("-\n" + pad + "  " + body)
            else:
                lines.append("- " + _yaml_scalar(item))
        return "\n".join(lines)
    if isinstance(value, dict):
        entries = [(k, v) for k, v in value.items() if not str(k).startswith("$")]
        if not entries:
            return "{}"
        lines = []
        for k, v in entries:
            if isinstance(v, (list, dict)):
                body = yaml_like(v, indent + 2).replace("\n", "\n" + pad + "  ")
                lines.append("%s:\n%s  %s" % (_yaml_key(k), pad, body))
            else:
                lines.append("%s: %s" % (_yaml_key(k), _yaml_scalar(v)))
        return "\n".join(lines)
    return _yaml_scalar(value)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render(text, char=None, user=None, state=None, variables=None):
    """渲染一段卡文本里的宏，返回 (新文本, 变量表)。

    variables 是 ST 的 {{getvar}} 命名空间（与 MVU 的 stat_data 不是一回事：
    前者是「账号级小抄」，后者是「这一局的存档」）。setvar/addvar 会写进返回的
    变量表，调用方自己决定要不要落盘——本函数不产生副作用。
    """
    text = _COMMENT_RE.sub("", str(text or ""))
    if not text:
        return "", dict(variables or {})
    env = dict(variables or {})

    # 先处理写操作，再处理读操作，保证同一段里 setvar 之后的 getvar 看得到新值
    def do_set(m):
        env[m.group(1).strip()] = m.group(2)
        return ""

    def do_add(m):
        key, raw = m.group(1).strip(), m.group(2)
        try:
            env[key] = str(float(env.get(key) or 0) + float(raw))
        except ValueError:
            env[key] = str(env.get(key) or "") + raw
        return ""

    def do_inc(m):
        key = m.group(1).strip()
        try:
            env[key] = str(float(env.get(key) or 0) + 1)
        except ValueError:
            env[key] = "1"
        return ""

    text = _SETVAR_RE.sub(do_set, text)
    text = _ADDVAR_RE.sub(do_add, text)
    text = _INCVAR_RE.sub(do_inc, text)

    text = _GETVAR_RE.sub(lambda m: str(env.get(m.group(1).strip(), "")), text)
    if state is not None:
        text = _GET_MSG_VAR_RE.sub(
            lambda m: _compact(message_path(state, m.group(1))), text)
        # format 是「多行块」，要反复替换（嵌在别的宏里面时一次替换可能算不完）
        for _ in range(8):
            m = _FMT_MSG_VAR_RE.search(text)
            if not m:
                break
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = " " * (m.start() - line_start)
            body = yaml_like(message_path(state, m.group(1)) or {}).replace("\n", "\n" + prefix)
            text = text[:m.start()] + body + text[m.end():]
    text = resolve_identity(text, char, user)
    return hide_display_markers(text), env


def _compact(v):
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def hide_display_markers(text):
    """去掉只给前端看的锚点，别让模型学着写。"""
    return (str(text or "")
            .replace("<FINAL>", "").replace("</FINAL>", "")
            .replace("<StatusPlaceHolderImpl/>", ""))


def looks_like_ejs(text):
    """这段是不是 EJS / JS 模板（本层不执行，调用方据此降级）。"""
    t = str(text or "")
    return ("<% " in t) or ("<%=" in t) or ("<%_" in t) or ("<%#" in t)
