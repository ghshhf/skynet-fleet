# -*- coding: utf-8 -*-
"""rizin 引擎真 handler：把 REA 的 rizin / ghidra_rizin / ghidra_or_rizin /
rizin_cutter 系能力真正跑起来（底层引擎 = rizin 0.9.1，winget 安装）。

rizin 能原生解析 PE / ELF / Mach-O / 固件：段与节、导入/导出表、资源、证书、
符号、字符串、熵、反汇编、xref、调用图。这部分我们真做，返回结构化数据。
REA 的一部分高层能力（devicetree 解析、崩溃核心转储、重建图谱 / guidance /
compare / trace）单靠 rizin 做不全——这些如实返回「rizin 提供底层反汇编/符号层，
完整能力需 Ghidra 反编译器 / pefile / pwntools / 复刻 REA 逻辑」并附 rizin 已能
拿到的部分数据，绝不谎报「做完了」。

调用方式：rizin 批处理 `rizin -e bin.relocs.apply=true -c "<cmds>" -q -2 <file>`
（-2 静默不打印 banner，-q 跑完退出；JSON 输出用 `-j`）。rizin `-c` 吃的是 rizin
内部命令而非系统 shell，配合 subprocess 列表传参，无 shell 注入风险。

注意：path 来自内部能力调用（不是不可信外部输入），但依旧做存在性校验。
"""
import os
import shutil
import subprocess

from ..core import DeviceResult as R

from .. import text as _fleet_text


def _rizin_bin():
    for c in ("rizin",):
        p = shutil.which(c)
        if p:
            return p
    # 兜底：已知安装目录（沙箱 PATH 可能未及时刷新注册表改动）
    for d in (r"C:\Program Files\Rizin\bin", r"C:\Program Files\rizin\bin"):
        cand = os.path.join(d, "rizin.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _run(cmds, path, timeout=120):
    """跑一组 rizin 命令，返回 (out, err, code)。

    说明：rizin 0.9.1 已不支持旧 r2 的 `-2` 标志；JSON 由命令后缀 `j` 控制
    （如 `iIj` 即「导入表 + JSON」），无需全局 `-j`。`-q` 表示跑完 -c 即退出。
    """
    binp = _rizin_bin()
    if not binp:
        return None, "rizin 可执行文件未找到（PATH 与已知目录均未命中）", 127
    if not path or not os.path.isfile(path):
        return None, "path 不是存在的文件: %r" % path, 2
    flags = ["-e", "bin.relocs.apply=true", "-q"]
    cmdline = [binp] + flags + ["-c", ";".join(cmds), path]
    try:
        proc = _fleet_text.run(cmdline, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return None, "rizin 超时(%ss)" % timeout, 124
    return proc.stdout, proc.stderr, proc.returncode


# cap -> [(rizin命令, 'json'|'text'), ...]；None 表示 rizin 不解析，需其他引擎/逻辑
_RIZIN_CMDS = {
    "rea.open": [("ij", "json")],
    # layout: 二进制基础信息 + 节/段 + 导入/导出表（rizin 原生能拿的都给上）
    "rea.inspect_binary_layout": [("ij", "json"), ("iSj", "json"), ("iij", "json"), ("iEj", "json")],
    "rea.inspect_native_exports": [("iEj", "json")],
    "rea.inspect_pe_imports": [("iij", "json")],
    "rea.inspect_pe_resources": [("iRj", "json")],
    "rea.inspect_pe_certificates": [("iCj", "json")],
    "rea.inspect_pe_load_config": [("ij", "json")],
    "rea.inspect_elf_notes": [("ij", "json")],
    "rea.inspect_kernel_module": [("ij", "json"), ("iSj", "json")],
    "rea.inspect_desktop_application": [("ij", "json")],
    "rea.inspect_byte_entropy": [("p=e", "text")],
    # rizin 不解析 / 需 Ghidra、pefile、pwntools 或复刻 REA 重建逻辑：
    "rea.inspect_devicetree": None,
    "rea.inspect_recorded_crash": None,
    "rea.inspect_apple_ips": None,
}

# 高层能力：rizin 仅提供底层反汇编/符号层，完整能力需 Ghidra / 复刻 REA 逻辑
_RIZIN_STUB = {
    "rea.guided_workflows": "REA 引导式工作流是上层编排，需复刻 REA 逻辑；rizin 仅提供底层分析原语",
    "rea.search_guidance": "REA 指引检索依赖其文档库，需复刻；rizin 不直接提供",
    "rea.read_guidance_section": "REA 指引章节读取依赖其文档库，需复刻；rizin 不直接提供",
    "rea.compare_application_versions": "版本差异比对需复刻 REA 重建逻辑；rizin 可分别提取两二进制符号（用 rea.open / inspect_native_exports）",
    "rea.compare_source_to_bundle": "源码-产物比对需复刻 REA 逻辑；rizin 可提取产物符号做粗比对",
    "rea.compare_javascript_export_shapes": "JS 导出形态比对依赖 JS 引擎分析，非 rizin 范畴",
    "rea.trace_application_feature": "特征追踪需复刻 REA 逻辑 + 反编译器；rizin 提供反汇编/xref 原语",
    "rea.trace_javascript_semantics": "JS 语义追踪依赖 JS 引擎分析，非 rizin 范畴",
    "rea.project_android_application_graph": "应用图谱重建需 Ghidra 反编译器 / 复刻 REA 逻辑；rizin 可提取函数与 xref 做草图",
    "rea.project_apple_application_graph": "应用图谱重建需 Ghidra 反编译器 / 复刻 REA 逻辑；rizin 可提取函数与 xref 做草图",
    "rea.project_harmony_application_graph": "应用图谱重建需 Ghidra 反编译器 / 复刻 REA 逻辑；rizin 可提取函数与 xref 做草图",
    "rea.build_reconstruction_obligation_ledger": "重建义务账本需 Ghidra 反编译器 / 复刻 REA 逻辑；rizin 不提供",
    "rea.evaluate_reconstruction_coverage": "重建覆盖率评估需 Ghidra 反编译器 / 复刻 REA 逻辑；rizin 不提供",
    "rea.start_rizin_debug_session": "调试会话管理需 r2pipe 持久会话，本 handler 暂未实现（rizin -d 可单发命令调试）",
    "rea.close_rizin_debug_session": "调试会话管理需 r2pipe 持久会话，本 handler 暂未实现",
    "rea.rizin_debug_session_status": "调试会话管理需 r2pipe 持久会话，本 handler 暂未实现",
    "rea.rizin_debug_command": "调试命令需 r2pipe 持久会话，本 handler 暂未实现（用 rea.execute_rizin_command 跑单条）",
    "rea.cutter_command": "Cutter 是 rizin 的图形前端；命令行分析请用 rea.execute_rizin_command",
    "rea.list_cutter_sessions": "Cutter 会话管理不在本 handler 范畴；用 rizin CLI 直接分析",
}


def _h_execute_rizin_command(dev, cap, args):
    """万能透传：在 path 上跑任意 rizin 命令串。"""
    command = (args.get("command") or "").strip()
    path = (args.get("path") or "").strip()
    if not command:
        return R.error(cap, "缺参数 command（rizin 命令串）", device=dev.name, via=dev.via)
    if not path:
        return R.error(cap, "缺参数 path（待分析文件）", device=dev.name, via=dev.via)
    if not os.path.isfile(path):
        return R.error(cap, "path 不存在：%s" % path, device=dev.name, via=dev.via)
    out, err, code = _run([command], path)
    if out is None:
        return R.error(cap, err, device=dev.name, via=dev.via)
    if code != 0 and not out.strip():
        return R.error(cap, "rizin 返回非零(%d)：%s" % (code, err[:300]),
                       device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via,
                     data={"command": command, "path": path,
                           "output": out[:200000],
                           "truncated": len(out) > 200000})


def _h_objdump(dev, cap, args):
    """rea.inspect_with_objdump：用本机已装的 objdump 做反汇编/头信息。"""
    objdump = shutil.which("objdump")
    if not objdump:
        return R.unavailable(cap, "objdump 未检测到（Strawberry 工具链里才有）",
                             device=dev.name, via=dev.via)
    path = (args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return R.error(cap, "缺参数 path（待分析文件）", device=dev.name, via=dev.via)
    flags = (args.get("flags") or "-d").strip()
    try:
        proc = _fleet_text.run([objdump] + flags.split() + [path],
                              timeout=120,
                              errors="replace")
    except subprocess.TimeoutExpired:
        return R.error(cap, "objdump 超时", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via,
                     data={"path": path, "flags": flags,
                           "output": proc.stdout[:200000],
                           "truncated": len(proc.stdout) > 200000})


def _h_rizin(dev, cap, args):
    """rizin 系四个引擎的统一入口（rizin / ghidra_rizin / ghidra_or_rizin / rizin_cutter）。"""
    if cap == "rea.execute_rizin_command":
        return _h_execute_rizin_command(dev, cap, args)
    if cap == "rea.inspect_with_objdump":
        return _h_objdump(dev, cap, args)

    # 高层 stub：rizin 做不全，诚实说明
    if cap in _RIZIN_STUB:
        return R.unavailable(cap, "rizin 仅提供底层分析原语：" + _RIZIN_STUB[cap],
                             device=dev.name, via=dev.via)

    spec = _RIZIN_CMDS.get(cap)
    if spec is None:
        return R.unavailable(
            cap, "rizin 不原生解析该格式/能力（需 Ghidra / pefile / pwntools / 复刻 REA 逻辑）",
            device=dev.name, via=dev.via)

    path = (args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return R.error(cap, "缺参数 path（待分析文件）", device=dev.name, via=dev.via)

    blocks = {}
    for cmd, mode in spec:
        out, err, code = _run([cmd], path)
        if out is None:
            return R.error(cap, err, device=dev.name, via=dev.via)
        if mode == "json":
            try:
                import json
                blocks[cmd] = json.loads(out)
            except Exception:
                blocks[cmd] = {"_raw": out[:50000],
                               "_note": "rizin JSON 解析失败，附原文"}
        else:
            blocks[cmd] = out[:50000]
    return R.success(cap, dev.name, via=dev.via,
                     data={"path": path, "blocks": blocks,
                           "engine": "rizin"})
