"""宿主模拟后端：在真模型没接上之前，让「她」先能开口。

## 这是什么

一个**规则驱动**的替身。读当前角色卡的字段（自称/口癖/说话方式/性格），
按主人这句话的意图挑一句符合人设的回话。目的是：

* 今晚能把**整条链路**跑通看效果（界面 → 上下文拼装 → 输出清洗 → 语音出声），
  而不用等真模型；
* 真模型接上后，把 `FLEET_LLM_URL` 指过去就自动接管，模拟层只做兜底。

## 它不做什么

它**不是**语言模型，不会真正理解问题。它的价值是**占位 + 暴露问题**：
如果你切到鲸鱼娘卡，模拟层会自称鲸鱼娘、带尾鳍动作；切到北极星就换成
「我」和直球口吻——**人设链路通没通，一眼就能看出来**。

真模型到位后这段代码就退到链尾，不影响任何事。
"""
import os
import re

# 意图 -> 回复模板。{self} {catch} {tail} 会被替换
_TEMPLATES = {
    "greet": ["{self}在，主人。", "{tail}嗯——{self}在呢。", "来了，主人。"],
    "who": ["{self}。", "{tail}……{self}，记住了？", "我是{self}。"],
    "name": ["{self}。", "叫{self}。别叫错了。"],
    "food": ["米饭。", "{tail}米饭。别的不要。"],
    "fat": ["{tail}才不胖！是……看起来壮。", "哼，谁胖了。"],
    "thanks": ["不用谢。", "{tail}哼……举手之劳。"],
    "bye": ["嗯，去忙吧。", "{tail}……嗯，走了就别拖。"],
    "love": ["{tail}……哼，说什么呢。", "嗯……只说一次，听好了。"],
    "help": ["说，我在听。", "{tail}什么？讲。"],
    "default": ["{tail}嗯。", "知道了。", "{tail}……然后呢？", "嗯，我听着。"],
}

_INTENT_RULES = [
    ("greet", r"^(你好|哈喽|hi|hello|在吗|在么|在不在|早|早安|晚上好)"),
    ("who", r"(你是谁|你是什么|介绍.*自己|自我介绍)"),
    ("name", r"(你叫什么|你的名字|叫什么名字)"),
    ("food", r"(吃|食物|饭|饿)"),
    ("fat", r"(胖|肥|体重)"),
    ("thanks", r"(谢|thanks|感谢)"),
    ("bye", r"(再见|拜拜|走了|下线|睡觉|晚安)"),
    ("love", r"(喜欢|爱你|抱抱|亲亲|想你)"),
    ("help", r"(帮我|帮忙|怎么办|做什么|能干什么)"),
]


def _pick_intent(text):
    t = (text or "").strip()
    for name, pat in _INTENT_RULES:
        if re.search(pat, t, re.I):
            return name
    return "default"


def _extract_profile(system):
    """从 system prompt 里抠出自称/口癖/说话方式，用来塑人设。"""
    prof = {"self": "她", "catch": "", "tail": "", "tone": ""}
    if not system:
        return prof
    m = re.search(r"自称[：:]\s*([^\n。]+)", system)
    if m:
        name = m.group(1).strip()
        # 「鲸鱼娘。称对方「主人」」-> 取第一段
        name = re.split(r"[。，,；;]", name)[0].strip()
        if name:
            prof["self"] = name
    m = re.search(r"口癖[：:]\s*([^\n]+)", system)
    if m:
        prof["catch"] = m.group(1).strip()[:40]
    # 尾鳍动作：只有鲸鱼娘这类卡才用
    if "尾鳍" in system or "MODE_TAIL_FLUKES" in system:
        prof["tail"] = "（尾鳍轻轻一摆）"
    m = re.search(r"说话方式[：:]\s*([^\n]+)", system)
    if m:
        prof["tone"] = m.group(1).strip()[:60]
    return prof


def _apply_tone(text, tone):
    """按「说话方式」做点表面调整：短句、禁表情符号。"""
    t = re.sub(r"[😀-🙏🌀-🫿]", "", text)
    t = t.replace("您", "你")
    if "十到三十字" in tone or "八到四十字" in tone:
        # 超长的截断到第一个句号
        if len(t) > 40 and "。" in t:
            t = t.split("。")[0] + "。"
    return t.strip()


def complete(prompt, model=None, system=None):
    """返回 (text, 'mock')。"""
    prof = _extract_profile(system)
    intent = _pick_intent(prompt)
    tpl = _TEMPLATES.get(intent, _TEMPLATES["default"])
    # 稳定选一条：按 prompt 内容取模，避免每次都变
    idx = abs(hash((prompt or "") + intent)) % len(tpl)
    text = tpl[idx]
    text = text.replace("{self}", prof["self"])
    text = text.replace("{tail}", (prof["tail"] + " ") if prof["tail"] else "")
    text = text.replace("{catch}", prof["catch"])
    text = _apply_tone(text, prof["tone"])
    return text, "mock"


def stream(prompt, model=None, system=None, on_delta=None):
    text, b = complete(prompt, model, system)
    if on_delta:
        for i in range(0, len(text), 4):
            on_delta(text[i:i + 4])
    return text, b


if __name__ == "__main__":
    import persona_card
    for q in ("你好", "你是谁", "你叫什么名字", "你最爱吃什么", "帮我看下日志"):
        s = persona_card.build_system("whale_girl")
        t, b = complete(q, system=s)
        print("[%s] %s -> %s" % (b, q, t))
