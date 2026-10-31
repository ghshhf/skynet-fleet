#!/usr/bin/env bash
# 天网 Next · 手机端第二代出包
#
# 与老工程 tianwang_android/build.sh 的区别：
#   老的那份是 aapt2+javac+d8+apksigner 手搓五步，零依赖、产物一百多 KB；
#   这一份走 Gradle（Compose 的依赖树几百 MB，手搓不现实），产物 2 MB 上下。
# 两份并存，互不影响。
#
# 用法：
#   bash tianwang_app/build.sh            # release（推荐，R8 裁资源）
#   bash tianwang_app/build.sh debug      # debug（大很多，只在要连调试器时用）
#
# 需要：JAVA_HOME 有 JDK 17+、ANDROID_HOME 指到 SDK（或本地 local.properties 里写 sdk.dir）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

MODE="${1:-release}"

# SDK 定位：local.properties 优先（Gradle 自己会读），这里只为给出可读的错误提示
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [ -z "$SDK" ] && [ -f local.properties ]; then
  SDK="$(sed -n 's/^sdk\.dir=//p' local.properties | tr -d '\r' | sed 's|\\\\|/|g; s|\\:|:|')"
fi
if [ -z "$SDK" ] || [ ! -d "$SDK" ]; then
  echo "找不到 Android SDK：设 ANDROID_HOME，或在 tianwang_app/local.properties 写 sdk.dir" >&2
  exit 1
fi
echo "SDK      = $SDK"

# Gradle 定位：PATH 上的可能坏（本机踩过：解压多套一层导致缺 lib/）。
# JDK 定位：不能直接信 JAVA_HOME。
# 本机就踩过：JAVA_HOME=E:\DevTools\Java\jdk-21.0.11.10-hotspot，E 盘拔了以后
# ${JAVA_HOME}/bin/java 根本不存在，而报错只会说"找不到能用的 gradle"——
# 把人指向 gradle，真凶是 JDK。所以先验证 java 真能跑，跑不了就换候选。
if [ -z "${JAVA_HOME:-}" ] || [ ! -x "${JAVA_HOME}/bin/java" ]; then
  for j in /d/DevTools/jdk* /d/DevTools/Java/jdk* /e/DevTools/Java/jdk*; do
    if [ -x "$j/bin/java" ]; then export JAVA_HOME="$j"; break; fi
  done
fi
if [ ! -x "${JAVA_HOME:-/nonexistent}/bin/java" ]; then
  echo "找不到可用的 JDK：JAVA_HOME=${JAVA_HOME:-<空>}，候选 /d|/e DevTools 下也没有 jdk*" >&2
  exit 1
fi
echo "JAVA     = $JAVA_HOME"

# 候选制，任一能用即可。通配不加引号才会展开。
GRADLE_BIN="${GRADLE_BIN:-}"
if [ -z "$GRADLE_BIN" ]; then
  for c in "$(command -v gradle 2>/dev/null || true)" \
           /d/DevTools/Gradle/*/bin/gradle \
           /d/DevTools/gradle*/bin/gradle \
           /g/dev/gradle-dist/bin/gradle \
           /g/dev/gradle-8.14/bin/gradle; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" --version >/dev/null 2>&1; then
      GRADLE_BIN="$c"; break
    fi
  done
fi
if [ -z "$GRADLE_BIN" ]; then
  echo "找不到能用的 gradle（PATH 上的那个可能坏了，试试 GRADLE_BIN=/path/to/gradle）" >&2
  exit 1
fi
echo "GRADLE   = $GRADLE_BIN"

case "$MODE" in
  debug)   TASK=":app:assembleDebug";   APK="app/build/outputs/apk/debug/app-debug.apk" ;;
  release) TASK=":app:assembleRelease"; APK="app/build/outputs/apk/release/app-release.apk" ;;
  *) echo "用法：$0 [release|debug]" >&2; exit 2 ;;
esac

"$GRADLE_BIN" "$TASK" --console=plain -q

mkdir -p build
cp "$APK" build/tianwang-next.apk
SIZE="$(wc -c < build/tianwang-next.apk | tr -d ' ')"
echo "--------"
echo "产物：tianwang_app/build/tianwang-next.apk  ${SIZE} 字节（$MODE）"
echo "安装：adb install -r tianwang_app/build/tianwang-next.apk"
echo "注意：包名是 app.skynet.tianwang.next，与老版 app.skynet.tianwang 共存，互不覆盖。"
