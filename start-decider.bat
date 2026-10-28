@echo off
rem ============================================================
rem  工具面决策模型 -- 一键起本地服务（端口 18080）
rem ------------------------------------------------------------
rem  为什么需要它:
rem    fleet/agent_tools.py 的 decide() 默认打 http://127.0.0.1:18080,
rem    服务没起 -> decide() 一律返回 None -> 界面上只表现为「助理什么都不会用」.
rem
rem  为什么用本地而不用云端免费端点（2026-10-26 / 10-28 实测）:
rem    * 免费端点 12 条问句里 7 条 429 或空返回；
rem    * 决策输入是用户原话，走云端等于把原话发给第三方；
rem    * 本机 Qwen3.5-4B(Q4_K_M) 实测 8/8 全通过，单次 4.2 ~ 9.3 秒.
rem
rem  路径不再写死盘符（2026-10-29）:
rem    以前这里写死 G:\_downloads\... 和 G:\_models\..., 换机即废.
rem    现在 llama-server 和 gguf 都由 scripts\start_llm_hidden.py 按
rem    「相对目录结构 x 本机盘符」全组合探测（见 fleet/locate.py）.
rem    想强指定就设环境变量:
rem        set FLEET_LLAMA_SERVER=D:\somewhere\llama-server.exe
rem        set FLEET_LLM_MODEL=D:\somewhere\model.gguf
rem ============================================================
setlocal
cd /d "%~dp0"

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

set "PORT=18080"
%PY% "%~dp0scripts\start_llm_hidden.py" --port %PORT% --ctx 4096 --tag decider
if errorlevel 1 (
    echo.
    echo [x] 起不来。上面已经列出找过的位置和补救办法。
    pause
)
endlocal
