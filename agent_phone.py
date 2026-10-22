"""让模型自己看屏幕、自己决定点哪里。

这是"她"的第一层：不是我说"按等于"，而是给一个任务，模型自己从可见控件里挑。
每一轮都是：读屏 → 控件清单 → 模型给一个动作 → 执行 → 回读核对 → 再问。

模型的权限被限死：只认 tap / type / back / home / done 五种动作，碰不到 shell、
文件、网络。它唯一能做的事就是"按屏幕上已有的字"。
"""
import json
import os
import re
import sys
import time
import urllib.request

import android_device as A
import phone_ops as P

MODEL = "dots-studio/dots-3-note-preview:free"
MODEL_CACHE = os.path.join(os.path.expanduser("~"), ".fleet", "agent_model.json")
DEFAULT_PORT = 18900

SYSTEM = """你是手机操作员。你每次只能做一个动作，从下面可见控件里挑。
只输出一行 JSON，不要解释、不要代码块：

{"action":"tap","label":"控件上原样的字"}
{"action":"tap_id","id":"控件的 ID"}
{"action":"set_text","target":"先点的控件（文字或 ID）","text":"要输入的内容"}
{"action":"set_seek","target":"滑条那行的字或 ID","percent":72}
{"action":"scroll","direction":"down"}
{"action":"back"}
{"action":"home"}
{"action":"open","package":"com.android.settings"}
{"action":"wait","text":"要等它出现的字"}
{"action":"done","answer":"任务完成时的结果"}

规则：
- 每次只推进一步，别想一次点完
- label / id / target 必须是清单里原样出现的值，不要自己编、不要写坐标
- 清单里带 [可点] 的才点得动；不带的多半是容器或纯文字，别点
- 要输入内容用 set_text，一次就能把字填进去；target 填那个输入框那行的字或 ID
- 滑条（亮度、音量这类）用 set_seek，percent 是**滑条自己的位置 0~100**，
  不等于它控制的东西的百分比。实测这颗 MIUI 亮度条：72≈亮度30%、80≈41%、90≈64%、100=100%
- 以「= 」开头的是结果显示，不是按钮，不要点它；看到它说明已经算出来了，
  直接把那个数抄进 done 的 answer
- 不许自己心算或推断答案：结果只能来自屏幕上真实显示的内容，
  你能算出来也必须点出来，否则算失败
- 界面上没有你要的字，就先 scroll 往下找，或 back/home 换界面，
  或直接 done 说明做不到
- 任务完成后才用 done，answer 里抄屏幕上显示的那个结果"""

# 动作白名单：模型只能从这 8 里挑，碰不到 shell / 文件 / 网络。
ACTIONS = ("tap", "tap_id", "set_text", "set_seek", "scroll", "back", "home", "open", "wait")


def settings():
    p = os.path.expanduser("~/.our-free-model/settings.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def endpoint():
    st = settings()
    port = st.get("standalonePort") or DEFAULT_PORT
    return f"http://127.0.0.1:{port}/v1/chat/completions"


def api_key():
    k = os.environ.get("OFM_KEY", "").strip()
    return k or settings().get("forwardKey", "")


def call(model, messages, max_tokens=160, timeout=90):
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0}
    req = urllib.request.Request(
        endpoint(), data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"{e.get('code')}: {str(e.get('message'))[:90]}")
    return data["choices"][0]["message"].get("content") or ""


def list_models(timeout=20):
    url = endpoint().replace("/chat/completions", "/models")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [m["id"] for m in data.get("data", []) if m.get("id")]


def probe(models=None, workers=6, timeout=30):
    """把模型逐个真调一次。列表里有不等于能出字——上游会超时、会限流、
    会地区封锁，必须实测。并发跑，卡住的不拖累其他。"""
    from concurrent.futures import ThreadPoolExecutor
    models = models or list_models()

    def one(m):
        t0 = time.time()
        try:
            txt = call(m, [{"role": "user", "content": "回一个字：好"}],
                       max_tokens=8, timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            if not txt.strip():
                return {"model": m, "ok": False, "ms": ms, "err": "空响应"}
            return {"model": m, "ok": True, "ms": ms, "sample": txt.strip()[:12]}
        except Exception as e:
            return {"model": m, "ok": False, "ms": int((time.time() - t0) * 1000),
                    "err": str(e)[:70]}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, models))
    rows.sort(key=lambda r: (not r["ok"], r.get("ms", 9e9)))
    good = [r["model"] for r in rows if r["ok"]]
    try:
        os.makedirs(os.path.dirname(MODEL_CACHE), exist_ok=True)
        with open(MODEL_CACHE, "w", encoding="utf-8") as f:
            json.dump({"at": int(time.time()), "usable": good, "detail": rows}, f,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass
    return rows


def usable_models():
    """可用模型清单：先看缓存，没缓存就现场探一次。按 PREFER 排优先级。"""
    try:
        with open(MODEL_CACHE, encoding="utf-8") as f:
            good = json.load(f).get("usable") or []
    except (OSError, ValueError):
        good = []
    if not good:
        good = [r["model"] for r in probe() if r["ok"]]
    return sorted(good, key=lambda m: PREFER.index(m) if m in PREFER else 99)


# 实测能出字的里面，按"能不能听话"排：参数大、指令跟随好的优先。
# 不按延迟排——最快的 cohere/north-mini-code 是代码模型，做 UI 决策容易跑偏。
PREFER = [
    # 实测：nemotron 系是 reasoning 模型，爱把思考过程一起吐出来，输出不是纯 JSON，
    # 单步动作这种"只回一行"的场合会被解析器丢掉。所以把非 reasoning 的放前面。
    "stealth/glyph-cluster",
    "kilo-auto/free",
    "openrouter/free",
    "poolside/laguna-s-2.1:free",
    "reflection/beam-free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]


def pick_model():
    good = usable_models()
    for m in PREFER:
        if m in good:
            return m
    return good[0] if good else MODEL


def parse_action(text):
    """从模型输出里抠出第一个 JSON 对象。

    模型爱包代码块、爱多打一个右括号（实测出现过 {"action":...}}），
    所以用 raw_decode 只吃第一个完整对象，尾部垃圾直接忽略。
    """
    i = text.find("{")
    if i < 0:
        return None
    try:
        act, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return None
    return act if isinstance(act, dict) and "action" in act else None


def screen_brief(serial, limit=40):
    """把界面压成模型能读的清单：一行一个可操作元素，带 ID 和「可点」标记。

    读屏走编队桥直读（毫秒级），不再用 uiautomator dump——后者每次 1~3 秒，
    一步一读的话光读屏就把一个任务拖到几十秒。

    返回 (前台包, 清单行, 输入框当前内容, 错误)。
    """
    sc = A.bridge_screen(serial, max_lines=300)
    if not sc["ok"]:
        return "", [], "", sc.get("err", "")
    items, seen = [], {}
    for n in sc["nodes"]:
        t = (n["text"] or n["desc"]).strip()
        vid = n["id"]
        if not t and not vid:
            continue
        if not t and not n["clickable"]:
            continue                      # 有 id 没字又不可点：纯容器，操作不了
        short = vid.rsplit("/", 1)[-1] if vid else ""
        key = (t, short)
        seen[key] = seen.get(key, 0) + 1
        row = "- " + (t or short or n["cls"])
        if short:
            row += f"  [id:{short}]"
        if n["clickable"]:
            row += "  [可点]"
        if seen[key] > 1:
            row += f"  (第{seen[key]}个)"
        items.append(row)
        if len(items) >= limit:
            break
    val = P.read_value(serial)
    value = (val.get("value") or "") if val.get("ok") else ""
    return sc["pkg"], items, value, ""


def _ambiguous_n(err):
    m = re.search(r"ambiguous (\d+)", err or "")
    return int(m.group(1)) if m else 0


def _tap_el(serial, kind, value, what):
    """按元素点。同名/同 id 多个时取视觉顺序第一个，但把这件事写进 note——
    不静默，也不含糊：调用方和日志都能看见"为什么点的是这一个"。
    """
    r = A.bridge_tap_el(serial, kind, value)
    if not r["ok"]:
        n = _ambiguous_n(r.get("err", ""))
        if n:
            r = A.bridge_tap_el(serial, kind, value, index=1)
            if r["ok"]:
                r["note"] = f"界面上有 {n} 个{what}「{value}」，点了最上面那个"
                r["changed"] = True
                return r
        r["changed"] = False
        return r
    r["changed"] = True
    return r


def _set_text(serial, target, text):
    """点一下输入框拿焦点，再把文本整体设进去。

    为什么不直接 ACTION_SET_TEXT：多数输入框在未聚焦时会被拒（text-rejected），
    而"先点后设"是有序的两步，哪步失败都在结果里说清楚。
    """
    target = target.strip()
    if not target:
        return {"ok": False, "changed": False,
                "err": "set_text 需要 target：先说清点哪个输入框（用清单里的字或 ID）"}
    if ":" in target or "/" in target:
        loc = _tap_el(serial, "id", target, " ID ")
        kind = "id"
    else:
        loc = _tap_el(serial, "text", target, "控件")
        kind = "text"
        if not loc["ok"] and "not-found" in loc.get("err", ""):
            # 模型可能直接把短 id 当 target 抄过来（清单里是 [id:btn_send] 这种），
            # 按 id 再解析一次。两种都是精确寻址，不会点到别处。
            alt = A.bridge_tap_el(serial, "id", target)
            if alt["ok"]:
                loc, kind = alt, "id"
                loc["changed"] = True
    if not loc["ok"]:
        return {"ok": False, "changed": False,
                "err": "找不到输入框：" + str(loc.get("err", ""))[:120]}
    time.sleep(0.25)
    r = A.bridge_set_text(serial, kind, target, text)
    if not r["ok"] and kind == "text":
        r = A.bridge_set_text(serial, "id", target, text)
    r["changed"] = True
    if loc.get("note"):
        r["note"] = loc["note"]
    return r


def _set_seek(serial, target, percent):
    """把滑条设到控件区间的 percent%。

    不先点：ACTION_SET_PROGRESS 不要求焦点，先点反而可能点到旁边的开关
    （亮度条紧挨着"自动调整亮度"）。三种寻址依次试 id → desc → text，
    因为滑条本体通常只有 ID 和 contentDescription，没有 text。
    """
    target = str(target if target is not None else "").strip()
    if not target:
        return {"ok": False, "changed": False,
                "err": "set_seek 需要 target：滑条那行的字或它的 ID"}
    try:
        pct = int(round(float(str(percent).strip().rstrip("%"))))
    except (TypeError, ValueError):
        return {"ok": False, "changed": False,
                "err": "set_seek 的 percent 要是 0~100 的数字，收到：" + str(percent)}
    pct = max(0, min(100, pct))
    r = A.bridge_set_seek(serial, "id", target, pct)
    for kind in ("desc", "text"):
        if r["ok"] or "not-found" not in r.get("err", ""):
            break
        r = A.bridge_set_seek(serial, kind, target, pct)
    r["changed"] = bool(r["ok"])
    return r


def _wait_text(serial, want, timeout=8.0):
    """等某段字出现在界面上（比死睡固定秒数靠谱：真的等到了才算）。"""
    if not want:
        return {"ok": False, "changed": False, "err": "wait 需要 text"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        sc = A.bridge_screen(serial, max_lines=300)
        if sc["ok"]:
            for n in sc["nodes"]:
                if want in n["text"] or want in n["desc"] or want == n["id"]:
                    return {"ok": True, "changed": True, "err": "", "hit": n["cls"]}
        time.sleep(0.4)
    return {"ok": False, "changed": False, "err": f"{timeout}s 内界面上没等到「{want}」"}


def do(serial, act):
    """执行一个动作。白名单之外的一律拒绝，模型碰不到 shell/文件/网络。"""
    a = str(act.get("action", "")).strip().lower()
    if a not in ACTIONS:
        return {"ok": False, "changed": False,
                "err": f"不允许的动作：{a or '(空)'}；只能用 {' / '.join(ACTIONS)}"}

    if a == "tap":
        label = str(act.get("label", "")).strip()
        if not label:
            return {"ok": False, "changed": False, "err": "tap 需要 label"}
        return _tap_el(serial, "text", label, "控件")
    if a == "tap_id":
        vid = str(act.get("id", "")).strip()
        if not vid:
            return {"ok": False, "changed": False, "err": "tap_id 需要 id"}
        return _tap_el(serial, "id", vid, " ID ")
    if a == "set_text":
        return _set_text(serial, str(act.get("target", "")), str(act.get("text", "")))
    if a == "set_seek":
        return _set_seek(serial, act.get("target", ""), act.get("percent", ""))
    if a == "scroll":
        r = A.bridge_scroll(serial, str(act.get("direction", "down")))
        r["changed"] = bool(r["ok"])
        return r
    if a == "wait":
        return _wait_text(serial, str(act.get("text", "")).strip())
    if a == "back":
        r = A.bridge_cmd(serial, "BACK")
        return {"ok": r["ok"], "changed": True, "err": r.get("err", "")}
    if a == "home":
        r = A.bridge_cmd(serial, "HOME")
        return {"ok": r["ok"], "changed": True, "err": r.get("err", "")}
    # open
    pkg = str(act.get("package", "")).strip()
    if not pkg:
        return {"ok": False, "changed": False, "err": "open 需要 package"}
    r = A.launch(serial, pkg)
    return {"ok": bool(r["ok"]), "changed": True,
            "err": "" if r["ok"] else str(r.get("out", ""))[:120]}


def solve(serial, task, model="", max_steps=14, verbose=True):
    """跑一轮完整任务。撞到步数上限就停，不无限点。

    模型抽风给空响应时换下一个可用模型重试，别让一次空回复把整个任务判死。
    """
    models = usable_models() or [MODEL]
    if model:
        models = [model] + [m for m in models if m != model]
    log, done_steps = [], []
    feedback = "（还没开始）"
    repeat = 0
    fail_streak = 0
    prev_act = None
    for step in range(1, max_steps + 1):
        fg, items, value, rerr = screen_brief(serial)
        if not items:
            # 读不到界面就停：没有清单，模型只能瞎猜，跟着瞎点比不点更糟
            log.append({"step": step, "err": "读不到界面，停止", "detail": rerr[:160]})
            break
        history = "；".join(done_steps[-8:]) or "（无）"
        user = (f"任务：{task}\n当前应用：{fg}\n"
                f"输入框当前内容：{value or '（空）'}\n"
                f"已做过的动作：{history}\n"
                f"可见控件：\n" + "\n".join(items[:40]) +
                f"\n上一步结果：{feedback}\n"
                f"先根据输入框内容判断进展到哪了，别重复已经做过的动作。\n"
                f"下一步动作（只输出 JSON）：")
        msg = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        raw, used, errs = "", models[0], []
        for attempt in range(min(3, len(models))):
            m = models[0]
            try:
                got = call(m, msg)
            except Exception as e:
                errs.append(f"{m}: {str(e)[:60]}")
                got = ""
            if got.strip():
                raw, used = got, m
                break
            models.append(models.pop(0))     # 空/失败就轮到下一个
        if not raw.strip():
            log.append({"step": step, "err": "所有可用模型都没给内容", "tried": errs})
            break
        act = parse_action(raw)
        if not act:
            # 模型偶尔会先写一段思考/忘掉格式（实测出现过整段英文推理）：
            # 把它的原话贴回去，逼它只交 JSON，给它一次补救机会
            try:
                raw2 = call(used, msg + [
                    {"role": "assistant", "content": raw[:400]},
                    {"role": "user", "content": "格式不对。只输出一行 JSON 动作，"
                                                "不要解释、不要写思考过程。"}])
                act = parse_action(raw2)
                if act:
                    raw = raw2
            except Exception:
                pass
        if not act:
            log.append({"step": step, "err": "模型没给出可解析的 JSON",
                        "model": used, "raw": raw[:120]})
            break
        if verbose:
            print(f"  [{step}] {used.split('/')[-1]}  {json.dumps(act, ensure_ascii=False)}",
                  flush=True)
        if str(act.get("action", "")).lower() == "done":
            log.append({"step": step, "done": act.get("answer", ""), "model": used})
            break
        # 同一个动作连着来两遍、界面又没变化，就是卡住了，别再烧 token
        repeat = repeat + 1 if act == prev_act else 0
        prev_act = act
        if repeat >= 2:
            log.append({"step": step, "err": "同一动作重复 3 次，判为卡死，停止",
                        "action": act})
            break
        r = do(serial, act)
        ok = bool(r.get("ok"))
        changed = r.get("changed")
        feedback = ("成功" if ok else "失败：" + str(r.get("err", ""))[:80]) + \
                   ("，界面已变化" if changed else "，界面没变化（这一下可能没点到）")
        if r.get("note"):
            feedback += "（" + str(r["note"]) + "）"
        step_desc = act.get("action", "")
        for k in ("label", "id", "target", "text", "percent", "direction", "package"):
            if act.get(k):
                step_desc += " " + str(act[k])
        done_steps.append(step_desc.strip())
        log.append({"step": step, "action": act, "ok": ok, "changed": changed,
                    "model": used, "note": feedback})
        # 单步失败不判死：动作多了以后「这次没找到」是很正常的反馈，
        # 该让模型看了错误自己换招。连错 3 次才是真卡住。
        fail_streak = 0 if ok else fail_streak + 1
        if fail_streak >= 3:
            log.append({"step": step, "err": f"连续 {fail_streak} 次动作失败，停止"})
            break
        time.sleep(0.3)
    return log


def _selftest():
    # ① 正常 JSON
    a = parse_action('{"action":"tap","label":"7"}')
    assert a == {"action": "tap", "label": "7"}, a

    # ② 多打一个右括号（实测 cohere 真这么回过）
    a = parse_action('{"action":"tap","label":"1"}}')
    assert a and a["label"] == "1", a

    # ③ 包在代码块里
    a = parse_action('```json\n{"action":"done","answer":"408"}\n```')
    assert a and a["answer"] == "408", a

    # ④ 前面带一段中文解释，后面还有废话
    a = parse_action('好的，我点一下：{"action":"tap","label":"乘"} 这样可以。')
    assert a and a["label"] == "乘", a

    # ⑤ 纯思考文本、空串、缺 action 键 -> 都要判成没有动作，不能瞎点
    assert parse_action("The user wants me to calculate 7 + 8 ...") is None
    assert parse_action("") is None
    assert parse_action('{"label":"7"}') is None

    # ⑥ 端点与令牌从本地服务的配置里读，不硬编码
    st = settings()
    assert endpoint().startswith("http://127.0.0.1:")
    assert endpoint().endswith("/v1/chat/completions")
    if st:
        assert api_key(), "settings.json 存在时应能读到 forwardKey"

    # ⑦ 偏好表里的模型必须都是探到过的名字，别写成瞎编的
    #    形状按探测返回的**真格式**：`vendor/model`（斜杠分隔，可再带 `:free`
    #    后缀）。原断言写成 `含 ":" 或以 -free 结尾` 是拍脑袋格式——
    #    实测缓存里的真名是 `stealth/glyph-cluster`、`kilo-auto/free` 这种，
    #    既没冒号也不是 -free 结尾，把真名字全毙了（2026-10-22 误报）。
    #    "真的能出字"由 probe() 现场逐个调验证，不在这条形状检查里假装能验。
    for m in PREFER:
        assert "/" in m and not m.startswith("/") and not m.endswith("/"), m

    # ⑧ 白名单之外的动作一律拒绝——模型碰不到 shell / 文件 / 网络。
    #    用假 serial：这些分支在校验阶段就返回，根本不会去碰设备。
    for bad in ("shell", "exec", "press", "type", "done", ""):
        r = do("FAKE-SERIAL", {"action": bad, "cmd": "reboot"})
        assert r["ok"] is False and "\u4e0d\u5141\u8bb8\u7684\u52a8\u4f5c" in r["err"], (bad, r)

    # ⑨ 参数缺失必须在校验阶段说清楚，不能拿着空参数去碰设备
    assert "\u9700\u8981 label" in do("FAKE-SERIAL", {"action": "tap"})["err"]
    assert "\u9700\u8981 id" in do("FAKE-SERIAL", {"action": "tap_id"})["err"]
    assert "\u9700\u8981 target" in do("FAKE-SERIAL", {"action": "set_text", "text": "x"})["err"]
    assert "\u9700\u8981 text" in do("FAKE-SERIAL", {"action": "wait"})["err"]
    assert "\u9700\u8981 package" in do("FAKE-SERIAL", {"action": "open"})["err"]
    assert "up/down/left/right" in do("FAKE-SERIAL",
                                      {"action": "scroll", "direction": "\u4fa7\u6ed1"})["err"]

    print("agent_phone \u81ea\u6d4b\uff1a9 \u9879\u5168\u8fc7")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "probe":
        rows = probe()
        ok = [r for r in rows if r["ok"]]
        for r in rows:
            mark = "+" if r["ok"] else "-"
            note = r.get("sample") or r.get("err", "")
            print(f"{mark} {r['ms']:>6}ms  {r['model']:<44} {note}")
        print(f"\n能出字 {len(ok)}/{len(rows)}")
        return 0

    d = A.attach()
    if not d["ok"]:
        print("设备接不上：", d.get("err"), file=sys.stderr)
        return 2
    if not argv:
        print(__doc__)
        return 1
    task = " ".join(argv)
    use = pick_model()
    print(f"任务：{task}\n模型：{use}\n")
    log = solve(d["serial"], task, model=use)
    print("\n轨迹：")
    print(json.dumps(log, ensure_ascii=False, indent=2))
    last = log[-1] if log else {}
    return 0 if "done" in last else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
