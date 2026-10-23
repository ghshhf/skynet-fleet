#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""静态门禁：XML 布局标签 vs Java 强转类型，不一致就报错。

起因（真事故，2026-10-24）：
    activity_device.xml 里 btnDevBack 是 <TextView>，
    DeviceCheckActivity 里却声明 `private Button btnBack;`，
    点「基准测试」必崩 ——
        java.lang.ClassCastException: TextView cannot be cast to Button
        at DeviceCheckActivity.onCreate(DeviceCheckActivity.java:60)

这种错**编译期查不出来**（findViewById 是泛型方法，编译器按声明类型推导，
不校验布局），只能在真机上点一下才知道。所以必须做静态检查：
读布局 XML 建 id->tag 表，再扫 Java 的 findViewById 赋值/强转目标类型，
两者不一致就红。

只报「必崩」的错，不报安全的情况：
  · 目标类型是 View / ViewGroup —— 任何标签都能接，不报
  · Android 的合法继承（如 XML 写 <Button>，Java 声明 TextView）—— 不报
    反过来（XML <TextView> 转 Button）才是必崩，要报
用法：python tools/check_viewcast.py    退出码 0 = 通过
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUTS = os.path.join(ROOT, 'tianwang_android', 'res', 'layout')
SRC = os.path.join(ROOT, 'tianwang_android', 'src')

#: XML 标签 -> 它在 Java 侧能被安全当成哪些类型（含自身与**父类**）
#: 只有「Java 要的类型不在表里」才是必崩。Button 是 TextView 的子类，
#: 所以 XML <Button> 当 TextView 用没事；XML <TextView> 当 Button 用必崩。
SAFE = {
    'View': {'View'},
    'TextView': {'TextView', 'View'},
    'Button': {'Button', 'TextView', 'View'},
    'EditText': {'EditText', 'TextView', 'View'},
    'ImageView': {'ImageView', 'View'},
    'ImageButton': {'ImageButton', 'ImageView', 'View'},
    'LinearLayout': {'LinearLayout', 'ViewGroup', 'View'},
    'RelativeLayout': {'RelativeLayout', 'ViewGroup', 'View'},
    'FrameLayout': {'FrameLayout', 'ViewGroup', 'View'},
    'ScrollView': {'ScrollView', 'FrameLayout', 'ViewGroup', 'View'},
    'HorizontalScrollView': {'HorizontalScrollView', 'FrameLayout', 'ViewGroup', 'View'},
    'RecyclerView': {'RecyclerView', 'ViewGroup', 'View'},
    'SeekBar': {'SeekBar', 'View'},
    'Switch': {'Switch', 'Button', 'TextView', 'View'},
    'CheckBox': {'CheckBox', 'Button', 'TextView', 'View'},
    'ProgressBar': {'ProgressBar', 'View'},
    'OrbView': {'OrbView', 'View'},
}


def layout_ids():
    """扫所有布局 XML，建 id -> (标签去掉包名, 文件名)。"""
    table = {}
    for p in sorted(glob.glob(os.path.join(LAYOUTS, '*.xml'))):
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as e:
            print('  [XML 解析失败] %s: %s' % (p, e))
            continue
        for el in root.iter():
            rid = el.get('{http://schemas.android.com/apk/res/android}id')
            if rid and rid.startswith('@+id/'):
                table.setdefault(rid[5:], (el.tag.split('.')[-1], os.path.basename(p)))
    return table


def java_files():
    return sorted(glob.glob(os.path.join(SRC, '**', '*.java'), recursive=True))


def check():
    ids = layout_ids()
    if not ids:
        print('  跳过：没找到布局文件 %s' % LAYOUTS)
        return 0
    bad = []
    for p in java_files():
        s = open(p, encoding='utf-8').read()
        rel = os.path.relpath(p, ROOT).replace('\\', '/')

        # 形态 1：显式强转  (Button) findViewById(R.id.x)
        for m in re.finditer(r'\(([\w.]+)\)\s*[\w.]*findViewById\(R\.id\.(\w+)\)', s):
            want = m.group(1).split('.')[-1]
            rid = m.group(2)
            if rid in ids:
                tag, lay = ids[rid]
                if want not in SAFE.get(tag, {tag}):
                    bad.append((rel, s[:m.start()].count('\n') + 1, want, rid, tag, lay))

        # 形态 2：字段声明 + 赋值  private Button b;  b = findViewById(R.id.x);
        dm = re.compile(r'private\s+([\w.<>]+?)\s+([\w,\s]+?);')
        for m in dm.finditer(s):
            typ = m.group(1).split('.')[-1].replace('[]', '')
            for name in [x.strip() for x in m.group(2).split(',')]:
                if not name or typ in ('View', 'ViewGroup'):
                    continue
                fm = re.search(r'\b' + re.escape(name) + r'\s*=\s*\(?\s*findViewById\(R\.id\.(\w+)\)', s)
                if not fm:
                    continue
                rid = fm.group(1)
                if rid not in ids:
                    continue
                tag, lay = ids[rid]
                if typ not in SAFE.get(tag, {tag}):
                    bad.append((rel, s[:fm.start()].count('\n') + 1, typ, rid, tag, lay))

    # 去重（同一处可能被两条规则都命中）
    seen, out = set(), []
    for b in bad:
        k = (b[0], b[2], b[3])
        if k not in seen:
            seen.add(k)
            out.append(b)

    if out:
        print('  [FAIL] XML 布局与 Java 类型不匹配 %d 处（真机点一下必崩）：' % len(out))
        for rel, line, want, rid, tag, lay in out:
            print('    %s:%d  Java 要 %s，但 R.id.%s 在 %s 里是 <%s>' % (rel, line, want, rid, lay, tag))
        return 1
    print('  [OK] %d 个布局 id 与 Java 强转类型全部一致' % len(ids))
    return 0


if __name__ == '__main__':
    sys.exit(check())
