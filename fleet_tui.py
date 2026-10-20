"""全屏终端界面的入口：`python fleet_tui.py` 直接进界面。

也是 PyInstaller 的打包入口——`fleet/tui.py` 用的是包内相对导入，
直接拿它当脚本跑会断，所以这里留一个不带相对导入的薄壳。
"""
import sys

from fleet.tui import main

if __name__ == "__main__":
    sys.exit(main())
