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

    # 出厂卡种子：FLEET_CARD_DIR 指向的可写目录还是空的（首次运行），而
    # 打包时 cards/ 已随包放进 MEIPASS——把出厂卡复制过去，用户改卡/下载新卡
    # 都落在可写目录，升级 exe 不丢卡。
    try:
        card_dir = os.environ["FLEET_CARD_DIR"]
        src_cards = os.path.join(meipass, "cards")
        if os.path.isdir(src_cards) and not os.path.exists(
                os.path.join(card_dir, "active.json")) and not any(
                f.endswith(".json") for f in os.listdir(card_dir)):
            import shutil
            shutil.copytree(src_cards, card_dir, dirs_exist_ok=True)
    except Exception:
        pass  # 种子失败不拦启动：卡库空时上层有明确报错


_apply()
