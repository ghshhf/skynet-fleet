# -*- coding: utf-8 -*-
"""把 markdown 角色卡（天网语音线）转成 ccv2 JSON 角色卡（X6 图形线）。

为什么存在：仓库里有两套卡——`cards/*.md`（天网桌面端今晚写的中文体例）
和 `persona_card.py`（X6 统一卡层，只认 chara_card_v2/v3 / V1 的 .json/.png）。
统一的方向是**嵌合**：md 是人写的人读的源稿，json 是运行时格式；
本工具把 md 一次性编译成 json，之后 X6 的卡库/卡商店/语音端用的是同一批卡。

字段映射（md → ccv2 data）：
  # 标题            -> name
  性格              -> personality
  说话方式/自称/口癖 -> mes_example（风格示例区，逐行）
  立场/核心冲突      -> scenario
  禁止 + ## 小节     -> system_prompt（人设补充，含协议映射）
  其余全部（定位/阵营/底色/世界观/装备/关系…） -> description（不丢信息）

用法：
    python tools/md2card.py                 # 转换 cards/ 下全部 .md
    python tools/md2card.py cards/xx.md     # 只转一张
同名 .json 已存在且比 .md 新时跳过（--force 覆盖）。
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARD_DIR = os.path.join(REPO, "cards")

FIELD_RE = re.compile(r"^[-*]\s*([^：:]{1,14})[：:]\s*(.*)$")


def parse_md(text):
    """md 卡 → ({字段: 值}, [小节标题, 正文])。"""
    fields, sections = {}, []
    cur_section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            cur_section = line[3:].strip()
            sections.append([cur_section, []])
            continue
        if line.startswith("# "):
            cur_section = None
            fields.setdefault("_title", line[2:].strip())
            continue
        if line.startswith("#"):        # 其它级标题跳过
            continue
        m = FIELD_RE.match(line.strip())
        if m and cur_section is None:
            k, v = m.group(1).strip(), m.group(2).strip()
            if v:
                fields[k] = (fields.get(k) + "\n" + v) if k in fields else v
            continue
        if cur_section is not None:
            sections[-1][1].append(line.strip())
        else:
            # 字段行之外的散文本归入底色
            fields["底色"] = (fields.get("底色", "") + "\n" + line.strip()).strip()
    return fields, [(t, "\n".join(b)) for t, b in sections if b]


def md_to_card(text):
    f, sections = parse_md(text)
    name = f.pop("_title", "") or "她"

    desc_parts = []
    for k in ("定位", "阵营", "底色", "装备", "关系", "世界观"):
        if f.get(k):
            desc_parts.append("%s：%s" % (k, f.pop(k)))

    personality = f.pop("性格", "")
    scenario = "；".join(f.pop(k) for k in ("立场", "核心冲突") if f.get(k))

    style_lines = []
    for k in ("说话方式", "自称", "口癖"):
        if f.get(k):
            style_lines.append("%s：%s" % (k, f.pop(k)))
    # 剩余没归类的字段也别丢
    for k, v in f.items():
        desc_parts.append("%s：%s" % (k, v))

    sp_parts = []
    if f.get("__残余__"):
        pass
    forbid = None
    for k in ("禁止",):
        if f.pop(k, None):
            pass
    # 禁止/协议在 sections/f pop 后可能仍在 f 里（上面 pop 顺序）——统一兜底：
    sp_parts = []
    if sections:
        for t, b in sections:
            sp_parts.append("【%s】\n%s" % (t, b))

    data = {
        "name": name,
        "description": "\n".join(desc_parts).strip(),
        "personality": personality.strip(),
        "scenario": scenario.strip(),
        "mes_example": "\n".join(style_lines).strip(),
        "system_prompt": "\n\n".join(sp_parts).strip(),
        "first_mes": "",
    }
    if data["mes_example"]:
        data["mes_example"] = (data["mes_example"] +
                               "\n{{char}}：嗯——我在呢，主人。").strip()
    return data


def convert(path, force=False):
    out = os.path.splitext(path)[0] + ".json"
    if os.path.exists(out) and not force:
        if os.path.getmtime(out) >= os.path.getmtime(path):
            return out, "skip"
    text = open(path, encoding="utf-8").read()
    data = md_to_card(text)
    if not data["first_mes"]:
        data["first_mes"] = "嗯——「%s」在呢，主人。" % data["name"]
    obj = {"spec": "chara_card_v2", "data": data}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    return out, "ok"


def main(argv):
    force = "--force" in argv
    paths = [a for a in argv if not a.startswith("-")]
    if not paths:
        paths = [os.path.join(CARD_DIR, f) for f in sorted(os.listdir(CARD_DIR))
                 if f.endswith(".md")]
    for p in paths:
        out, st = convert(p, force)
        print("%-6s %s -> %s" % (st, os.path.basename(p), os.path.basename(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
