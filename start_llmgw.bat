@echo off
rem ============================================================
rem  Start tianwang llmgw gateway (port 8790) - hidden window
rem  One entry, backend auto-failover (flash/claude/gpt/cn/...)
rem
rem  Why this exists: the gateway is a long-running serve_forever
rem  process. It must be started from the user side (double-click
rem  or startup), NOT from an AI chat subprocess - those get
rem  reaped when the session switches.
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
    echo [x] no Python found. install Python 3.9+ or set FLEET_PY=
    pause & exit /b 1
)

%PY% "%~dp0scripts\start_llmgw_hidden.py" --port 8790
endlocal
