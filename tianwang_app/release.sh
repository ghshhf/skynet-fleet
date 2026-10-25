#!/usr/bin/env bash
# 天网 APP 发版/迭代：一条命令完成「版本 +1 → 构建 → 覆盖安装 → 校验」。
#
#   bash release.sh                  # 自动 patch +1 并装到手机
#   bash release.sh -n 0.2.0-proto   # 指定版本名
#   bash release.sh --build-only     # 只构建不安装
#
# 🔴 铁律：永远 `install -r` 覆盖，绝不卸载。
#    卸载会连带清掉 /sdcard/Android/data/app.skynet.tianwang.next/files/models
#    下的 GGUF（4B/1.7B/SmolLM2 共约 4 GB），重推很痛。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PROPS="$HERE/version.properties"
ADB="${ADB:-/g/sdk/platform-tools/adb.exe}"
JAVA_HOME="${JAVA_HOME:-/g/huanjie/Android Studio/jbr}"
export JAVA_HOME
export MSYS_NO_PATHCONV=1   # 否则 adb 的 /sdcard/... 会被 Git Bash 转成 Windows 路径

NAME_OVERRIDE=""
BUILD_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -n) NAME_OVERRIDE="$2"; shift 2 ;;
    --build-only) BUILD_ONLY=1; shift ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

# ---------- 1. 版本 +1 ----------
cur_code=$(grep -E '^VERSION_CODE=' "$PROPS" | cut -d= -f2 | tr -d ' \r')
cur_name=$(grep -E '^VERSION_NAME=' "$PROPS" | cut -d= -f2 | tr -d ' \r')
next_code=$((cur_code + 1))

if [ -n "$NAME_OVERRIDE" ]; then
  next_name="$NAME_OVERRIDE"
else
  # 把 x.y.z-* 的 z 加一，其余照旧
  next_name=$(printf '%s' "$cur_name" | awk -F'[-.]' 'BEGIN{OFS="-"} {core=$1"."$2"."($3+1); if (NF>=4) {s=""; for(i=4;i<=NF;i++) s=s (i>4?"-":"") $i; print core"-"s} else print core}' 2>/dev/null || printf '%s' "$cur_name")
  [ -z "$next_name" ] && next_name="$cur_name"
fi

{
  echo "# 版本迭代唯一真源。"
  echo "# 改这两行 = 发版；\`bash release.sh\` 会自动把 VERSION_CODE 加一并构建覆盖安装。"
  echo "# 🔴 手机只认「同一签名 + 版本号更大」的覆盖更新，卸载会连带删掉"
  echo "#    /sdcard/Android/data/app.skynet.tianwang.next/files/models 下的模型，"
  echo "#    所以永远走 install -r，不卸载。"
  echo "VERSION_CODE=$next_code"
  echo "VERSION_NAME=$next_name"
} > "$PROPS"

echo "▶ 版本：$cur_name ($cur_code)  →  $next_name ($next_code)"

# ---------- 2. 构建 ----------
SO="$HERE/app/src/main/jniLibs/arm64-v8a/libfleet_llama.so"
if [ ! -f "$SO" ]; then
  echo "✗ 缺 $SO —— 先跑 bash build-llama.sh 编原生推理库"
  exit 1
fi

APK="$HERE/app/build/outputs/apk/debug/app-debug.apk"

echo "▶ 构建 debug APK（jniLibs 里的 .so 会一起打进去）…"
# 先删旧包：构建失败时绝不能拿上一次的 APK 去装（会以为发版成功）
rm -f "$APK"
BUILD_LOG="$HERE/build/release-build.log"
mkdir -p "$HERE/build"
if ! ./gradlew assembleDebug --console=plain > "$BUILD_LOG" 2>&1; then
  echo "✗ 构建失败，编译器报错："
  grep -E '^e: |error:|FAILED' "$BUILD_LOG" | head -20
  exit 1
fi

[ -f "$APK" ] || { echo "✗ 没产出 APK"; exit 1; }
SIZE_MB=$(awk -v b="$(wc -c < "$APK")" 'BEGIN{printf "%.1f", b/1048576}')
echo "▶ 产物：$APK（${SIZE_MB} MB）"

# ---------- 3. 覆盖安装 ----------
if [ "$BUILD_ONLY" -eq 1 ]; then
  echo "✓ 只构建，未安装。"
  exit 0
fi

if ! "$ADB" devices | grep -qw "device$"; then
  echo "✗ 没检测到 USB 设备（adb devices 为空）"
  exit 1
fi

echo "▶ 覆盖安装（install -r，保留模型与数据）…"
# 🔴 MSYS_NO_PATHCONV=1 是为了保住 adb shell 里的 /sdcard/... 不被 Git Bash 转义，
#    副作用是宿主机路径必须自己转成 Windows 形式交给 adb.exe，否则它 stat 不到。
APK_WIN=$(cygpath -m "$APK")
OUT=$("$ADB" install -r "$APK_WIN" 2>&1 | tail -3)
echo "$OUT"
case "$OUT" in
  *Success*) ;;
  *) echo "✗ 安装没成功，上面是原因"; exit 1 ;;
esac

# ---------- 4. 校验：手机上真的是新版本、模型还在 ----------
sleep 1
INSTALLED=$("$ADB" shell dumpsys package app.skynet.tianwang.next 2>/dev/null \
  | grep -m1 'versionCode=' | tr -d '\r')
MODELS=$("$ADB" shell "ls /sdcard/Android/data/app.skynet.tianwang.next/files/models/ 2>/dev/null" \
  | tr -d '\r' | grep -c 'gguf' || true)

echo "▶ 手机实际：$INSTALLED"
echo "▶ 模型文件：${MODELS} 个还在"

if printf '%s' "$INSTALLED" | grep -q "versionCode=$next_code "; then
  echo "✓ 迭代完成：$next_name ($next_code) 已在手机上运行"
else
  echo "⚠ 装上了但版本号对不上预期 $next_code，检查是否被系统回滚"
  exit 1
fi
