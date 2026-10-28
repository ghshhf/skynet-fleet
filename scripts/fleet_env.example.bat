@echo off
REM ============================================================
REM  Tianwang - backend config TEMPLATE
REM ------------------------------------------------------------
REM  HOW TO USE
REM    1. copy this file to  scripts\fleet_env.bat
REM    2. fill in your own key / paths
REM    3. fleet_env.bat is git-ignored - NEVER commit it
REM
REM  WHY A TEMPLATE
REM    The real fleet_env.bat used to be tracked in git with a live
REM    API key inside, while its own header claimed it was ignored.
REM    It is now genuinely ignored; this file is the tracked one,
REM    with every secret blanked out.
REM
REM  PATHS ARE OPTIONAL (2026-10-29)
REM    Anything left blank is auto-detected by fleet/locate.py: it
REM    enumerates the local drives at runtime and combines them with
REM    known relative layouts (hints x roots). Do NOT hardcode a
REM    drive letter unless auto-detection genuinely cannot find it -
REM    a hardcoded drive breaks the moment the repo moves machines.
REM ============================================================

REM ---- cloud gateway (best persona carry) ----
set FLEET_CLOUD_URL=http://127.0.0.1:8787/v1
set FLEET_CLOUD_KEY=
set FLEET_CLOUD_MODEL=kilo-auto/free

REM ---- local backend (offline fallback) ----
set FLEET_LLM_URL=http://127.0.0.1:8080
REM blank -> locate picks the newest *.gguf it can find
set FLEET_LLM_MODEL=

REM ---- voice ----
set FLEET_WHISPER_LOCAL=1
REM blank -> locate searches the usual whisper dirs
set FLEET_WHISPER_DIR=
set FLEET_ASR_HINT=

REM ---- escape hatches for locate (only when auto-detect fails) ----
REM set FLEET_LLAMA_SERVER=
REM set FLEET_FFMPEG=
REM set FLEET_ADB=
