@echo off
rem ============================================================
rem  工具面决策模型 -- 一键起本地服务
rem ------------------------------------------------------------
rem  为什么需要它:
rem    fleet/agent_tools.py 的 decide() 默认打 http://127.0.0.1:18080,
rem    那句话决定[助理能不能选中工具].服务没起 -> decide() 一律返回 None
rem    -> 工具面常年是废的,界面上只表现为[助理什么都不会用].
rem
rem  为什么用本地而不用云端免费端点(2026-10-26 实测):
rem    * pollinations 免费端点 5 次里 4 次失败(402 / 读超时 / 空串)
rem    * 决策输入是用户原话,走云端等于把原话发给第三方
rem    本机 Qwen3.5-4B(Q4_K_M)实测 8/8 全通过,单次 4.2 秒~9.3 秒.
rem
rem  用法:双击即可.想换模型就改下面两行.
rem ============================================================

set "LLAMA_BIN=G:\_downloads\llamacpp-b11840\llama-server.exe"
set "MODEL=G:\_models\llm\gguf\Qwen3.5-4B-Q4_K_M.gguf"
set "PORT=18080"

if not exist "%LLAMA_BIN%" (
    echo [错] 找不到 llama-server.exe: %LLAMA_BIN%
    echo      改本脚本里的 LLAMA_BIN 指到你自己的路径.
    pause & exit /b 1
)
if not exist "%MODEL%" (
    echo [错] 找不到模型: %MODEL%
    echo      改本脚本里的 MODEL 指到你自己的 gguf.
    pause & exit /b 1
)

echo 起服务: %MODEL%
echo 端口:   %PORT%    上下文 4096    全部层卸载到 GPU
echo.
echo 首次加载 2.6G 进显存要几十秒,看到 starting the main loop 才可用.
echo 验证:   curl http://127.0.0.1:%PORT%/health
echo 关掉它: 直接关这个窗口.
echo.

"%LLAMA_BIN%" -m "%MODEL%" -c 4096 -ngl 99 --port %PORT% --host 127.0.0.1 --jinja

echo.
echo 服务已退出.
pause
