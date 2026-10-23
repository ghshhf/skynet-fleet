"""技能加载器：读 SKILL.md，认出「有哪些技能、每张卡干什么」。

为什么技能是 SKILL.md 而不是能力清单：
  **能力是「能做什么」**（注册表里登记的 70 项，能改外部世界，要过闸门）；
  **技能是「怎么做」**（一份方法论文档，教模型按什么流程办事，
  自己不动手、不改外部世界）。
  两者混在一起，就会出现「技能要审批吗」这种没法答的问题。

格式（Claude/WorkBuddy 生态通用）：
  一个目录 = 一个技能，目录里必须有 `SKILL.md`。
  文件头部是 YAML frontmatter：

      ---
      name: crypto-token-analysis
      description: This skill should be used when ...
      agent_created: true
      ---

      # 正文（Markdown）

为什么不用 pyyaml 解析 frontmatter：
  本机没装 pyyaml（已确认 `ModuleNotFoundError`），
  而 frontmatter 只需要「顶层标量键值」这一种形状。
  自己写一个**只认这一种形状**的解析器，30 行，零依赖，
  遇到不认识的形状就**如实报错**而不是瞎猜。

为什么技能目录要扫两个地方：
  · 本仓 `skills/`       —— 本仓自己的技能，跟着代码走
  · 用户 `~/.workbuddy/skills/` —— 用户级的技能（37 个真实的）
  用户级是真数据源，不造假卡。
"""
import os


#: 技能根目录。仓内优先，其次用户级。
REPO_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills")
USER_SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")

SKILL_FILE = "SKILL.md"


class SkillError(Exception):
    """技能读不出来。message 必须说清是哪个技能、哪个字段。"""


# ---------------------------------------------------------------- frontmatter

def parse_frontmatter(text, where=""):
    """拆出 frontmatter 与正文。返回 (meta, body)。

    格式要求：文件第一行必须是 `---`，然后是 `key: value` 行，
    遇到下一个 `---` 结束 frontmatter。

    边界：
      · 没有 frontmatter ⇒ meta 为空 dict（不算错：有些技能就是纯 Markdown）
      · frontmatter 没闭合 ⇒ 报错并说清缺哪个 `---`
      · 键没有 `:` ⇒ 报错并指出是哪一行
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    last_key = None
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue                     # frontmatter 里允许注释行
        if ":" not in line:
            # 不是 key: value —— 但真实世界的前置元数据有两种常见写法，
            # 2026-10-22 实测 38 条真实技能里有 7 条因此读不出来
            # （多行描述、YAML 列表项）：
            #   · 缩进续行   `  描述文字……`
            #   · 列表项     `  - bash`
            # 两种都并进上一个 key，不再一律报错。
            # 仍要报错的是「既不是续行也不是列表项」的乱行
            # （自测里那一行就是这种，必须继续报错——不然真错误会被静默吞掉）。
            stripped = line.strip()
            if last_key is None:
                raise SkillError(
                    f"{where or 'SKILL.md'} 的 frontmatter 第 {i + 1} 行不是 key: value：{line!r}")
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                cur = meta.get(last_key)
                if isinstance(cur, list):
                    cur.append(item)
                elif cur in (None, ""):
                    meta[last_key] = [item]
                else:
                    meta[last_key] = [str(cur), item]
            elif line[:1] in (" ", "\t"):
                meta[last_key] = (str(meta.get(last_key) or "") + " " + stripped).strip()
            else:
                raise SkillError(
                    f"{where or 'SKILL.md'} 的 frontmatter 第 {i + 1} 行不是 key: value：{line!r}")
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # 去掉一层引号（YAML 风格），但不做完整 YAML 解析
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        last_key = key
        if key in ("name", "description", "agent_created", "license", "version"):
            meta[key] = val
        elif key in ("evolved_from", "caps"):
            # 这两个是列表。frontmatter 只支持标量的解析器在这里加一条
            # 明确支持的单行列表语法：`key: [a, b, c]`。
            # 为什么值得加：技能进化要声明「我从哪几条证据长出来」
            # （evolved_from）和「我要用哪些能力」（caps），
            # 没这两个字段，技能就只是一段散文，没法验证。
            meta[key] = _parse_inline_list(val, where)
        elif key == "guard":
            meta[key] = val
        else:
            # 未知键也留着（可能是别人生态的字段），但不报错
            meta.setdefault("extra", {})[key] = val
    raise SkillError(
        f"{where or 'SKILL.md'} 的 frontmatter 没有闭合（少了收尾的 ---）")


def _parse_inline_list(val, where=""):
    """解析 `key: [a, b, c]` 这种单行列表。

    只支持这一种形状（方括号 + 逗号分隔）。遇到别的形状**如实报错**，
    不猜、不静默返回空列表——空列表会让技能看起来"没有引用任何能力"，
    那是假的。
    """
    s = (val or "").strip()
    if not (s.startswith("[") and s.endswith("]")):
        raise SkillError(
            f"{where or 'SKILL.md'} 的列表字段必须写成 [a, b, c]，得到 {val!r}")
    inner = s[1:-1].strip()
    if not inner:
        return []
    out = []
    for part in inner.split(","):
        p = part.strip()
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        if p:
            out.append(p)
    return out


# ---------------------------------------------------------------- 读一个技能

def _first_heading(body):
    """正文里第一个 markdown 标题（当副标题用）。"""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return ""


def load_skill(path):
    """读一个 SKILL.md，返回一条技能记录。

    记录形状（前端只吃这一个形状）：
      {name, description, dir, file, source, heading, body_len, agent_created}
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise SkillError(f"技能文件不存在：{path}")
    except UnicodeDecodeError as e:
        raise SkillError(f"技能文件不是 UTF-8：{path}（{e}）")
    except Exception as e:
        raise SkillError(f"读不了技能文件 {path}：{type(e).__name__}: {e}")

    meta, body = parse_frontmatter(text, where=path)
    name = (meta.get("name") or "").strip()
    desc = (meta.get("description") or "").strip()
    dir_name = os.path.basename(os.path.dirname(os.path.abspath(path)))

    if not name:
        # 没有 name 就退回目录名，但要说清（不是静默）
        name = dir_name
    if not desc:
        desc = _first_heading(body)

    # evolved_from 的 id 必须是整数（证据是 journal 的自增 id）。
    # 这里只做**形状**校验（是不是整数），不查存在性——
    # 存在性要问证据库，那是 P2 验证门的事，两边职责不混。
    ev_from = []
    for raw in (meta.get("evolved_from") or []):
        try:
            ev_from.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            raise SkillError(
                f"{path} 的 evolved_from 里有不是整数 id 的项：{raw!r}")
    caps = [str(c).strip() for c in (meta.get("caps") or []) if str(c).strip()]

    return {
        "name": name,
        "description": desc,
        "dir": os.path.dirname(os.path.abspath(path)),
        "file": os.path.basename(path),
        "source": "repo" if _is_repo(path) else "user",
        "heading": _first_heading(body),
        "body_len": len(body),
        "agent_created": str(meta.get("agent_created", "")).lower() == "true",
        "evolved_from": ev_from,
        "caps": caps,
        "guard": (meta.get("guard") or "").strip(),
        "body": body,
    }


def validate_caps(skill, cap_names):
    """核技能声明的能力名是否都真实存在。

    返回 (ok, 缺失列表, 说明)。缺失时**指名道姓**说是哪个能力不存在，
    不许只说「有错」——使用者得知道去改哪一行。
    """
    caps = list(skill.get("caps") or [])
    if not caps:
        return False, [], "这张技能没有声明 caps（不许留空：空 = 没法验证它做过什么）"
    known = set(cap_names or [])
    missing = [c for c in caps if c not in known]
    if missing:
        return False, missing, ("caps 里这些能力在注册表里不存在：%s"
                                % "、".join(missing))
    return True, [], "caps 里的 %d 个能力都存在" % len(caps)


def _is_repo(path):
    try:
        return os.path.abspath(path).startswith(
            os.path.abspath(REPO_SKILLS_DIR) + os.sep)
    except Exception:
        return False


# ---------------------------------------------------------------- 扫目录

def scan_dir(root):
    """扫一个技能根目录。目录不存在 ⇒ 空清单（不是错误）。

    只认「一级子目录 + 里面有 SKILL.md」——
    与 Claude/WorkBuddy 生态的约定一致，不搞更深的扫描。
    """
    if not root or not os.path.isdir(root):
        return []
    out = []
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        return []
    for fn in entries:
        d = os.path.join(root, fn)
        if not os.path.isdir(d):
            continue
        sk = os.path.join(d, SKILL_FILE)
        if not os.path.isfile(sk):
            continue
        try:
            out.append(load_skill(sk))
        except SkillError:
            # 一个坏技能不许让整份清单消失——但要记下来。
            out.append({"name": fn, "description": "", "dir": d, "file": SKILL_FILE,
                        "source": "repo" if _is_repo(sk) else "user",
                        "heading": "", "body_len": 0, "agent_created": False,
                        "evolved_from": [], "caps": [], "guard": "", "body": "",
                        "error": "这个技能的 SKILL.md 解析失败"})
    return out


def list_skills(roots=None):
    """全部技能（仓内 + 用户级）。

    `roots` 给了就用它（测试用），否则用默认两个目录。
    仓内的排前面——本仓自己的技能优先级更高。
    """
    if roots is None:
        roots = [REPO_SKILLS_DIR, USER_SKILLS_DIR]
    out = []
    for r in roots:
        out.extend(scan_dir(r))
    return out


def summary():
    """给 /api/skills 的返回体。

    正文（body）不往外发：38 张技能的正文字数加起来不小，
    而 /api/skills 只是给前端看清单，不是给模型读全文。
    要读正文请按目录自己开文件。
    """
    skills = list_skills()
    brief = []
    for s in skills:
        s = dict(s)
        s.pop("body", None)
        brief.append(s)
    return {"ok": True, "count": len(brief), "skills": brief,
            "roots": [REPO_SKILLS_DIR, USER_SKILLS_DIR]}


# ---------------------------------------------------------------- 自测

def _selftest():
    import tempfile

    # frontmatter 正常解析
    txt = ("---\nname: demo\ndescription: 这是一个演示\nagent_created: true\n---\n"
           "\n# 标题\n\n正文内容\n")
    meta, body = parse_frontmatter(txt)
    assert meta["name"] == "demo"
    assert meta["description"] == "这是一个演示"
    assert meta["agent_created"] == "true"
    assert "正文内容" in body
    assert not body.lstrip().startswith("---")

    # 没有 frontmatter ⇒ meta 空、正文原样
    meta2, body2 = parse_frontmatter("# 只有正文\n")
    assert meta2 == {}
    assert body2.startswith("# 只有正文")

    # 没闭合 ⇒ 报错
    try:
        parse_frontmatter("---\nname: x\ndescription: y\n", where="t.md")
    except SkillError as e:
        assert "没有闭合" in str(e)
    else:
        raise AssertionError("frontmatter 没闭合应该报错")

    # 坏行 ⇒ 报错并指出行号
    try:
        parse_frontmatter("---\nname: x\n这不是键值\n---\n", where="t.md")
    except SkillError as e:
        assert "第 3 行" in str(e)
    else:
        raise AssertionError("坏行应该报错")

    # 引号剥掉
    meta3, _ = parse_frontmatter('---\nname: "q"\n---\n')
    assert meta3["name"] == "q"

    # 列表字段：单行 [a, b, c]
    meta4, _ = parse_frontmatter(
        "---\nname: d4\nevolved_from: [12, 15]\ncaps: [a.b, c.d]\n"
        "guard: 任一步失败即停\n---\n")
    assert meta4["evolved_from"] == ["12", "15"], meta4
    assert meta4["caps"] == ["a.b", "c.d"], meta4
    assert meta4["guard"] == "任一步失败即停", meta4

    # 列表字段写成别的形状 ⇒ 如实报错，不静默给空列表
    try:
        parse_frontmatter("---\nname: d5\ncaps: a.b, c.d\n---\n", where="t.md")
    except SkillError as e:
        assert "[a, b, c]" in str(e), str(e)
    else:
        raise AssertionError("列表字段形状不对应该报错")

    # evolved_from 里的非整数 id ⇒ 报错（id 是 journal 自增号）
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "ev")
        os.makedirs(d)
        p = os.path.join(d, SKILL_FILE)
        with open(p, "w", encoding="utf-8") as f:
            f.write("---\nname: ev\ndescription: x\n"
                    "evolved_from: [12, 不是数字]\ncaps: [a.b]\n---\n\n# 正文\n")
        try:
            load_skill(p)
        except SkillError as e:
            assert "不是整数" in str(e), str(e)
        else:
            raise AssertionError("非整数证据 id 应该报错")

    # validate_caps：指名道姓说是哪个能力不存在
    ok, missing, why = validate_caps({"caps": ["x.y", "z.w"]}, ["x.y"])
    assert not ok and missing == ["z.w"], (missing, why)
    assert "z.w" in why, why
    ok2, _, _ = validate_caps({"caps": ["x.y", "z.w"]}, ["x.y", "z.w"])
    assert ok2
    ok3, _, why3 = validate_caps({"caps": []}, ["x.y"])
    assert not ok3 and "没有声明 caps" in why3, why3

    # 扫目录：临时造一个技能
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "my-skill")
        os.makedirs(d)
        with open(os.path.join(d, SKILL_FILE), "w", encoding="utf-8") as f:
            f.write("---\nname: my-skill\ndescription: 干活的\n---\n\n# 干活\n\n步骤\n")
        got = scan_dir(td)
        assert len(got) == 1
        assert got[0]["name"] == "my-skill"
        assert got[0]["description"] == "干活的"
        assert got[0]["heading"] == "干活"

        # 没有 SKILL.md 的目录不算技能
        os.makedirs(os.path.join(td, "not-a-skill"))
        assert len(scan_dir(td)) == 1

        # 目录不存在 ⇒ 空，不报错
        assert scan_dir(os.path.join(td, "nope")) == []

    # 真目录：用户级 37 个技能（存在的话）
    user = scan_dir(USER_SKILLS_DIR)
    if os.path.isdir(USER_SKILLS_DIR):
        assert len(user) > 0, "用户级技能目录存在却一个都没读到"
        names = [s["name"] for s in user]
        assert "crypto-token-analysis" in names, names[:10]

    print(f"fleet.skills selftest ok（读到 {len(list_skills())} 个技能）")


if __name__ == "__main__":
    _selftest()
