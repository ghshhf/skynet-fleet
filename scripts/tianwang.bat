@echo off
REM ============================================================
REM  天网 · 一键启动
REM
REM  三种用法：
REM    tianwang.bat            本机桌面端（Tkinter，起本地模型 + 界面）
REM    tianwang.bat web        浏览器版（本机只当服务端，别的电脑用浏览器连）
REM    tianwang.bat nollm      不起本地模型（纯云端增强，最快）
REM
REM  路径不再写死盘符（2026-10-29）：
REM    以前这里写死 E:\_tools\llama.cpp-b11483 与 E:\_train_qwen06b 的模型，
REM    以及 E:\Python312\python.exe，换到只有 C/D 盘的机器上就整个起不来。
REM    现在 Python 解释器走 FLEET_PY -> py -3 -> python，
REM    模型和 llama-server 由 start_llm_hidden.py 按盘符自动探测。
REM ============================================================
setlocal
set "MODE=%1"
cd /d "%~dp0"

REM 后端配置（云端 key、语音设置都在这儿）。本机才有的文件，不入库。
if exist "%~dp0fleet_env.bat" call "%~dp0fleet_env.bat"

set "PY="
if defined FLEET_PY set "PY=%FLEET_PY%"
if not defined PY (
    where /q py 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=py -3"
    )
)
if not defined PY (
    where /q python 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [x] 没找到可用的 Python。装 Python 3.9+，或 set FLEET_PY=你的python.exe
    pause & exit /b 1
)

set "PORT=8080"

if /i "%MODE%"=="web" goto web
if /i "%MODE%"=="nollm" goto desktop

echo [1/2] 启动 llama-server (port %PORT%) ...
REM 就绪等待与超时降级都在脚本里做，这里不再自己数循环。
%PY% "%~dp0start_llm_hidden.py" --port %PORT% --ctx 4096 --tag chat --wait 60
if errorlevel 1 echo [警告] 本地模型起不来，继续启动（会自动降级到云端/模拟）。
goto desktop

:desktop
echo [2/2] 启动天网桌面端 ...
%PY% "%~dp0..\desktop.py"
goto end

:web
echo 启动天网 Web 版 ...
echo   本机访问：     http://127.0.0.1:8765
echo   局域网其他电脑：http://本机IP:8765
%PY% "%~dp0..\web.py"
goto end

:end
endlocal
