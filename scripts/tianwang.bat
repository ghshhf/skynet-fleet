@echo off
REM ============================================================
REM  天网 · 一键启动
REM
REM  三种用法：
REM    tianwang.bat            本机桌面端（Tkinter，起本地模型 + 界面）
REM    tianwang.bat web        浏览器版（本机只当服务端，别的电脑用浏览器连）
REM    tianwang.bat nollm      不起本地模型（纯云端增强，最快）
REM ============================================================
setlocal
set MODE=%1

REM 后端配置（云端 key、语音设置都在这儿）
if exist "%~dp0fleet_env.bat" call "%~dp0fleet_env.bat"

set LLAMA_DIR=G:\_tools\llama.cpp-b11483\bin
set MODEL=G:\_train_qwen06b\v13-Q8_0.gguf
set TEMPLATE=%~dp0..\templates\qwen3_thinkblock.jinja
set PORT=8080

if /i "%MODE%"=="web" goto web
if /i "%MODE%"=="nollm" goto desktop

REM ---------------- 起本地模型 ----------------
if not exist "%MODEL%" (
  echo [警告] 找不到本地模型 %MODEL%
  echo        跳过本地模型，只用云端/模拟后端。
  goto desktop
)
echo [1/2] 启动 llama-server (port %PORT%) ...
start "tianwang-llama" /min cmd /c ^
  "%LLAMA_DIR%\llama-server.exe" -m "%MODEL%" --port %PORT% ^
  --host 127.0.0.1 -c 4096 -ngl 99 -dev Vulkan1 ^
  --jinja --chat-template "%TEMPLATE%" --reasoning-budget 0 ^
  -t 8 --no-webui 2^>^&1"

set /a tries=0
:wait
timeout /t 1 /nobreak >nul
set /a tries+=1
curl -s -m 2 http://127.0.0.1:%PORT%/v1/models >nul 2>&1
if not errorlevel 1 goto ready
if %tries% lss 60 goto wait
echo [警告] 本地模型 60 秒没就绪，继续启动（会自动降级）。
goto desktop
:ready
echo [1/2] 本地模型就绪。

:desktop
echo [2/2] 启动天网桌面端 ...
G:\Python312\python.exe "%~dp0..\desktop.py"
goto end

:web
echo 启动天网 Web 版 ...
echo   本机访问： http://127.0.0.1:8765
echo   局域网其他电脑： http://本机IP:8765
G:\Python312\python.exe "%~dp0..\web.py"
goto end

:end
endlocal
