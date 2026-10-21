"""PyInstaller 运行时钩子（仅冻结后生效）。

冻扁包解压到临时目录（MEIPASS，只读），但代码里有几处把状态文件默认写到
「仓库根」——冻扁后仓库根就是 MEIPASS，只读，写进去会当场崩。这里把它们
重定向到 %APPDATA%/fleet（可写），且只在冻结后生效，绝不污染开发态。
用户仍可用同名环境变量覆盖。

涉及的状态文件：
  FLEET_PERSONA    persona_state.json   —— 人格抽屉的「她现在怎么样」
  FLEET_CTX       fleet_context.db     —— 记忆层（E-7：可寻址可删）
  FLEET_CARD_DIR  cards/               —— 角色卡库
  FLEET_QUEUE     fleet_queue.json      —— 待办队列账本
"""
import os
import shutil
import sys
import tempfile


def _apply():
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return  # 开发态：一切照旧

    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    base = os.path.join(appdata, "fleet")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        # APPDATA 不可写（极端情况）——退回系统临时目录，至少别崩
        base = os.path.join(tempfile.gettempdir(), "fleet")
        os.makedirs(base, exist_ok=True)

    overrides = {
        "FLEET_PERSONA": os.path.join(base, "persona_state.json"),
        "FLEET_CTX": os.path.join(base, "fleet_context.db"),
        "FLEET_CARD_DIR": os.path.join(base, "cards"),
        "FLEET_QUEUE": os.path.join(base, "fleet_queue.json"),
    }
    for key, val in overrides.items():
        os.environ.setdefault(key, val)

    # 卡片库不只是「可写路径」，还是**初始数据**：语音对话视图没有卡就没有
    # 人格、没有音色（say_for_card 查不到卡就落到默认音色）。而 %APPDATA%/fleet/cards
    # 首次是空的 ⇒ 从冻扁包内置的 cards/ 播种一次。
    # 只补缺，不覆盖：用户自己改过/加过的卡永远优先。
    card_dir = overrides["FLEET_CARD_DIR"]
    try:
        os.makedirs(card_dir, exist_ok=True)
        src = os.path.join(meipass, "cards")
        if os.path.isdir(src):
            for fn in os.listdir(src):
                sp = os.path.join(src, fn)
                dp = os.path.join(card_dir, fn)
                # 只拷文件，compiled/ 这类子目录是缓存，不播
                if os.path.isfile(sp) and not os.path.exists(dp):
                    shutil.copy2(sp, dp)
    except OSError:
        # 播种失败不阻塞启动：卡库空会在界面上表现为「没有卡」，
        # 比启动直接崩掉要好查得多。
        pass


_apply()
