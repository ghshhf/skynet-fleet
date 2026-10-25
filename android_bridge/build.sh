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
PASS="fleetbridge"

# ---- 工具链定位：候选制，别写死（2026-10-26 改）----
# 原先 JAVA/BT/PLATFORM/PY 四个路径全写死在 `/d/tools/...`、`/d/DevTools/...`，
# D 盘整体搬走之后脚本第一步就 `缺依赖: .../javac.exe` 直接退，而它想干的事
# （编那个几十 KB 的桥 APK）一点没变。写死路径 = 换个盘就废，和「不写死设备」
# 是同一条道理：**能力跟着探测结果走，不跟着上一次的目录结构走**。
pick() { for c in "$@"; do [ -e "$c" ] && { printf '%s' "$c"; return 0; }; done; return 1; }

SDK="$(pick "${ANDROID_SDK_ROOT:-}" /g/sdk /d/DevTools/Android "$HOME/Android/Sdk" || true)"
JAVA_BIN="$(pick "${JAVA_HOME:-}/bin" /g/_tools/jdk-25/bin /d/tools/jdk25_raw/jdk-25.0.4.1+1/bin || true)"
BT="$(pick "${SDK:+$SDK/build-tools/35.0.1}" "${SDK:+$SDK/build-tools/35.0.0}" \
           ${SDK:+$SDK/build-tools/37.0.0} ${SDK:+$SDK/build-tools/34.0.0} || true)"
PLATFORM="$(pick "${SDK:+$SDK/platforms/android-35/android.jar}" \
                ${SDK:+$SDK/platforms/android-36/android.jar} || true)"
PY="$(pick /g/Python312/python.exe /d/tools/python/python.exe "$(command -v python 2>/dev/null || true)" || true)"

[ -n "$SDK" ]      || { echo "找不到 Android SDK（试过 /g/sdk、/d/DevTools/Android）" >&2; exit 1; }
[ -n "$JAVA_BIN" ] || { echo "找不到 JDK（试过 /g/_tools/jdk-25、/d/tools/jdk25_raw）" >&2; exit 1; }
[ -n "$BT" ]       || { echo "找不到 build-tools（在 $SDK/build-tools 下）" >&2; exit 1; }
[ -n "$PLATFORM" ] || { echo "找不到 platforms/android-35/android.jar（SDK=$SDK）" >&2; exit 1; }
[ -n "$PY" ]       || { echo "找不到 python" >&2; exit 1; }
JAVA="$JAVA_BIN"

export PATH="$JAVA:$PATH"
# d8.bat / apksigner.bat 会读 JAVA_HOME，而系统里那个可能指着已经不存在的盘，
# 必须在本脚本内覆盖成真实 JDK 的 **Windows** 路径（bat 不认 /g/... 这种 MSYS 路径）
export JAVA_HOME="$(cygpath -w "$(dirname "$JAVA_BIN")" 2>/dev/null || echo "$JAVA_BIN")"
echo "SDK=$SDK"; echo "JAVA_HOME=$JAVA_HOME"; echo "BT=$BT"
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
cp src/app/fleet/bridge/*.java build/src/app/fleet/bridge/
sed -i "s/__TOKEN__/$TOKEN/" build/src/app/fleet/bridge/BridgeService.java
echo "$TOKEN" > build/token.txt

# ② 编译 Java：--release 11 产出 class 55，d8 认得
"$JAVA/javac.exe" -encoding UTF-8 --release 11 -classpath "$PLATFORM" \
  -d build/classes build/src/app/fleet/bridge/*.java
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
echo "安装：adb install --no-streaming -r build/fleet-bridge.apk"
# 🔴 必须 --no-streaming（2026-10-26 实测）：MIUI/HyperOS 对**流式安装**直接回
#    INSTALL_FAILED_USER_RESTRICTED: Install canceled by user（就算屏幕亮着、
#    人就守着也一样，它不给弹窗）。换成 push 安装（--no-streaming）一次就过，
#    全程不用点屏幕。
# 🔴 而且**别 uninstall 再装**：卸载后无障碍授权一起没了，而这代 ROM 不让
#    adb 写 secure settings（`settings put` 与 `pm grant` 全被拒），
#    只能人去「设置 → 无障碍 → 编队桥」手开一次。
#    只要 keystore.jks 还在（本脚本会复用，不会重新生成），
#    覆盖安装就是无痛的一条命令。
echo "授权：手机 设置 → 无障碍 → 已下载的服务 → 「编队桥」→ 打开"
