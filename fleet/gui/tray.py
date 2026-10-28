"""托盘常驻：关掉窗口不等于关掉她。

## 为什么一个常驻程序必须有托盘

这程序平时不该占着屏幕。它要做的事——接手机的心跳、等审批、看住被控机——
全都在窗口关掉之后才真正有用。没有托盘只有两个选择：要么窗口一直开着挡视线，
要么关掉窗口就等于把她关了。托盘是把这两个烂选择拆开的那一刀：
**窗口是脸，托盘是命。**

## 三条边界

1. **没装 pystray 不炸。** 返回 None，调用方退化成"关窗即退出"，并把原因打出来
   （一个静默失去常驻能力的程序，比明说"我没法常驻"更坏）。
2. **托盘线程不许碰界面。** 菜单回调只调传进来的 `show` / `quit` 两个函数，
   这两个函数内部再各自处理线程安全（pywebview 的 show/hide 本身可跨线程调用）。
3. **图标不依赖外部文件。** 现画的——打包时少一个"忘带 ico"的失败点。

## 菜单为什么是这几项

`显示主窗口` `开机自启` `打开数据目录` `退出`。
「打开数据目录」不是为了好看：出问题时主人要能自己找到日志和凭据文件，
不然每一次排查都得先问一句"东西存哪了"。
"""
import os
import sys
import threading

TITLE = "设备编队"

# 图标：深蓝底 + 鲸尾。不读外部文件，免得打包时又少一个资源。
_ICON_SIZE = 64


def _make_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, _ICON_SIZE - 2, _ICON_SIZE - 2], fill=(22, 38, 66, 255))
    # 尾柄：从右下往左上的一条粗线
    d.line([(20, 46), (34, 30)], fill=(232, 240, 255, 255), width=7)
    # 两片尾鳍
    d.polygon([(34, 30), (46, 12), (50, 34)], fill=(232, 240, 255, 255))
    d.polygon([(34, 30), (46, 50), (28, 48)], fill=(160, 200, 255, 255))
    return img


class Tray:
    """把 pystray 包一层：菜单项固定是那四件，行为全由回调决定。"""

    def __init__(self, show=None, quit=None, open_data=None,
                 autostart_on=None, autostart_off=None, autostart_state=None,
                 title=TITLE):
        self._show = show
        self._quit = quit
        self._open_data = open_data
        self._as_on = autostart_on
        self._as_off = autostart_off
        self._as_state = autostart_state or (lambda: False)
        self._title = title
        self._icon = None
        self._thread = None

    def start(self):
        """起托盘线程。返回 True = 真在托盘里了。"""
        try:
            import pystray
        except Exception as e:
            print("[fleet-tray] 没装 pystray（%s: %s），关窗口就是退出。"
                  "要常驻：pip install pystray" % (type(e).__name__, e), flush=True)
            return False
        try:
            image = _make_image()
        except Exception as e:
            print("[fleet-tray] 画不出图标（%s: %s），托盘不启用。" % (type(e).__name__, e),
                  flush=True)
            return False

        def _call(fn):
            def handler(icon=None, item=None):
                if fn is not None:
                    try:
                        fn()
                    except Exception as e:
                        print("[fleet-tray] 菜单动作失败：%s: %s" % (type(e).__name__, e),
                              flush=True)
            return handler

        def _toggle_autostart(icon=None, item=None):
            try:
                if self._as_state():
                    if self._as_off:
                        self._as_off()
                else:
                    if self._as_on:
                        self._as_on()
            except Exception as e:
                print("[fleet-tray] 改开机自启失败：%s: %s" % (type(e).__name__, e),
                      flush=True)

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", _call(self._show), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", _toggle_autostart,
                             checked=lambda item: bool(self._as_state())),
            pystray.MenuItem("打开数据目录", _call(self._open_data)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _call(self._quit)),
        )
        try:
            self._icon = pystray.Icon("device-fleet", image, self._title, menu)
        except Exception as e:
            print("[fleet-tray] 托盘创建失败：%s: %s" % (type(e).__name__, e), flush=True)
            return False

        def run():
            try:
                self._icon.run()
            except Exception as e:
                print("[fleet-tray] 托盘线程退出：%s: %s" % (type(e).__name__, e), flush=True)

        self._thread = threading.Thread(target=run, name="fleet-tray", daemon=True)
        self._thread.start()
        print("[fleet-tray] 托盘已就位：关窗口不退出，从托盘里退出。", flush=True)
        return True

    def notify(self, message, title=None):
        """气泡提示。托盘不可用时静默——这不是关键路径。"""
        if self._icon is None:
            return False
        try:
            self._icon.notify(message, title or self._title)
            return True
        except Exception:
            return False

    def set_title(self, text):
        if self._icon is None:
            return
        try:
            self._icon.title = text
        except Exception:
            pass

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


def data_dir():
    """数据和凭据所在目录 —— 与 fleet 其余部分保持一致（~/.fleet）。"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".fleet")
