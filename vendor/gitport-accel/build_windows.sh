#!/usr/bin/env bash
# gitport 编译脚本（Git Bash）：把「本机为什么编译不过」的三条环境依赖固化下来。
#
# 2026-10-31 踩坑实录：
#   ① RUSTUP_HOME / CARGO_HOME 曾被指到 E 盘，E 盘一拔整条工具链消失
#      → 这里显式优先用 C 盘的 ~/.cargo 和 ~/.rustup。
#   ② boringssl 走 bindgen，硬依赖 libclang.dll。LLVM 装在 E 盘，一起没了
#      → 这里按顺序找：仓库内 tools/.libclang → 常见 LLVM 安装路径。
#      仓库内那份可以用 pip 快速补齐：
#        pip install libclang --proxy http://127.0.0.1:3067 --target tools/.libclang
#   ③ 有 libclang 还不够：clang 看不到 MinGW 的 C 头文件，
#      报 base.h:59 'stddef.h' file not found
#      → 必须给 BINDGEN_EXTRA_CLANG_ARGS 指定 --target 和 MinGW include。
#
#   用法：./build_windows.sh [debug]
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"

MODE="${1:-release}"
if [ "$MODE" = "release" ]; then
  CARGO_ARGS=(--release)
else
  CARGO_ARGS=()
fi

# ---- ① Rust 工具链：C 盘优先 ----
if [ -x "$HOME/.cargo/bin/cargo" ]; then
  export PATH="$HOME/.cargo/bin:$PATH"
  export CARGO_HOME="$HOME/.cargo"
  [ -d "$HOME/.rustup" ] && export RUSTUP_HOME="$HOME/.rustup"
fi
command -v cargo >/dev/null || { echo "[x] 找不到 cargo" >&2; exit 1; }

export CARGO_BUILD_TARGET="${CARGO_BUILD_TARGET:-x86_64-pc-windows-gnu}"

# ---- ② libclang：仓库内优先，其次常见 LLVM 路径 ----
find_libclang() {
  for d in \
    "$ROOT/tools/.libclang/clang/native" \
    "/c/Program Files/LLVM/bin" \
    "/c/Program Files (x86)/LLVM/bin" \
    "/d/DevTools/LLVM/bin"
  do
    if [ -f "$d/libclang.dll" ] || [ -f "$d/clang.dll" ]; then
      echo "$d"; return 0
    fi
  done
  return 1
}
if LIBCLANG_DIR="$(find_libclang)"; then
  export LIBCLANG_PATH="$LIBCLANG_DIR"
else
  cat >&2 <<'EOF'
[x] 找不到 libclang.dll，boringssl 的 bindgen 会失败。
    补一份（约 20MB，走代理最快）：
      pip install libclang --proxy http://127.0.0.1:3067 \
          --target <repo>/tools/.libclang
EOF
  exit 1
fi

# ---- ③ MinGW include：从 gcc 反推，别写死盘符 ----
MINGW_GCC="$(command -v x86_64-w64-mingw32-gcc || true)"
if [ -z "$MINGW_GCC" ]; then
  echo "[x] 找不到 x86_64-w64-mingw32-gcc（MinGW）" >&2
  exit 1
fi
# gcc 在 <root>/bin/，头文件在 <root>/x86_64-w64-mingw32/include
MINGW_ROOT="$(cd "$(dirname "$MINGW_GCC")/.." && pwd)"
MINGW_INC="$MINGW_ROOT/x86_64-w64-mingw32/include"
if [ ! -f "$MINGW_INC/stddef.h" ]; then
  echo "[x] $MINGW_INC/stddef.h 不存在，MinGW 头文件不全" >&2
  exit 1
fi
# 光有 MinGW include 还不够：malloc.h 会 #include <mm_malloc.h>，
# 那是 GCC 自己的内部头文件，在 lib/gcc/<target>/<ver>/include 下，得单独给。
GCC_INC=""
for d in "$MINGW_ROOT"/lib/gcc/x86_64-w64-mingw32/*/include; do
  [ -f "$d/mm_malloc.h" ] && GCC_INC="$d" && break
done
if [ -z "$GCC_INC" ]; then
  echo "[x] 找不到 GCC 内部 include（mm_malloc.h）" >&2
  exit 1
fi
export BINDGEN_EXTRA_CLANG_ARGS="--target=x86_64-pc-windows-gnu -isystem $(cygpath -m "$MINGW_INC") -isystem $(cygpath -m "$GCC_INC")"

echo "  cargo      : $(command -v cargo)"
echo "  target     : $CARGO_BUILD_TARGET"
echo "  libclang   : $LIBCLANG_PATH"
echo "  mingw inc  : $MINGW_INC"
echo "  gcc inc    : $GCC_INC"
echo

exec cargo build "${CARGO_ARGS[@]}"
