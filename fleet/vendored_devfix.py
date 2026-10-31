# -*- coding: utf-8 -*-
"""devfix.* —— 编译/链接错误自动诊断与修复（AutoFix-Skill，Apache-2.0）。

来源：https://github.com/hyz0906/autofix_skills （Apache 2.0）
本机部署：D:/DevTools/devfix/autofix_skills（Python 3.10+，纯标准库可选 ast-grep）
30+ 技能四大类：symbol_header（缺头文件/命名空间/前向声明）、
linkage_dependency（链接库/Rust crate/依赖）、api_type（签名/类型转换/废弃 API）、
build_config（编译 flag/权限/ninja 缓存/GN 语法）。
适配构建系统：CMake / Make / Kbuild / Soong(Android.bp) / GN(BUILD.gn)。
"""

import os

from .core import Capability, Param

AUTOFIX_HOME = "D:/DevTools/devfix/autofix_skills"


def _check_repo(reg, dev):
    if not os.path.isdir(AUTOFIX_HOME):
        return False, "需要 AutoFix-Skill 仓库（git clone 到 %s）" % AUTOFIX_HOME
    if not os.path.isfile(os.path.join(AUTOFIX_HOME, "src", "cli.py")):
        return False, "仓库不完整（缺 src/cli.py）"
    return True, ""


DEVFIX_CAPABILITIES = [
    Capability("devfix.skills", "列出已注册的修复技能与各自覆盖的错误模式",
               params=(), side_effect=False, check=_check_repo,
               note="[DEVFIX] AutoFix-Skill 30+ 技能，Apache-2.0"),
    Capability("devfix.diagnose", "诊断编译/链接错误：解析构建日志 → 分类 → 匹配技能 → 给修复建议（不落盘）",
               params=(Param("log", "str", doc="构建日志文件路径"),
                       Param("error", "str", doc="或直接给一条错误文本"),
                       Param("build_system", "str", doc="cmake/make/kbuild/soong/gn，缺省自动")),
               side_effect=False, check=_check_repo,
               note="[DEVFIX] 只读；建议不含改动"),
    Capability("devfix.autofix", "自动修复：按技能执行修复计划并返回变更（真改文件，慎用）",
               params=(Param("log", "str", True, "构建日志文件路径"),
                       Param("root", "str", doc="项目根目录（缺省用日志所属工程）"),
                       Param("build_system", "str", doc="cmake/make/kbuild/soong/gn"),
                       Param("dry_run", "bool", doc="默认 true，先看不动手")),
               side_effect=True, check=_check_repo,
               note="[DEVFIX] 会改项目文件；默认 dry_run=true 只出计划"),
]
