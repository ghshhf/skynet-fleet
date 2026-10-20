#!/usr/bin/env bash
# 编译「编队桥」最小无障碍 APK。
#
# 不用 Gradle：Gradle 会拖进几百 MB 依赖和 daemon，这里只有 1 个 Java 类 + 2 个 XML，
# aapt2 + javac + d8 + apksigner 手搓四步就够，产物体积几十 KB。
#
# 用法：bash build.sh
# 产物：build/fleet-bridge.apk 与 build/token.txt（控制端鉴权令牌）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
JAVA="/d/tools/jdk25_raw/jdk-25.0.4.1+1/bin"
BT="/d/DevTools/Android/build-tools/35.0.0"
PLATFORM="/d/DevTools/Android/platforms/android-35/android.jar"
PY="/d/tools/python/python.exe"
PASS="fleetbridge"

export PATH="$JAVA:$PATH"
# d8.bat / apksigner.bat 会读 JAVA_HOME，而系统里那个还指着已消失的 E 盘，
# 必须在本脚本内覆盖成真实 JDK 的 Windows 路径，否则 bat 直接拒绝启动
export JAVA_HOME="D:\\tools\\jdk25_raw\\jdk-25.0.4.1+1"
cd "$HERE"

for f in "$JAVA/javac.exe" "$BT/aapt2.exe" "$BT/d8.bat" "$BT/apksigner.bat" "$PLATFORM"; do
  [ -e "$f" ] || { echo "缺依赖: $f" >&2; exit 1; }
done

rm -rf build
mkdir -p build/classes build/dex build/res build/src/app/fleet/bridge

# ① 令牌：默认复用 .token（重建 APK 后手机上那台仍是同一个令牌，
#    不然每次重装都要重新授权无障碍）。要轮换就 build.sh --new-token
TOKEN_FILE="$HERE/.token"
if [ ! -f "$TOKEN_FILE" ] || [ "${1:-}" = "--new-token" ]; then
  "$PY" -c "import secrets;print(secrets.token_hex(12))" > "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"
sed "s/__TOKEN__/$TOKEN/" src/app/fleet/bridge/BridgeService.java > build/src/app/fleet/bridge/BridgeService.java
echo "$TOKEN" > build/token.txt

# ② 编译 Java：--release 11 产出 class 55，d8 认得
"$JAVA/javac.exe" -encoding UTF-8 --release 11 -classpath "$PLATFORM" \
  -d build/classes build/src/app/fleet/bridge/BridgeService.java
echo "[1/5] javac 完成"

# ③ 转 dex
"$BT/d8.bat" --lib "$PLATFORM" --min-api 26 --release \
  --output build/dex build/classes/app/fleet/bridge/*.class
echo "[2/5] d8 完成"

# ④ 资源 + 资源表
"$BT/aapt2.exe" compile --dir res -o build/res
FLATS=$(find build/res -name '*.flat' | tr '\n' ' ')
# shellcheck disable=SC2086
"$BT/aapt2.exe" link -o build/base.apk -I "$PLATFORM" \
  --manifest AndroidManifest.xml \
  --min-sdk-version 26 --target-sdk-version 35 \
  --version-code 1 --version-name 1.0 \
  --auto-add-overlay $FLATS
echo "[3/5] aapt2 完成"

# ⑤ 塞 dex、对齐、签名
cp build/base.apk build/work.apk
"$PY" - "$HERE/build/work.apk" "$HERE/build/dex/classes.dex" <<'PYEOF'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
PYEOF

if [ ! -f keystore.jks ]; then
  "$JAVA/keytool.exe" -genkeypair -keystore keystore.jks -alias fleetbridge \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$PASS" -keypass "$PASS" \
    -dname "CN=fleet-bridge, OU=fleet, O=fleet, L=Beijing, ST=Beijing, C=CN" >/dev/null 2>&1
  echo "      已生成签名密钥 keystore.jks（不入库）"
fi

"$BT/zipalign.exe" -f -p 4 build/work.apk build/aligned.apk
"$BT/apksigner.bat" sign --ks keystore.jks --ks-pass "pass:$PASS" --key-pass "pass:$PASS" \
  --out build/fleet-bridge.apk build/aligned.apk
echo "[4/5] 签名完成"

SIZE=$("$PY" -c "import os,sys;print(os.path.getsize(sys.argv[1]))" build/fleet-bridge.apk)
echo "[5/5] 产物 build/fleet-bridge.apk  ${SIZE} 字节"
echo "--------"
echo "令牌：$TOKEN"
echo "安装：adb install -r build/fleet-bridge.apk"
echo "授权：手机 设置 → 无障碍 → 已下载的服务 → 「编队桥」→ 打开"
