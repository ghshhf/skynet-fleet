"""AI：终端里的免费模型层。

这一层把「免费 AI」当成一台设备来管，跟其它设备走同一条路：
注册表登记 → 闸门 → 结果信封 → 终端两栏同时显形。能力名 domain 用 `ai.*`。

为什么 pollinations 免 key 是默认：学 Cherry 把免费端点当一等公民——零配置开箱即用，
不至于「装好才知道一个模型都用不了」。本地模型（openai 兼容端点）有 FLEET_LLM_URL
才活；llm7 有 token 才活。外部 token 永远从环境变量读。

多模型同问（学 Cherry 的「多模型对比」+ 话题内 executions 并行）：`ai.chat` 默认把
问题并发丢给所有可用后端，按 [后端id] 同屏——人一眼看出谁答得更好。长任务流式回流
走 `_on_line`（跟 web.* 同一条管道），界面才有真进度。

后端清单与探测见 `ai_backends.py`（四元组：id / protocol / base_url / model）。
"""
import threading
import time

from .. import ai_backends as AB
from ..core import BaseDevice, DeviceResult as R

# 🔴 默认 max_tokens 为什么是 2048（2026-10-21 实测抓到的坑）：
#   minimax-m3.1-flash 这类**推理模型**会先花一大截 token 思考，再吐正文。
#   带 system prompt 时 max_tokens=300 → finish_reason=length、正文 0 字符
#   （界面看着像「模型没话说」，其实是预算被推理烧光）；
#   同样参数 1024/2048 → finish_reason=stop，正文正常。
#   原默认 512 正好落在「够不着正文」的区间里 ⇒ 默认给 2048。
DEFAULT_MAX_TOKENS = 2048


class AI(BaseDevice):
    kind = "ai"
    via = "web"

    def __init__(self, name="ai:free", **kw):
        super().__init__(name, via="web", **kw)
        self.meta.setdefault("layer", "ai")

    def probe(self):
        bs = AB.backends()
        if not bs:
            return False, ("没有任何可用后端：联网状态下 pollinations（免key）会自动在；"
                           "本地模型需设 FLEET_LLM_URL")
        return True, ""

    def caps(self):
        return ("ai.list", "ai.status", "ai.complete", "ai.chat")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "ai.list":
            return self._list(args)
        if cap == "ai.status":
            return self._status(args)
        if cap == "ai.complete":
            return self._complete(args)
        if cap == "ai.chat":
            return self._chat(args)
        return super().invoke(cap, args)

    # ---------------- 只读 ----------------

    def _list(self, args):
        # 每个端点附带健康/冷却状态，让调用方一眼看出谁当前可用
        out = []
        for b in AB.backends():
            cd = AB.cooldown_remaining(b["id"])
            out.append({"id": b["id"], "title": b["title"],
                        "healthy": cd <= 0, "cooldown_sec": cd})
        return R.success("ai.list", self.name, via=self.via, data={"backends": out})

    def _status(self, args):
        """逐个后端真发请求探测连通（学 Cherry 的「检测」按钮 = probe 契约）。"""
        out = []
        for b in AB.backends():
            ok, reason = AB.probe(b)
            out.append({"id": b["id"], "title": b["title"],
                        "ok": ok, "reason": reason})
        return R.success("ai.status", self.name, via=self.via,
                         data={"backends": out})

    # ---------------- 动手（流式）----------------

    def _complete(self, args):
        """向一个后端要一段完整回复（不流式）。走 failover：依次试候选端点，
        第一个产出正文的胜出，切换对调用方不可见（只在 backend 字段暴露实际用了谁）。"""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return R.error("ai.complete", "prompt 不能为空",
                           device=self.name, via=self.via)
        bs = self._pick(args.get("backends") or "")
        if not bs:
            spec = (args.get("backends") or "").strip()
            have = ", ".join(b["id"] for b in AB.backends()) or "(无)"
            why = (f"后端「{spec}」不存在或无健康端点，现有可用：{have}"
                   if spec else "没有可用（健康）后端")
            return R.unavailable("ai.complete", why, device=self.name, via=self.via)
        try:
            text, via = AB.chat_fallback(
                bs, prompt, system=args.get("system") or "",
                max_tokens=int(args.get("max_tokens") or DEFAULT_MAX_TOKENS))
        except Exception as e:
            return R.error("ai.complete", f"全部后端失败: {e}",
                           device=self.name, via=self.via)
        if not (text or "").strip():
            return R.unavailable("ai.complete",
                                 f"{via} 空回：这次流里没有正文（上游临时抽风）",
                                 device=self.name, via=self.via)
        return R.success("ai.complete", self.name, side_effect=True, via=self.via,
                         data={"backend": via, "text": text})

    def _chat(self, args):
        """流式对话。默认所有可用后端并发，按 [id] 同屏；可 --backends a,b 指定。"""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return R.error("ai.chat", "prompt 不能为空",
                           device=self.name, via=self.via)
        bs = self._pick(args.get("backends") or "")
        if not bs:
            # 指名要某个后端却一个都没匹配上：必须把「你要的名字」和「现有名字」
            # 都打出来。只回一句「没有可用后端」会让人以为是网络问题。
            spec = (args.get("backends") or "").strip()
            have = ", ".join(b["id"] for b in AB.backends()) or "(无)"
            why = (f"后端「{spec}」不存在，现有可用：{have}" if spec
                   else "没有可用后端")
            return R.unavailable("ai.chat", why,
                                 device=self.name, via=self.via)
        on_line = args.get("_on_line")
        mt = int(args.get("max_tokens") or DEFAULT_MAX_TOKENS)
        system = args.get("system") or ""
        results = {}

        if len(bs) == 1:
            try:
                results[bs[0]["id"]] = self._one(bs[0], prompt, system, mt, on_line)
            except Exception as e:
                results[bs[0]["id"]] = f"[错误: {e}]"
        else:
            # 多模型同问：并发跑，谁先回谁先显形（学 Cherry 话题内 executions 并行）
            lock = threading.Lock()

            def work(b):
                try:
                    t = self._one(b, prompt, system, mt, on_line)
                    with lock:
                        results[b["id"]] = t
                except Exception as e:
                    with lock:
                        results[b["id"]] = f"[错误: {e}]"
            ths = [threading.Thread(target=work, args=(b,), daemon=True) for b in bs]
            for t in ths:
                t.start()
            for t in ths:
                t.join()

        return R.success("ai.chat", self.name, side_effect=True, via=self.via,
                         data={"prompt": prompt,
                               "backends": list(results.keys()),
                               "answers": results})

    def _one(self, b, prompt, system, mt, on_line):
        """单个后端：流式拿 token，攒成行再经 _on_line 回流（界面才是一行行长出来）。

        SSE 给的是 token 不是行；这里按换行切，每段完整行回流一次，末尾残行也吐掉。
        """
        buf = {"t": ""}

        def on_token(delta):
            buf["t"] += delta
            while "\n" in buf["t"]:
                head, buf["t"] = buf["t"].split("\n", 1)
                if on_line:
                    on_line(f"[{b['id']}] {head}")

        text = AB.chat(b, prompt, system=system, on_token=on_token, max_tokens=mt)
        if buf["t"].strip() and on_line:
            on_line(f"[{b['id']}] {buf['t']}")
        if not (text or "").strip():
            # 空回是最阴的那种失败：界面上看着像「模型没话说」，实则是流里根本没有
            # 正文——上游临时抽风（实测 15 次里偶发 1 次，curl 直连也复现不了规律）。
            # 静默失败不许过：失败必须给原因，否则人只能盯着一块空白猜。
            note = ("[空回] 这个后端这次流里没有正文（上游临时抽风，实测偶发）；"
                    "换个后端或再问一次")
            if on_line:
                on_line(f"[{b['id']}] {note}")
            return note
        return text

    def _pick(self, spec):
        # 「all」/空 → 只返回健康端点（冷却中的不打扰）；显式指定 → 按名过滤（尊重用户选择）
        if not spec or spec.strip().lower() in ("all", ""):
            return AB.healthy_backends()
        want = {s.strip() for s in spec.split(",") if s.strip()}
        return [b for b in AB.backends() if b["id"] in want]
