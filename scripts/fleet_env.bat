@echo off
REM ============================================================
REM  天网 · 后端配置（本文件含 key，已进 .gitignore，不要提交）
REM  用法：在 tianwang.bat / web 启动前 call 本文件
REM ============================================================

REM ---- 云端增强后端（人格承载力最好，优先用）----
REM 实测：免费端点上的模型能真演出角色卡；本地 0.6B 载不动完整人格。
set FLEET_CLOUD_URL=http://127.0.0.1:8787/v1
set FLEET_CLOUD_KEY=ofm-mP2XcdVTNrpG2HU3dr41b3daZzw_lICB
set FLEET_CLOUD_MODEL=kilo-auto/free

REM ---- 本地后端（离线保底）----
set FLEET_LLM_URL=http://127.0.0.1:8080
set FLEET_LLM_MODEL=G:/_train_qwen06b/v13-Q8_0.gguf

REM ---- 语音 ----
REM whisper 模型已落盘，只读本地，不再联网
set FLEET_WHISPER_LOCAL=1
set FLEET_WHISPER_DIR=G:/_models/whisper
REM 识别热词：减少同音错字
set FLEET_ASR_HINT=主人，鲸鱼娘，天网，天枢，北极星，米饭，本地模型，聊天。

REM ---- 关掉云端就把它注释掉：本地/模拟会顶上 ----
