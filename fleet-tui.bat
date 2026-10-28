@echo off
REM 设备编队 - 全屏终端界面（双击进入）
REM 挑解释器顺序：FLEET_PY 覆盖，本机 portable，py -3，python
REM 每个候选都要真跑通 import sys 才要：Windows 商店那个存根是空壳，
REM 不实测的话它会假装自己是 python（静默 exit=49），然后什么都干不了。
setlocal
cd /d "%~dp0"

set "PY="
if defined FLEET_PY set "PY=%FLEET_PY%"
REM 便携版 python 装在哪个盘不写死（2026-10-29）：FLEET_PY 指，否则落到 py 启动器 / PATH 上的 python。
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=py -3"
    )
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY (
    echo [x] 没找到能用的 Python。请先装 Python 3.9+，
    echo    或者在命令行里 set FLEET_PY=你的python.exe 后再打开本文件。
    pause
    exit /b 1
)

%PY% -c "import textual" >nul 2>nul
if errorlevel 1 (
    echo [!] 首次运行，正在装界面依赖 textual...
    %PY% -m pip install -q textual
    if errorlevel 1 (
        echo [x] 依赖装不上，自己敲： %PY% -m pip install textual
        pause
        exit /b 1
    )
)

%PY% fleet_tui.py %*
