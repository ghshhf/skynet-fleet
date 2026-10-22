#!/usr/bin/env bash
# 天网 · 手机原生端构建
#
# 不用 Gradle：整个工程 5 个 Java 类 + 6 个资源文件，拉 Gradle 进来要几百 MB
# 依赖和常驻 daemon，纯属为了走流程而走流程。aapt2 + javac + d8 + apksigner
# 手搓五步，几十秒出包，产物一百多 KB。
#
# 同一份脚本在 Windows（Git Bash）和 GitHub Actions（ubuntu）上都能跑：
#   SDK   ← $ANDROID_HOME / $ANDROID_SDK_ROOT，都没有就回落到本机 D:/DevTools/Android
#   工具  ← build-tools 里按版本号取最高的那套，自动补 .exe / .bat 后缀
#
# 用法：bash tianwang_android/build.sh
# 产物：tianwang_android/build/tianwang.apk
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# ---------------- ① 定位 SDK ----------------
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [ -z "$SDK" ] || [ ! -d "$SDK" ]; then
  SDK="/d/DevTools/Android"
fi
if [ ! -d "$SDK" ]; then
  echo "找不到 Android SDK：ANDROID_HOME 未设置，回落路径 $SDK 也不存在" >&2
  exit 1
fi

BT_DIR="$(ls -d "$SDK"/build-tools/*/ 2>/dev/null | sort -V | tail -1)"
BT_DIR="${BT_DIR%/}"
PLATFORM="$(ls -d "$SDK"/platforms/android-*/ 2>/dev/null | sort -V | tail -1)"
PLATFORM="${PLATFORM%/}/android.jar"

if [ -z "$BT_DIR" ] || [ ! -d "$BT_DIR" ]; then
  echo "找不到 build-tools（$SDK/build-tools）" >&2
  exit 1
fi
if [ ! -f "$PLATFORM" ]; then
  echo "找不到 android.jar（$SDK/platforms/android-*/android.jar）" >&2
  exit 1
fi
API="$(basename "$(dirname "$PLATFORM")")"
API="${API#android-}"

# 工具名在 Windows 带后缀，在 Linux 不带——统一解析，不写两套逻辑
tool() {
  local n="$1" c
  for c in "$BT_DIR/$n" "$BT_DIR/$n.exe" "$BT_DIR/$n.bat"; do
    if [ -f "$c" ]; then printf '%s' "$c"; return 0; fi
  done
  echo "缺工具：$BT_DIR/$n" >&2
  return 1
}
AAPT2="$(tool aapt2)"
AAPT="$(tool aapt)"
D8="$(tool d8)"
ZIPALIGN="$(tool zipalign)"
APKSIGNER="$(tool apksigner)"

# ---------------- ② 定位 JDK ----------------
# Windows 上 PATH 里那个 java 可能和 JAVA_HOME 不一致，d8.bat / apksigner.bat
# 认的是 JAVA_HOME，两边对不上就会直接拒绝启动——所以优先用带 javac 的那份。
if [ -n "${JAVA_HOME:-}" ] && { [ -x "$JAVA_HOME/bin/javac" ] || [ -x "$JAVA_HOME/bin/javac.exe" ]; }; then
  :
elif [ -x "/d/tools/jdk25_raw/jdk-25.0.4.1+1/bin/javac.exe" ]; then
  JAVA_HOME="/d/tools/jdk25_raw/jdk-25.0.4.1+1"
fi

if [ -n "${JAVA_HOME:-}" ]; then
  if [ -x "$JAVA_HOME/bin/javac" ]; then
    JAVAC="$JAVA_HOME/bin/javac"
  else
    JAVAC="$JAVA_HOME/bin/javac.exe"
  fi
  # .bat 走 cmd，需要 Windows 风格的 JAVA_HOME
  case "$JAVA_HOME" in
    /?/*) export JAVA_HOME="$(cygpath -w "$JAVA_HOME" 2>/dev/null || echo "$JAVA_HOME")" ;;
  esac
else
  JAVAC="$(command -v javac || true)"
  [ -n "$JAVAC" ] || { echo "找不到 javac：设 JAVA_HOME 或装 JDK" >&2; exit 1; }
fi

echo "SDK      : $SDK"
echo "build-tools: $BT_DIR"
echo "platform : $PLATFORM (API $API)"
echo "javac    : $JAVAC"
echo "--------"

rm -rf build
mkdir -p build/res build/gen build/classes build/dex

# ---------------- ③ 资源 → 资源表 + R.java ----------------
"$AAPT2" compile --dir res -o build/res
FLATS="$(find build/res -name '*.flat' | tr '\n' ' ')"
# shellcheck disable=SC2086
"$AAPT2" link -o build/base.apk -I "$PLATFORM" \
  --manifest AndroidManifest.xml \
  --min-sdk-version 26 --target-sdk-version "$API" \
  --java build/gen \
  --auto-add-overlay $FLATS
echo "[1/5] 资源编译完成"

# ---------------- ④ 编译 Java（含 R.java） ----------------
find src build/gen -name '*.java' > build/sources.txt
"$JAVAC" -encoding UTF-8 --release 11 -nowarn -classpath "$PLATFORM" \
  -d build/classes @build/sources.txt
echo "[2/5] javac 完成"

# ---------------- ⑤ dex ----------------
CLASSES="$(find build/classes -name '*.class')"
# shellcheck disable=SC2086
"$D8" --lib "$PLATFORM" --min-api 26 --release \
  --output build/dex $CLASSES
echo "[3/5] d8 完成"

# ---------------- ⑥ 把 dex 塞进 apk ----------------
# 没有 zip 命令也照样能干：aapt(1) 自带 add，build-tools 里必有。
( cd build/dex && "$AAPT" add -f ../base.apk classes.dex >/dev/null )
echo "[4/5] classes.dex 已合入"

# ---------------- ⑥b 本地语音的 .so ----------------
# 不在这一步现场编（NDK 全套编一遍一分半，且大多数改动跟语音无关），
# 用 jni/build/libfleet_whisper.so 这个已经 strip 过的产物（约 1.6MB）。
# 要重编：bash jni/build.sh
SO="$HERE/jni/build/libfleet_whisper.so"
if [ -f "$SO" ]; then
  PY="${PY:-/d/tools/python/python.exe}"
  "$PY" - "$HERE/build/base.apk" "$SO" <<'PYEOF'
import sys, zipfile, os
apk, so = sys.argv[1], sys.argv[2]
# 用 STORED 存（不压缩）：Android 6+ 可以直接 mmap APK 里的库，
# 压缩存的话系统还得先解压到 /data 再加载，既多一份磁盘又慢一拍。
# 以 'a' 模式打开会保留原有条目，只追加这一个。
with zipfile.ZipFile(apk, "a", zipfile.ZIP_STORED) as z:
    z.write(so, "lib/arm64-v8a/" + os.path.basename(so))
print("      lib/arm64-v8a/%s  %.1f MB" % (os.path.basename(so),
                                          os.path.getsize(so) / 1048576))
PYEOF
  echo "[4/5b] libfleet_whisper.so 已合入"
else
  echo "[4/5b] 跳过本地语音（没有 jni/build/libfleet_whisper.so）"
fi

# ---------------- ⑦ 签名 ----------------
KS="$HERE/keystore.jks"
PASS="tianwang"
if [ -n "${ANDROID_KEYSTORE_B64:-}" ]; then
  printf '%s' "$ANDROID_KEYSTORE_B64" | base64 -d > "$KS"
  echo "      用 CI 注入的签名密钥"
elif [ ! -f "$KS" ]; then
  KEYTOOL="$(dirname "$JAVAC")/keytool"
  [ -x "$KEYTOOL" ] || KEYTOOL="${KEYTOOL}.exe"
  "$KEYTOOL" -genkeypair -keystore "$KS" -alias tianwang \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$PASS" -keypass "$PASS" \
    -dname "CN=tianwang, OU=skynet, O=skynet, L=Beijing, ST=Beijing, C=CN" >/dev/null 2>&1
  echo "      已生成签名密钥 keystore.jks（别入库，重装要用同一份）"
fi

"$ZIPALIGN" -f -p 4 build/base.apk build/aligned.apk
"$APKSIGNER" sign --ks "$KS" --ks-pass "pass:$PASS" --key-pass "pass:$PASS" \
  --out build/tianwang.apk build/aligned.apk
echo "[5/5] 签名完成"

SIZE="$(wc -c < build/tianwang.apk | tr -d ' ')"
echo "--------"
echo "产物：tianwang_android/build/tianwang.apk  ${SIZE} 字节"
echo "连接：adb reverse tcp:8000 tcp:8000"
echo "安装：adb install -r tianwang_android/build/tianwang.apk"
