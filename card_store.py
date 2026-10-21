"""角色卡商店：拉取器 + 体检器 + 落盘。

这一层解决主人那句「能自己浏览卡、能随便下载、随便切换」里的**前两半**：
浏览（搜源上的卡）与下载（把卡落到 `cards/`）。第三半「切换」早就有
（`POST /api/card/use` → `persona_card.set_active`），本模块不动它。

为什么单独一个模块而不是塞进 GUI 后端：
  ① 「借天下」是本仓铁律——**每个源都是一个独立适配器**，加一个新源
     只许加一个函数，不许改别人的代码。
  ② 体检器（lint）不是商店的附属品，它对**本地投递口**的卡同样要跑。
     所以它独立于任何源。
  ③ 它要能被命令行、MCP、图形版共用 ⇒ 不许 import 任何 GUI 模块。

🔴 三条铁律（都是实测踩出来的）：

  1. **「能下到 PNG」≠「能下到卡」。**
     aicharactercards 的 `GET /api/cards/{id}/download` 返回的是**头像 PNG**
     ——里面只有 `aicc_id` / `aiCd` 两个 tEXt 块，**没有卡数据**。
     真卡文件路径在**详情接口**的 `versions[].fileUrl` 里。
     照着「download 端点就是卡文件」的直觉写，拿到的是一张能显示、
     但读不出任何人格的图——**又是静默失败**。
  2. **本机下载会中途截断。**
     实测同一个 953733 字节的文件：单次 curl 只落 359474 字节，
     换 `fileUrl` 只落 262144 字节（= 正好 256KB）。
     ⇒ 必须 **Range 续传**，且续到 IEND 为止。不校验完整性就落盘，
     会把半张 PNG 当成卡存进 `cards/`（然后 `load_card` 报「没有卡块」）。
  3. **下载有冷却。**
     aicharactercards 对同一张卡有 30 分钟下载冷却，重复打返回
     `{"error":"Duplicate download"}`。
     ⇒ 本地记一张「已下载过」的账（`cards/.downloads.json`），
     冷却期内直接说「这张刚下过，冷却到 HH:MM」而不是再打一次。

**口径声明**：本模块只接**能程序化拉取**的源。Chub / JanitorAI /
CharacterHub / Discord 中文社区实测不可程序化拉（403 / 超时 / 要人工），
**不做绕过**——绕鉴权不在本仓的选项里。那些源走 `cards_inbox/`：
主人自己下好的卡扔进那个目录，本仓负责体检 + 入库 + 切换。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

#: 默认卡目录。**唯一真相在 persona_card.DEFAULT_CARD_DIR**，这里不自己算一遍
#: （之前三处各算一遍 ⇒ 卡下到 A 处、界面读 B 处）。
def _default_card_dir():
    import persona_card as pc
    return pc.DEFAULT_CARD_DIR

#: 本地投递口：主人把手动下载的卡扔这里，本仓体检后入库。
INBOX_DIRNAME = "cards_inbox"

#: 下载账（记冷却用）。
LEDGER_NAME = ".downloads.json"

#: 网络超时（秒）。小文件 20s 足够；大卡另走分块续传。
HTTP_TIMEOUT = 20

#: 单次下载的块大小（Range 续传每块 1MB，避免一次拉 900KB 被截）。
CHUNK = 1024 * 1024

#: 续传最多试几轮。实测 2 轮就够（第一轮 359KB，第二轮补到 953733）。
MAX_RESUME_ROUNDS = 8

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) device-fleet/0.1"


# --------------------------------------------------------------------------
# 源适配器
# --------------------------------------------------------------------------
#
# 每个源实现两个函数：
#   list_cards(query, skip, limit) -> {"total": int, "items": [{id, title, ...}]}
#   card_url(card_id)              -> (下载 URL, 显示名)
#
# 🔴 源不可用时**抛异常并说清原因**，不返回空列表假装「没搜到」。

class SourceError(Exception):
    """源不可用/请求失败。消息就是给主人看的原因。"""


def _http(url, headers=None, timeout=HTTP_TIMEOUT):
    """GET 一个 URL，返回 (状态码, 字节内容, 响应头)。失败抛 SourceError。

    不用 requests（不引依赖）：本仓的纪律是「标准库能写就自己写」。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()[:400]
        except Exception:
            pass
        raise SourceError(f"HTTP {e.code} {e.reason}"
                          + (f"：{body.decode('utf-8', 'replace')[:200]}" if body else ""))
    except urllib.error.URLError as e:
        raise SourceError(f"连不上：{e.reason}")
    except Exception as e:
        raise SourceError(f"{type(e).__name__}: {e}")


def _http_json(url, headers=None, timeout=HTTP_TIMEOUT):
    code, body, _h = _http(url, headers=headers, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SourceError(f"返回的不是 JSON（HTTP {code}）：{e}")


# --------------------------------------------------------------------------
# 源 1：aicharactercards.com（实测唯一能程序化拉真卡的站）
# --------------------------------------------------------------------------

AICC_API = "https://api.aicharactercards.com/api"
AICC_FILE_BASE = "https://api.aicharactercards.com"


def aicc_list(query="", skip=0, limit=24):
    """搜索/浏览 aicharactercards。

    实测端点：`GET /api/cards/?search=&skip=&limit=`
      · 无 search → 全部
      · search   → 标题/描述模糊匹配
    返回 `{"message":..., "data":[...], "pagination":{"skip","limit","total"}}`。
    """
    q = urllib.parse.urlencode({"search": (query or "").strip(),
                                "skip": int(skip or 0),
                                "limit": int(limit or 24)})
    obj = _http_json(f"{AICC_API}/cards/?{q}")
    if not obj.get("success", True) and obj.get("error"):
        raise SourceError(str(obj.get("message") or obj.get("error")))
    data = obj.get("data") or []
    if not isinstance(data, list):
        raise SourceError("列表接口的 data 不是数组（源结构变了？）")
    page = obj.get("pagination") or {}
    items = []
    for c in data:
        if not isinstance(c, dict):
            continue
        items.append({
            "id": c.get("id"),
            "title": c.get("title") or "(无名)",
            "author": c.get("author") or "",
            "excerpt": (c.get("excerpt") or "")[:240],
            "tags": [t.get("name") for t in (c.get("tags") or [])
                     if isinstance(t, dict) and t.get("name")],
            "token_count": c.get("tokenCount"),
            "download_count": c.get("downloadCount"),
            "rating": c.get("ratingAvg"),
            "nsfw": bool(c.get("isNsfw")),
            "image": c.get("imageUrl") or "",
        })
    return {"total": page.get("total", len(items)), "items": items}


def aicc_detail(card_id):
    """取单卡详情。"""
    obj = _http_json(f"{AICC_API}/cards/{card_id}")
    if not isinstance(obj, dict) or not obj.get("id"):
        raise SourceError(f"详情接口没认出这张卡（id={card_id}）")
    return obj


def aicc_card_url(card_id):
    """真卡文件的直链。

    🔴 **不是** `/api/cards/{id}/download`——那个端点返回的是头像 PNG，
    里面没有卡数据块。实测真卡路径在详情接口的 `versions[].fileUrl`
    （相对路径，挂在 API 域名下）。
    """
    det = aicc_detail(card_id)
    versions = det.get("versions") or []
    rel = ""
    for v in versions:
        if isinstance(v, dict) and v.get("isCurrent") and v.get("fileUrl"):
            rel = v["fileUrl"]
            break
    if not rel:
        for v in versions:
            if isinstance(v, dict) and v.get("fileUrl"):
                rel = v["fileUrl"]
                break
    if not rel:
        raise SourceError(f"这张卡没有可下载的文件版本（id={card_id}，"
                          f"cardFormat={det.get('cardFormat')}）")
    if rel.startswith("http"):
        return rel, (det.get("title") or f"card-{card_id}")
    return AICC_FILE_BASE + rel, (det.get("title") or f"card-{card_id}")


# --------------------------------------------------------------------------
# 源注册表
# --------------------------------------------------------------------------

SOURCES = {
    "aicc": {
        "id": "aicc",
        "name": "aicharactercards.com",
        "note": "实测唯一能程序化拉真卡的站；卡是 aicc_card 私有 schema，本仓有适配器",
        "list": aicc_list,
        "url": aicc_card_url,
    },
}


def list_sources():
    """列出所有卡源（给界面下拉用）。"""
    return [{"id": k, "name": v["name"], "note": v["note"]}
            for k, v in SOURCES.items()]


# --------------------------------------------------------------------------
# 下载（Range 续传 + 完整性校验）
# --------------------------------------------------------------------------

def _png_complete(data):
    """PNG 是否完整：必须能走到 IEND。

    🔴 为什么必须查：本机下载会中途截断（实测 359KB/256KB 断点），
    半张 PNG 存进 cards/ 之后 `load_card` 只会说「PNG 里没有角色卡块」，
    主人看到的是「下载成功但卡是空的」——又是一次静默失败。
    """
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "不是 PNG 文件"
    i = 8
    n = len(data)
    while i + 8 <= n:
        ln = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        if ctype == b"IEND":
            return True, ""
        if i + 12 + ln > n:
            return False, (f"文件被截断（读到 {ctype.decode('latin-1')} 时数据不够，"
                           f"还差 {i + 12 + ln - n} 字节）")
        i += 12 + ln
    return False, "文件不完整：没走到 IEND"


def download(url, dest_path, timeout=HTTP_TIMEOUT):
    """把 url 下到 dest_path。**必须完整**，否则报错并删掉半成品。

    做法：Range 续传，一块一块补，直到 PNG 走完 IEND。
    """
    tmp = dest_path + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    total = 0
    last_err = ""
    for _round in range(MAX_RESUME_ROUNDS):
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            code, body, hdrs = _http(url, headers=headers, timeout=timeout)
        except SourceError as e:
            last_err = str(e)
            # Range 请求返回 416 = 已经下完了（服务端认为超出范围）
            if have and "416" in last_err:
                break
            raise SourceError(last_err)
        if have and code == 200:
            # 服务端不支持 Range，退回整份重下
            with open(tmp, "wb") as f:
                f.write(body)
            have = 0
        else:
            with open(tmp, "ab") as f:
                f.write(body)
        total = os.path.getsize(tmp)
        # 完整性判定：PNG 走完 IEND 就算好
        with open(tmp, "rb") as f:
            data = f.read()
        ok, why = _png_complete(data)
        if ok:
            os.replace(tmp, dest_path)
            return {"path": dest_path, "bytes": total, "rounds": _round + 1}
        last_err = why
        if "不是 PNG" in why:
            # 不是截断问题，是源给了别的东西 ⇒ 不用续传了
            break
    # 到这里还没下全
    if os.path.exists(tmp):
        os.remove(tmp)
    raise SourceError(f"下载不完整：{last_err or '未知原因'}（已重试 "
                      f"{MAX_RESUME_ROUNDS} 轮）")


# --------------------------------------------------------------------------
# 体检器
# --------------------------------------------------------------------------
#
# 体检器跟商店无关：本地投递口的卡、网上下的卡，都要过它。
# 它回答一个问题：**这张卡装进来之后，她到底会不会说话？**

#: 人格各段的「像话」下限（字符数）。低于此值视作内容缺失。
MIN_DESC = 60
MIN_MESG = 20
MIN_SYSTEM = 80


def lint_card(path):
    """体检一张卡。返回体检报告 dict，**不抛异常**（坏卡也要有报告）。

    报告形状：
      {file, ok, name, spec, adapter, score, issues:[{level, field, why}],
       stats:{desc, scenario, personality, first_mes, mes_example, system,
              worldbook, greetings}}
    """
    import persona_card as pc

    issues = []
    rep = {"file": os.path.basename(path), "ok": False, "name": "", "spec": "",
           "adapter": "", "score": 0, "issues": issues,
           "stats": {}}
    try:
        data = pc.load_card(path)
    except Exception as e:
        issues.append({"level": "bad", "field": "file",
                       "why": f"读不出来：{type(e).__name__}: {e}"})
        return rep

    rep["name"] = data.get("name") or "(无名)"
    rep["spec"] = data.get("_spec") or ""
    rep["adapter"] = data.get("_adapter") or ""

    def grab(key):
        v = data.get(key)
        return v if isinstance(v, str) else ""

    desc = grab("description")
    scen = grab("scenario")
    pers = grab("personality")
    sysp = grab("system_prompt")
    mesg = grab("mes_example")
    first = grab("first_mes")
    wb = pc.world_book_entries(data)
    greetings = data.get("alternate_greetings") or []
    if not isinstance(greetings, list):
        greetings = []

    rep["stats"] = {
        "desc": len(desc), "scenario": len(scen), "personality": len(pers),
        "first_mes": len(first), "mes_example": len(mesg),
        "system": len(sysp), "worldbook": len(wb),
        "greetings": len(greetings) + (1 if first else 0),
    }

    def bad(field, why):
        issues.append({"level": "bad", "field": field, "why": why})

    def warn(field, why):
        issues.append({"level": "warn", "field": field, "why": why})

    if not desc:
        bad("description", "没有 description（她是谁）—— 这张卡装上也不会说话")
    elif len(desc) < MIN_DESC:
        warn("description", f"description 只有 {len(desc)} 字，太薄")

    # 🔴 personality 在 V3 里是**可选字段**（实测有的卡把性格全写进 description）。
    # 判 bad 会误伤好卡 ⇒ 只当提醒，不当否决项。
    if not pers:
        warn("personality", "personality 字段是空的（这张卡把性格写进 description 了，"
                            "不算缺陷，只是没有独立性格段）")
    if not first:
        warn("first_mes", "没有开场白（她第一句说什么）")
    if len(mesg) < MIN_MESG:
        warn("mes_example", f"对话样例只有 {len(mesg)} 字（几乎没有）")
    if sysp and len(sysp) < MIN_SYSTEM:
        warn("system_prompt", f"system_prompt 只有 {len(sysp)} 字")

    # 真降级判定：把卡塞进 build_system，看它到底吐出多少字。
    # 这是**唯一权威**的判据——字段都在但拼不出人设，跟字段缺失是一回事。
    try:
        built = pc.build_system(data)
    except Exception as e:
        built = ""
        bad("build_system", f"拼人设时炸了：{type(e).__name__}: {e}")
    rep["built_chars"] = len(built)
    has_bad = any(i["level"] == "bad" for i in issues)
    if len(built) < MIN_SYSTEM:
        bad("build_system",
            f"拼出来只有 {len(built)} 字（<{MIN_SYSTEM}）——"
            f"这张卡装上等于没有人格")
    elif has_bad:
        rep["ok"] = False
    else:
        rep["ok"] = True

    rep["score"] = max(0, 100 - sum(30 if i["level"] == "bad" else 8
                                    for i in issues))
    return rep


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

_SAFE = re.compile(r"[^\w一-鿿.-]+")


#: 从商店下载的卡，文件名加这个前缀。
#:
#: 🔴 为什么加前缀而不是另开目录：
#:   切换（`persona_card.set_active`）按**文件名**定位卡，另开目录就得让
#:   active.json 记住「来自哪个目录」，改动面更大。
#:   加前缀则切换逻辑一行不用动，而且 git 能靠 `cards/store_*.png` 精确忽略
#:   ——主人自己手写的 PNG 卡照样能入库。
STORE_PREFIX = "store_"


def safe_name(title, fallback="card"):
    """把标题变成安全的文件名（保留中文，去掉路径分隔符与控制字符）。"""
    s = _SAFE.sub("_", (title or "").strip())[:60].strip("._")
    return s or fallback


def ledger_path(card_dir=None):
    d = card_dir or _default_card_dir()
    return os.path.join(d, LEDGER_NAME)


def load_ledger(card_dir=None):
    p = ledger_path(card_dir)
    if not os.path.exists(p):
        return {}
    try:
        obj = json.load(open(p, encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_ledger(obj, card_dir=None):
    d = card_dir or _default_card_dir()
    os.makedirs(d, exist_ok=True)
    tmp = ledger_path(card_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ledger_path(card_dir))


def note_download(card_dir, source, card_id, path):
    """记一笔「这张下过了」，用于 30 分钟冷却。"""
    led = load_ledger(card_dir)
    key = f"{source}:{card_id}"
    led[key] = {"at": time.time(), "file": os.path.basename(path)}
    save_ledger(led, card_dir)


def cooldown_left(card_dir, source, card_id, window=1800):
    """这张卡还剩多少秒冷却。没下过返回 0。"""
    rec = load_ledger(card_dir).get(f"{source}:{card_id}")
    if not rec:
        return 0
    left = window - (time.time() - rec.get("at", 0))
    return int(left) if left > 0 else 0


# --------------------------------------------------------------------------
# 高层动作
# --------------------------------------------------------------------------

def fetch_card(source, card_id, card_dir=None, force=False):
    """从源下载一张卡到 card_dir，落盘前先落临时文件、体检合格才入库。

    返回 {"ok", "file", "path", "lint", "bytes"} 或 {"ok":False,"err":...}。
    """
    if source not in SOURCES:
        return {"ok": False, "err": f"没有这个卡源：{source}"
                               f"（可用的：{', '.join(SOURCES)}）"}
    d = card_dir or _default_card_dir()
    os.makedirs(d, exist_ok=True)

    left = cooldown_left(d, source, card_id)
    if left and not force:
        return {"ok": False,
                "err": f"这张卡刚下过，冷却还剩 {left // 60} 分 {left % 60} 秒"
                       f"（站点同卡 30 分钟冷却；真要再下就带 force=true）"}

    try:
        url, title = SOURCES[source]["url"](card_id)
    except SourceError as e:
        return {"ok": False, "err": f"拿不到下载地址：{e}"}
    except Exception as e:
        return {"ok": False, "err": f"拿不到下载地址：{type(e).__name__}: {e}"}

    fn = STORE_PREFIX + safe_name(title, f"{source}_{card_id}")
    dest = os.path.join(d, fn + ".png")
    try:
        info = download(url, dest)
    except SourceError as e:
        return {"ok": False, "err": f"下载失败：{e}"}
    except Exception as e:
        return {"ok": False, "err": f"下载失败：{type(e).__name__}: {e}"}

    rep = lint_card(dest)
    if not rep.get("ok"):
        # 🔴 体检不过就不入库：宁可没有这张卡，也不要一个「装着但不会说话」的她。
        try:
            os.remove(dest)
        except OSError:
            pass
        return {"ok": False, "file": fn + ".png", "lint": rep,
                "err": "这张卡没通过体检，没入库：" + "；".join(
                    i["why"] for i in rep.get("issues", [])[:4])}

    note_download(d, source, card_id, dest)
    return {"ok": True, "file": fn + ".png", "path": dest,
            "bytes": info.get("bytes"), "lint": rep}


def inbox_dir(card_dir=None):
    d = card_dir or _default_card_dir()
    parent = os.path.dirname(os.path.abspath(d))
    return os.path.join(parent, INBOX_DIRNAME)


def ingest_inbox(card_dir=None, move=True):
    """把本地投递口里的卡体检后入库。

    这是**不可程序化拉的源**（Chub / JanitorAI / Discord）的唯一入口：
    主人自己下好的文件扔进 `cards_inbox/`，本仓负责体检、入库、（可选）移走原件。

    返回 {"ok", "inbox", "items":[{file, ok, lint, err}]}。
    """
    d = card_dir or _default_card_dir()
    os.makedirs(d, exist_ok=True)
    inbox = inbox_dir(d)
    os.makedirs(inbox, exist_ok=True)
    items = []
    for fn in sorted(os.listdir(inbox)):
        if fn.startswith("."):
            continue
        if not fn.lower().endswith((".json", ".png", ".charx")):
            continue
        src = os.path.join(inbox, fn)
        if not os.path.isfile(src):
            continue
        rep = lint_card(src)
        # 🔴 `file` 恒为**投递口里的原名**（这才是主人认识的那个名字），
        #    落库后的名字放 `stored_as`。
        #    之前 `file` 有时是原名、有时是库名（好卡才改），同一个列表里
        #    两种含义 ⇒ 调用方没法判断哪个是哪个。
        row = {"file": fn, "stored_as": "", "ok": False, "lint": rep, "err": ""}
        if not rep.get("ok"):
            row["err"] = "没通过体检：" + "；".join(
                i["why"] for i in rep.get("issues", [])[:4])
            items.append(row)
            continue
        base = STORE_PREFIX + os.path.splitext(fn)[0]
        dest = os.path.join(d, base + os.path.splitext(fn)[1])
        if os.path.abspath(dest) == os.path.abspath(src):
            # 已经在库里了
            row["ok"] = True
            row["stored_as"] = os.path.basename(dest)
            items.append(row)
            continue
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(d, f"{base}_{n}{os.path.splitext(fn)[1]}")
            n += 1
        try:
            if move:
                shutil.move(src, dest)
            else:
                shutil.copy2(src, dest)
        except Exception as e:
            row["err"] = f"入库失败：{type(e).__name__}: {e}"
            items.append(row)
            continue
        row["ok"] = True
        row["stored_as"] = os.path.basename(dest)
        row["lint"] = lint_card(dest)
        items.append(row)
    return {"ok": True, "inbox": inbox, "items": items}


def search(source, query="", skip=0, limit=24):
    """搜一个源。失败抛 SourceError（消息即原因）。"""
    if source not in SOURCES:
        raise SourceError(f"没有这个卡源：{source}"
                          f"（可用的：{', '.join(SOURCES)}）")
    return SOURCES[source]["list"](query, skip, limit)


# --------------------------------------------------------------------------
# 自测（真跑真网，可离线跑）
# --------------------------------------------------------------------------

def _selftest():
    import persona_card as pc

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "cardstore_test")
    os.makedirs(tmp, exist_ok=True)
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [ok] {label}")
        else:
            fail += 1
            print(f"  [FAIL] {label}")

    # ---- ① 坏卡体检：必须报明确原因，不能静默通过 ----
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"spec": "some_unknown_schema", "foo": 1}')
    rep = lint_card(bad)
    check(rep["ok"] is False, "陌生 schema 的卡体检不通过")
    check(any("不认" in i["why"] for i in rep["issues"]),
          "体检报告说清了为什么不认（不是「读不出来」）")

    # ---- ② 空壳卡（V1 但没内容）：build_system 太短 ⇒ 判不过 ----
    hollow = os.path.join(tmp, "hollow.json")
    with open(hollow, "w", encoding="utf-8") as f:
        json.dump({"name": "空壳"}, f, ensure_ascii=False)
    rep = lint_card(hollow)
    check(rep["ok"] is False, "空壳卡体检不通过")
    check(rep["stats"]["desc"] == 0, "空壳卡 description 长度 = 0")

    # ---- ③ 本仓真卡：必须通过 ----
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.join(here, "cards", "大肥鱼.json")
    if os.path.exists(real):
        rep = lint_card(real)
        check(rep["ok"] is True, "本仓真卡体检通过")
        check(rep["built_chars"] > MIN_SYSTEM,
              f"真卡拼出人设 {rep.get('built_chars')} 字（>={MIN_SYSTEM}）")
    else:
        print("  [skip] 本仓 cards/大肥鱼.json 不在，跳过真卡体检")

    # ---- ④ PNG 完整性判定 ----
    check(_png_complete(b"\x89PNG\r\n\x1a\n" + b"x" * 10)[0] is False,
          "假 PNG 被判为不完整")
    check(_png_complete(b"not a png")[0] is False, "非 PNG 被判为不完整")

    # ---- ⑤ 文件名安全化 ----
    check("/" not in safe_name("../../etc/passwd"), "文件名里的路径分隔符被清掉")
    check(safe_name("") == "card", "空标题有兜底名")

    # ---- ⑥ 冷却账 ----
    led_dir = os.path.join(tmp, "led")
    os.makedirs(led_dir, exist_ok=True)
    note_download(led_dir, "aicc", 123, os.path.join(led_dir, "x.png"))
    check(cooldown_left(led_dir, "aicc", 123) > 1700, "下过的卡有 30 分钟冷却")
    check(cooldown_left(led_dir, "aicc", 999) == 0, "没下过的卡没有冷却")

    # ---- ⑦ 投递口入库：坏卡不许混进库 ----
    cards_dir = os.path.join(tmp, "cards")
    os.makedirs(cards_dir, exist_ok=True)
    inbox = inbox_dir(cards_dir)
    os.makedirs(inbox, exist_ok=True)
    with open(os.path.join(inbox, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    with open(os.path.join(inbox, "hollow2.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "空壳"}, f, ensure_ascii=False)
    res = ingest_inbox(cards_dir)
    check(all(not i["ok"] for i in res["items"]), "坏卡与空壳卡都没入库")
    check(not any(f.lower().endswith(".json")
                  for f in os.listdir(cards_dir)), "库目录里没混进坏卡")
    check(all(i["stored_as"] == "" for i in res["items"] if not i["ok"]),
          "没入库的卡没有落库名（file 恒为原名）")

    # ---- ⑧ 源清单 ----
    srcs = list_sources()
    check(len(srcs) >= 1, "至少列出一个卡源")
    check(all(s.get("name") and s.get("note") for s in srcs), "每个源都有名字与说明")

    print(f"[card_store] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
