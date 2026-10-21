"""Windows 图形版的打包入口：`python fleet_gui.py` 直接开窗口。

也是 PyInstaller 的打包入口（见 tools/build_gui.py）。
fleet/gui/app.py 用的是包内相对导入，直接拿它当脚本跑会断，
所以这里留一个不带相对导入的薄壳——和 fleet_tui.py 一个套路。
"""
import sys

from fleet.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
