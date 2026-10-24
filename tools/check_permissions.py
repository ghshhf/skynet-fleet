#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""静态门禁（Android 第二条）：运行时权限 vs AndroidManifest 声明。

起因（同类真事故的预防，2026-10-25 补）：
    代码里 `ContextCompat.checkSelfPermission(ctx, Manifest.permission.CAMERA)`
    或 `requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, ...)`，
    但 AndroidManifest.xml 没写对应的 `<uses-permission>` ——
    编译期完全不报，运行时直接 SecurityException / 权限永远拿不到，
    表现成「点了没反应」或「崩了但栈里看不出跟权限有关」。

    `check_viewcast.py` 管的是「布局标签 vs Java 强转类型」（XML ↔ Java）；
    这一条管的是「Java 用到的权限 vs 清单声明」（代码 ↔ 清单）。
    两条互补，都是编译期查不出来、只能静态拦的那类错。

只报「必崩」的情况：
  · Java 引用了 Manifest.permission.X（或字符串 "android.permission.X"），
    但 Manifest 里既没有 `<uses-permission android:name="android.permission.X">`，
    也没有 `<uses-permission-sdk-23 ...>`（Android 6+ 的等价写法）
  · 反向不报：Manifest 声明了但 Java 没引用（很常见，无害）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AND = os.path.join(ROOT, "tianwang_android")
MANIFEST = os.path.join(AND, "AndroidManifest.xml")
SRC = os.path.join(AND, "src")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def declared_permissions(text):
    names = set()
    for m in re.finditer(
            r'<uses-permission(?:-sdk-23)?\s+[^>]*android:name="([^"]+)"', text):
        names.add(m.group(1).strip())
    return names


def used_permissions(src_dir):
    names = set()
    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            if not fn.endswith(".java"):
                continue
            try:
                t = _read(os.path.join(root, fn))
            except OSError:
                continue
            # Manifest.permission.XXX
            for m in re.finditer(r'Manifest\.permission\.([A-Z0-9_]+)', t):
                names.add("android.permission." + m.group(1))
            # 字符串常量 "android.permission.XXX"
            for m in re.finditer(r'"android\.permission\.([A-Z0-9_]+)"', t):
                names.add("android.permission." + m.group(1))
    return names


def main():
    if not os.path.isfile(MANIFEST):
        print("权限门禁 FAIL：找不到", os.path.relpath(MANIFEST, ROOT))
        return 1
    if not os.path.isdir(SRC):
        print("权限门禁 FAIL：找不到源码目录", os.path.relpath(SRC, ROOT))
        return 1

    dec = declared_permissions(_read(MANIFEST))
    use = used_permissions(SRC)
    missing = sorted(use - dec)

    if missing:
        print("权限门禁 FAIL：Java 用到但 AndroidManifest.xml 没声明（运行时会 SecurityException）：")
        for p in missing:
            print("  ·", p)
        print(f"（Manifest 已声明 {len(dec)} 个，Java 引用 {len(use)} 个）")
        return 1

    print(f"[OK] 权限门禁通过：Java 引用 {len(use)} 个运行时权限，"
          f"Manifest 声明 {len(dec)} 个，无缺失")
    return 0


if __name__ == "__main__":
    sys.exit(main())
