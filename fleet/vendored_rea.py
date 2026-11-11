# -*- coding: utf-8 -*-
"""REA 能力注入：从 morluto/rea 提取的逆向工程能力（方案 A：全量声明 + 诚实依赖检查）。

来源仓库 clone 于 D:/code/rea。每个能力的 check= 探测对应底层引擎（Ghidra/JADX/rizin
adb/frida...）是否在场；不在则如实下线，不谎报可用。装好引擎并接入 CLI handler 后，
对应能力自动从「声明」变「实跑」（见 fleet/kinds/rea_device.py）。

命名空间：2026-11 归并——原 rea.* 领域能力并入统一的 re. 命名空间
（rea.native→re.native / rea.mobile→re.mobile / …）。对外只露这 6 个 re.* 领域能力，
子动作走 action 参数。内部 ENGINE_OF 仍用 rea.<动作> 做「动作名→引擎」路由，属内部键，
与对外能力名是两回事，不要混改。
"""
import os
import shutil
import sys as _sys
from .core import Capability, Param

# 已知安装根：本机为 Windows 岗机，winget / 手动安装后的常见落地目录。
# 沙箱环境 PATH 可能未及时刷新注册表改动，这里在 import 时把已存在的根目录补进 PATH，
# 既让 check 诚实可用，也让 handler 能直接调起引擎（不依赖会话 PATH 状态）。
_py_scripts = os.path.join(os.path.dirname(_sys.executable), "Scripts")  # pip 装 frida/binwalk 等 CLI
for _d in (r"C:\Program Files\Rizin\bin", r"C:\Program Files\rizin\bin",
           os.environ.get("FLEET_REVERSE_BIN") or "",
           os.environ.get("FLEET_JADX_BIN") or "",
           r"C:\Program Files\Wireshark",     # tshark.exe
           _py_scripts):                      # pip 装的 frida / binwalk / mitmproxy 等 CLI
    if _d and os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(";"):
        os.environ["PATH"] = _d + ";" + os.environ.get("PATH", "")

_ENGINE_CMDS = {
    'adb': (['adb'], 'adb (Android SDK platform-tools)'),
    'apktool': (['apktool', 'apktool.cmd'], 'apktool'),
    'sqlite3': ([], 'Python 标准库 sqlite3（自带）'),
    'rizin': (['rizin'], 'rizin'),
    'rizin_cutter': (['rizin', 'r2', 'cutter'], 'rizin 或 cutter'),
    'ghidra_or_rizin': (['rizin', 'r2', 'ghidra'], 'rizin 或 Ghidra'),
    'ghidra_rizin': (['ghidra', 'rizin', 'r2'], 'Ghidra 或 rizin'),
    'dotnet_ilspy': (['dotnet'], 'dotnet + ilspy'),
    'binwalk': (['binwalk'], 'binwalk'),
    'adb_apktool': (['adb', 'apktool'], 'adb + apktool'),
    'gdb_pwndbg': (['gdb'], 'gdb / pwndbg'),
    'frida': (['frida'], 'frida (PC 端 frida-tools；手机端 frida-server 另推)'),
    'evm_tools': (['evm'], 'evm 工具链'),
    'go_dump': (['go'], 'go 工具链'),
    'llm': ([], '本地/远程 LLM 服务'),
    'il2cpp_dumper': (['il2cpp-dumper'], 'il2cpp-dumper'),
    'rust_decompiler': (['cargo', 'rustc'], 'rust 工具链（cargo/rustc）'),
    'python_uncompyle': (['uncompyle6', 'decompyle3'], 'uncompyle6 / decompyle3'),
    'erlang_beam': (['erl'], 'Erlang/OTP'),
    'wasm_tools': (['wasm-objdump'], 'wasm-tools / wasm-objdump'),
    'chromium': (['chromium', 'chrome', 'chrome.exe'], 'Chromium / Chrome'),
    'node': (['node', 'node.exe'], 'Node.js'),
    'node_chromium': (['node', 'chromium'], 'Node.js + Chromium'),
    'node_mitmproxy': (['node', 'mitmproxy'], 'Node.js + mitmproxy'),
    'mixed': ([], '混合依赖（视具体工具）'),
    'none': ([], 'REA 内部/会话能力，无外部依赖'),
    'apktool_keytool': (['apktool', 'keytool'], 'apktool + keytool'),
    'flutter': (['flutter'], 'Flutter SDK'),
    'tshark': (['tshark', 'wireshark'], 'tshark / Wireshark'),
    'jadx': (['jadx'], 'jadx (DEX/APK 反编译)'),
    'jeb': (['jeb'], 'JEB (商业)'),
    'gpu_tools': (['nvidia-smi'], 'nvidia-smi'),
}

# 复合引擎中"任一满足即可"的集合；其余默认"全部命令必须都在场"（诚实语义）
_OR_ENGINES = {'ghidra_or_rizin', 'ghidra_rizin', 'rizin_cutter'}

def _check(cmds, label, reg, dev, or_mode=False):
    if not cmds:
        return True, ""
    if or_mode:
        for c in cmds:
            if shutil.which(c):
                return True, ""
        return False, "需要以下任一：%s（均未检测到）" % label
    missing = [c for c in cmds if not shutil.which(c)]
    if missing:
        return False, "需要 %s（未检测到：%s）" % (label, ", ".join(missing))
    return True, ""

def _mk(engine):
    cmds, label = _ENGINE_CMDS.get(engine, ([], engine))
    or_mode = engine in _OR_ENGINES
    return lambda reg, dev: _check(cmds, label, reg, dev, or_mode)


def _mk_any(engines):
    """域级 check：该域涉及的引擎**任一在场**即视为可用。

    2026-11-03 归并时加的。原子能力折叠成领域能力后，一个域会横跨多个引擎
    （rea.native 同时要 rizin / tshark / binwalk / node…）。若沿用「全部必须到齐」，
    装了一个引擎也整域下线，比折叠前更不可用。所以这里只判「至少有一条路能走」，
    具体子动作用哪个引擎、那个引擎在不在，留到 invoke 时按 ENGINE_OF 精确判——
    宁可入口可用、子动作如实报缺，也不要整域假装不可用。
    """
    uniq = []
    for e in engines:
        if e not in uniq:
            uniq.append(e)
    has_free = "none" in uniq

    def _f(reg, dev):
        if has_free:
            return True, ""
        missing = []
        for e in uniq:
            cmds, label = _ENGINE_CMDS.get(e, ([], e))
            ok, why = _check(cmds, label, reg, dev, e in _OR_ENGINES)
            if ok:
                return True, ""
            missing.append(label)
        return False, "需要以下任一引擎：%s（均未检测到）" % " / ".join(missing)

    return _f

ENGINE_OF = {
    'rea.inspect_analysis_view': 'none',
    'rea.search_guidance': 'ghidra_rizin',
    'rea.guided_workflows': 'ghidra_rizin',
    'rea.read_guidance_section': 'ghidra_rizin',
    'rea.trace_application_feature': 'ghidra_rizin',
    'rea.trace_javascript_semantics': 'ghidra_rizin',
    'rea.compare_application_versions': 'ghidra_rizin',
    'rea.compare_source_to_bundle': 'ghidra_rizin',
    'rea.compare_javascript_export_shapes': 'ghidra_rizin',
    'rea.build_reconstruction_obligation_ledger': 'ghidra_rizin',
    'rea.evaluate_reconstruction_coverage': 'ghidra_rizin',
    'rea.project_android_application_graph': 'ghidra_rizin',
    'rea.project_apple_application_graph': 'ghidra_rizin',
    'rea.project_harmony_application_graph': 'ghidra_rizin',
    'rea.capture_browser_scenario': 'chromium',
    'rea.list_browser_targets': 'chromium',
    'rea.inspect_web_page': 'chromium',
    'rea.analyze_web_bundle': 'chromium',
    'rea.observe_web_session': 'chromium',
    'rea.discover_webmcp_tools': 'chromium',
    'rea.compare_web_captures': 'chromium',
    'rea.capture_web_screenshot': 'chromium',
    'rea.compare_web_screenshots': 'chromium',
    'rea.list_cutter_sessions': 'rizin_cutter',
    'rea.cutter_command': 'rizin_cutter',
    'rea.inspect_desktop_application': 'ghidra_rizin',
    'rea.export_environment_bundle': 'none',
    'rea.grafana': 'none',
    'rea.list_frida_devices': 'frida',
    'rea.list_frida_processes': 'frida',
    'rea.start_frida_session': 'frida',
    'rea.load_frida_script': 'frida',
    'rea.resume_frida_session': 'frida',
    'rea.unload_frida_script': 'frida',
    'rea.frida_session_status': 'frida',
    'rea.close_frida_session': 'frida',
    'rea.instrument_with_frida': 'frida',
    'rea.start_gdb_session': 'gdb_pwndbg',
    'rea.gdb_console': 'gdb_pwndbg',
    'rea.gdb_session_status': 'gdb_pwndbg',
    'rea.close_gdb_session': 'gdb_pwndbg',
    'rea.inspect_with_objdump': 'ghidra_rizin',
    'rea.execute_rizin_command': 'ghidra_rizin',
    'rea.start_rizin_debug_session': 'rizin',
    'rea.rizin_debug_command': 'rizin',
    'rea.rizin_debug_session_status': 'rizin',
    'rea.close_rizin_debug_session': 'rizin',
    'rea.inspect_web_network_capture': 'node_mitmproxy',
    'rea.observe_web_execution': 'node_chromium',
    'rea.inspect_web_event_listeners': 'node_chromium',
    'rea.trace_web_source_location': 'node',
    'rea.trace_web_module_imports': 'node',
    'rea.export_web_scripts': 'node',
    'rea.inspect_adb_client': 'adb',
    'rea.list_adb_devices': 'adb',
    'rea.inspect_adb_device': 'adb',
    'rea.list_adb_packages': 'adb',
    'rea.pull_adb_package': 'adb',
    'rea.read_adb_logcat': 'adb',
    'rea.inspect_adb_package': 'adb',
    'rea.pull_adb_file': 'adb',
    'rea.push_adb_file': 'adb',
    'rea.capture_adb_screen': 'adb',
    'rea.list_adb_processes': 'adb',
    'rea.list_adb_directory': 'adb',
    'rea.list_adb_features': 'adb',
    'rea.list_adb_services': 'adb',
    'rea.inspect_adb_display': 'adb',
    'rea.inspect_adb_window': 'adb',
    'rea.read_adb_setting': 'adb',
    'rea.collect_adb_bugreport': 'adb',
    'rea.resolve_adb_packages': 'adb',
    'rea.install_adb_package': 'adb',
    'rea.uninstall_adb_package': 'adb',
    'rea.start_adb_app': 'adb',
    'rea.start_adb_activity': 'adb',
    'rea.stop_adb_app': 'adb',
    'rea.inspect_android_jni_bindings': 'adb_apktool',
    'rea.inspect_android_package': 'adb_apktool',
    'rea.search_android_classes': 'adb_apktool',
    'rea.inspect_android_class': 'adb_apktool',
    'rea.inspect_android_method': 'adb_apktool',
    'rea.trace_android_references': 'adb_apktool',
    'rea.notify': 'adb_apktool',
    'rea.inspect_apk_signature': 'apktool_keytool',
    'rea.inspect_apktool_client': 'apktool',
    'rea.decode_android_resources': 'apktool',
    'rea.inspect_packet_fields': 'none',
    'rea.inspect_packet_capture': 'none',
    'rea.inspect_erlang_beam': 'erlang_beam',
    'rea.inspect_evm_interface': 'evm_tools',
    'rea.inspect_firmware_regions': 'binwalk',
    'rea.extract_firmware': 'binwalk',
    'rea.identify_flutter_build': 'flutter',
    'rea.inspect_dart_aot': 'flutter',
    'rea.inspect_go_binary': 'go_dump',
    'rea.inspect_gpu_code_objects': 'gpu_tools',
    'rea.inspect_il2cpp_metadata': 'il2cpp_dumper',
    'rea.list_electron_targets': 'node',
    'rea.inspect_electron_page': 'node',
    'rea.analyze_javascript_application': 'node',
    'rea.reconcile_javascript_runtime': 'node',
    'rea.capture_electron_scenario': 'node',
    'rea.inspect_electron_fuses': 'node',
    'rea.recover_javascript_sources': 'node',
    'rea.list_javascript_runtime_targets': 'node',
    'rea.observe_javascript_runtime': 'node',
    'rea.inspect_jeb_client': 'jeb',
    'rea.open_jeb_project': 'jeb',
    'rea.list_jeb_units': 'jeb',
    'rea.decompile_jeb_item': 'jeb',
    'rea.inspect_managed_artifact': 'dotnet_ilspy',
    'rea.inspect_managed_members': 'dotnet_ilspy',
    'rea.inspect_managed_native_boundaries': 'dotnet_ilspy',
    'rea.compare_managed_members': 'dotnet_ilspy',
    'rea.verify_managed_native_boundaries': 'dotnet_ilspy',
    'rea.import_managed_reconstruction': 'dotnet_ilspy',
    'rea.project_managed_application_graph': 'dotnet_ilspy',
    'rea.inspect_model_artifact': 'llm',
    'rea.inspect_devicetree': 'ghidra_or_rizin',
    'rea.inspect_pe_load_config': 'ghidra_or_rizin',
    'rea.inspect_elf_notes': 'ghidra_or_rizin',
    'rea.inspect_apple_ips': 'ghidra_or_rizin',
    'rea.inspect_kernel_module': 'ghidra_or_rizin',
    'rea.inspect_byte_entropy': 'ghidra_or_rizin',
    'rea.inspect_pe_certificates': 'ghidra_or_rizin',
    'rea.inspect_pe_imports': 'ghidra_or_rizin',
    'rea.inspect_pe_resources': 'ghidra_or_rizin',
    'rea.inspect_native_exports': 'ghidra_or_rizin',
    'rea.inspect_binary_layout': 'ghidra_or_rizin',
    'rea.inspect_recorded_crash': 'ghidra_or_rizin',
    'rea.open': 'ghidra_or_rizin',
    'rea.inspect_pcap_client': 'tshark',
    'rea.inspect_pcap_capture': 'tshark',
    'rea.inspect_python_pyc': 'python_uncompyle',
    'rea.inspect_rust_binary': 'rust_decompiler',
    'rea.inspect_signal_decoders': 'none',
    'rea.decode_signal_recording': 'none',
    'rea.inspect_sqlite_database': 'sqlite3',
    'rea.inspect_wasm_artifact': 'wasm_tools',
}

# ==========================================================================
# 领域级能力（2026-11-03 归并）：140 个引擎原子动作 → 6 个领域
#
# 为什么折叠：原来把外部引擎的每个 CLI 动作 1:1 注册成能力——Frida 一个会话
# 拆成 start/load/resume/unload/status/close/instrument 七项，GDB 拆四项。
# 那是「点击鼠标算一项、点击右键再算一项」，能力表虚高、真实能力被淹没。
#
# 折叠后：catalog 只露领域能力，子动作走 action 参数（原原子名去掉 rea. 前缀）。
# 内部路由不变：ENGINE_OF 仍是「原子名 → 引擎」，rea_device 按 action 反查它。
# ==========================================================================
DOMAINS = {
    're.native': (
        'compare_managed_members', 'decode_signal_recording',
        'decompile_jeb_item', 'export_environment_bundle',
        'extract_firmware', 'grafana',
        'identify_flutter_build', 'inspect_apple_ips',
        'inspect_binary_layout', 'inspect_byte_entropy',
        'inspect_dart_aot', 'inspect_devicetree',
        'inspect_elf_notes', 'inspect_erlang_beam',
        'inspect_evm_interface', 'inspect_firmware_regions',
        'inspect_go_binary', 'inspect_gpu_code_objects',
        'inspect_il2cpp_metadata', 'inspect_jeb_client',
        'inspect_kernel_module', 'inspect_managed_artifact',
        'inspect_managed_members', 'inspect_managed_native_boundaries',
        'inspect_model_artifact', 'inspect_native_exports',
        'inspect_packet_capture', 'inspect_packet_fields',
        'inspect_pcap_capture', 'inspect_pcap_client',
        'inspect_pe_certificates', 'inspect_pe_imports',
        'inspect_pe_load_config', 'inspect_pe_resources',
        'inspect_python_pyc', 'inspect_recorded_crash',
        'inspect_rust_binary', 'inspect_signal_decoders',
        'inspect_sqlite_database', 'inspect_wasm_artifact',
        'inspect_with_objdump', 'list_jeb_units',
        'notify', 'open',
        'open_jeb_project', 'verify_managed_native_boundaries',
    ),
    're.mobile': (
        'capture_adb_screen', 'collect_adb_bugreport',
        'decode_android_resources', 'inspect_adb_client',
        'inspect_adb_device', 'inspect_adb_display',
        'inspect_adb_package', 'inspect_adb_window',
        'inspect_android_class', 'inspect_android_jni_bindings',
        'inspect_android_method', 'inspect_android_package',
        'inspect_apk_signature', 'inspect_apktool_client',
        'install_adb_package', 'list_adb_devices',
        'list_adb_directory', 'list_adb_features',
        'list_adb_packages', 'list_adb_processes',
        'list_adb_services', 'pull_adb_file',
        'pull_adb_package', 'push_adb_file',
        'read_adb_logcat', 'read_adb_setting',
        'resolve_adb_packages', 'search_android_classes',
        'start_adb_activity', 'start_adb_app',
        'stop_adb_app', 'trace_android_references',
        'uninstall_adb_package',
    ),
    're.web': (
        'analyze_javascript_application', 'analyze_web_bundle',
        'capture_browser_scenario', 'capture_electron_scenario',
        'capture_web_screenshot', 'compare_application_versions',
        'compare_javascript_export_shapes', 'compare_source_to_bundle',
        'compare_web_captures', 'compare_web_screenshots',
        'discover_webmcp_tools', 'export_web_scripts',
        'inspect_analysis_view', 'inspect_desktop_application',
        'inspect_electron_fuses', 'inspect_electron_page',
        'inspect_web_event_listeners', 'inspect_web_network_capture',
        'inspect_web_page', 'list_browser_targets',
        'list_electron_targets', 'list_javascript_runtime_targets',
        'observe_javascript_runtime', 'observe_web_execution',
        'observe_web_session', 'reconcile_javascript_runtime',
        'recover_javascript_sources', 'trace_application_feature',
        'trace_javascript_semantics', 'trace_web_module_imports',
        'trace_web_source_location',
    ),
    're.dynamic': (
        'close_frida_session', 'close_gdb_session',
        'close_rizin_debug_session', 'cutter_command',
        'execute_rizin_command', 'frida_session_status',
        'gdb_console', 'gdb_session_status',
        'instrument_with_frida', 'list_cutter_sessions',
        'list_frida_devices', 'list_frida_processes',
        'load_frida_script', 'resume_frida_session',
        'rizin_debug_command', 'rizin_debug_session_status',
        'start_frida_session', 'start_gdb_session',
        'start_rizin_debug_session', 'unload_frida_script',
    ),
    're.appgraph': (
        'build_reconstruction_obligation_ledger', 'evaluate_reconstruction_coverage',
        'import_managed_reconstruction', 'project_android_application_graph',
        'project_apple_application_graph', 'project_harmony_application_graph',
        'project_managed_application_graph',
    ),
    're.guidance': (
        'guided_workflows', 'read_guidance_section',
        'search_guidance',
    ),
}

#: 原子名 → 所属域。旧调用（直接传 rea.pull_adb_package）靠它一步归位。
# 老调用可能还用 rea.<动作> 写法，保留双前缀兼容（对外能力名已统一为 re.<域>）
DOMAIN_OF = {p + act: dom
             for dom, acts in DOMAINS.items() for act in acts for p in ("re.", "rea.")}

REA_CAPABILITIES = [
    Capability(
        're.native',
        '本地二进制与文件格式静态解析：PE/ELF/Mach-O/WASM/pyc/Go/Rust、固件、PCAP、SQLite、.NET 托管程序集、模型/设备树/信号记录。',
        params=(Param("action", "str", True,
                '子动作，如 compare_managed_members、decode_signal_recording、decompile_jeb_item'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=False,
        check=_mk_any(['dotnet_ilspy', 'none', 'jeb', 'binwalk', 'flutter', 'ghidra_or_rizin', 'erlang_beam', 'evm_tools', 'go_dump', 'gpu_tools', 'il2cpp_dumper', 'llm', 'tshark', 'python_uncompyle', 'rust_decompiler', 'sqlite3', 'wasm_tools', 'ghidra_rizin', 'adb_apktool']),
        note="[REA] 46 个子动作；完整清单见 vendored_rea.DOMAINS['re.native']",
    ),
    Capability(
        're.mobile',
        '移动端逆向：adb 设备/包/文件/日志/截屏、APK 静态分析、签名与资源解码、HarmonyOS 应用。',
        params=(Param("action", "str", True,
                '子动作，如 capture_adb_screen、collect_adb_bugreport、decode_android_resources'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=True,
        check=_mk_any(['adb', 'apktool', 'adb_apktool', 'apktool_keytool']),
        note="[REA] 33 个子动作；完整清单见 vendored_rea.DOMAINS['re.mobile']",
    ),
    Capability(
        're.web',
        'Web / JavaScript 前端逆向：浏览器目标、CDP 观察、web bundle 抓取、源码映射还原、Electron 页面与 fuses。',
        params=(Param("action", "str", True,
                '子动作，如 analyze_javascript_application、analyze_web_bundle、capture_browser_scenario'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=True,
        check=_mk_any(['node', 'chromium', 'ghidra_rizin', 'none', 'node_chromium', 'node_mitmproxy']),
        note="[REA] 31 个子动作；完整清单见 vendored_rea.DOMAINS['re.web']",
    ),
    Capability(
        're.dynamic',
        '动态调试与插桩：Frida 会话、GDB MI、Rizin/Cutter 调试会话（会附加到进程）。',
        params=(Param("action", "str", True,
                '子动作，如 close_frida_session、close_gdb_session、close_rizin_debug_session'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=True,
        check=_mk_any(['frida', 'gdb_pwndbg', 'rizin', 'rizin_cutter', 'ghidra_rizin']),
        note="[REA] 20 个子动作；完整清单见 vendored_rea.DOMAINS['re.dynamic']",
    ),
    Capability(
        're.appgraph',
        '应用图投影与重建：Android/Apple/Harmony/托管程序集的应用图、重建义务账本与覆盖率评估。',
        params=(Param("action", "str", True,
                '子动作，如 build_reconstruction_obligation_ledger、evaluate_reconstruction_coverage、import_managed_reconstruction'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=False,
        check=_mk_any(['ghidra_rizin', 'dotnet_ilspy']),
        note="[REA] 7 个子动作；完整清单见 vendored_rea.DOMAINS['re.appgraph']",
    ),
    Capability(
        're.guidance',
        '逆向指引与工作流：检索内置 REA 工作流文档、按小节精确读取。',
        params=(Param("action", "str", True,
                '子动作，如 guided_workflows、read_guidance_section、search_guidance'),
                Param("args", "json",
                      doc="该子动作的参数，原样透传给引擎")),
        side_effect=False,
        check=_mk_any(['ghidra_rizin']),
        note="[REA] 3 个子动作；完整清单见 vendored_rea.DOMAINS['re.guidance']",
    ),
]
