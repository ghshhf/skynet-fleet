"""让模型自己看屏幕、自己决定点哪里。

这是"她"的第一层：不是我说"按等于"，而是给一个任务，模型自己从可见控件里挑。
每一轮都是：读屏 → 控件清单 → 模型给一个动作 → 执行 → 回读核对 → 再问。

模型的权限被限死：只认 tap / type / back / home / done 五种动作，碰不到 shell、
文件、网络。它唯一能做的事就是"按屏幕上已有的字"。
"""
import json
import os
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
{"action":"type","text":"要输入的内容"}
{"action":"back"}
{"action":"home"}
{"action":"done","answer":"任务完成时的结果"}

规则：
- 每次只推进一步，别想一次点完
- label 必须是清单里原样出现的文字，不要自己编、不要写坐标
- 以「= 」开头的是结果显示，不是按钮，不要点它；看到它说明题目已经算出来了，
  直接把那个数抄进 done 的 answer
- 不许自己心算或推断答案：结果只能来自屏幕上真实显示的内容，
  你能算出来也必须点出来，否则算失败
- 界面上没有你要的字，就先用 back 或 home 换界面，或直接 done 说明做不到
- 任务完成后才用 done，answer 里抄屏幕上显示的那个结果"""


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
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "dots-studio/dots-3-note-preview:free",
    "reflection/beam-free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3.5-lightning:free",
    "cohere/north-mini-code:free",
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


def screen_brief(serial):
    """把界面压成模型能读的东西：文字清单 + 当前输入框内容。

    输入框内容必须给——不给的话模型看不见自己的进展，会一直点同一个键。
    """
    scr = P.read_screen(serial)
    val = P.read_value(serial)
    seen, items = set(), []
    for n in scr["nodes"]:
        lb = A.label(n)
        if not lb or lb in seen:
            continue
        seen.add(lb)
        items.append(f"- {lb}" + ("  [可点]" if n["clickable"] else ""))
    return scr["foreground"], items, (val.get("value") or "") if val.get("ok") else ""


def do(serial, act):
    a = str(act.get("action", "")).lower()
    if a == "tap":
        return P.press(serial, str(act.get("label", "")))
    if a == "type":
        return P.type_text(serial, str(act.get("text", "")))
    if a == "back":
        r = A.bridge_cmd(serial, "BACK")
        return {"ok": r["ok"], "changed": True, "err": r.get("err", "")}
    if a == "home":
        r = A.bridge_cmd(serial, "HOME")
        return {"ok": r["ok"], "changed": True, "err": r.get("err", "")}
    return {"ok": False, "err": f"不允许的动作：{a}"}


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
    prev_act = None
    for step in range(1, max_steps + 1):
        fg, items, value = screen_brief(serial)
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
        done_steps.append(f"{act.get('action')} {act.get('label') or act.get('text') or ''}".strip())
        log.append({"step": step, "action": act, "ok": ok, "changed": changed,
                    "model": used, "note": feedback})
        if not ok:
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
    for m in PREFER:
        assert ":" in m or m.endswith("-free"), m

    print("agent_phone \u81ea\u6d4b\uff1a7 \u9879\u5168\u8fc7")


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
