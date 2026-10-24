#!/usr/bin/env python
"""统计数字生成器：门禁数 / eval 文件数 / 能力数 / 文档数 / App build。

总纲 §三 P3-17 与 §六 数字纪律：文档里的一切进度数字必须由本脚本产出，
禁止手写。手填的进度表必然过期（漂移史：门禁 52/58/67/74 四数并存、
文档 30+/71/75、能力 87/109/121）。

用法：
    python tools/gen_stats.py            # 打印 + 写 docs/_stats.json
    python tools/gen_stats.py --json     # 只打印 JSON（给人 CI/脚本读）
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def gate_count():
    """run_selftests.py 里实注册的用例数（AST 读 CASES，不执行它）。"""
    path = os.path.join(ROOT, "run_selftests.py")
    tree = ast.parse(_read(path), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "CASES" for t in node.targets):
            if isinstance(node.value, ast.List):
                return len(node.value.elts)
    return 0


def eval_file_count():
    d = os.path.join(ROOT, "evals")
    try:
        return len([f for f in os.listdir(d) if f.endswith(".py")])
    except OSError:
        return 0


def doc_count():
    d = os.path.join(ROOT, "docs")
    try:
        return len([f for f in os.listdir(d) if f.endswith(".md")])
    except OSError:
        return 0


def app_build():
    try:
        return _read(os.path.join(ROOT, "tianwang_android", "version.txt")).strip()
    except OSError:
        return "?"


def capability_count():
    """catalog.py 里登记的 Capability(...) 条数（AST 计数，不 import 设备）。"""
    path = os.path.join(ROOT, "fleet", "catalog.py")
    tree = ast.parse(_read(path), filename=path)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Capability":
            n += 1
    return n


def collect():
    return {
        "gates_registered": gate_count(),
        "eval_files": eval_file_count(),
        "capabilities_catalog": capability_count(),
        "doc_md": doc_count(),
        "app_build": app_build(),
    }


def main():
    st = collect()
    if "--json" in sys.argv:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    out = os.path.join(ROOT, "docs", "_stats.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("天网数字（脚本生成，勿手填）：")
    for k, v in st.items():
        print(f"  {k}: {v}")
    print(f"  已写入 {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
