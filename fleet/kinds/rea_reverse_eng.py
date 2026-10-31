# -*- coding: utf-8 -*-
"""REA 逆向引擎真 handler：apktool / binwalk / tshark / frida。

命令在 PATH 里已兜底（vendored_rea.py 导入时把 D:/DevTools/reverse/bin、
jadx/bin、Wireshark 等补进 PATH）。handler 签名与 rea_device.py 一致：(dev, cap, args)。
"""
import os
import shutil
import subprocess
import tempfile

from ..core import DeviceResult as R


def _run(cmd, timeout=120, cwd=None):
    """跑命令，返回 (out, err, code)。首元素若是无扩展名包装器（.cmd/.bat），
    用 shutil.which 解析成绝对路径，避免 Windows CreateProcess 扩展名搜索失败。"""
    cmd = list(cmd)
    if cmd and not os.path.isabs(cmd[0]):
        _w = shutil.which(cmd[0])
        if _w:
            cmd[0] = _w
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, errors="replace", cwd=cwd)
    except subprocess.TimeoutExpired:
        return None, "timeout(%ss)" % timeout, 124
    except Exception as e:
        return None, "命令执行异常：%s" % e, 127
    return p.stdout, p.stderr, p.returncode


def _h_apktool(dev, cap, args):
    if not shutil.which("apktool"):
        return R.unavailable(
            cap, "apktool 未安装（需 D:/DevTools/reverse/apktool/apktool.jar + apktool.cmd）",
            device=dev.name, via=dev.via)
    # 客户端自检
    if cap == "rea.inspect_apktool_client":
        out, err, code = _run(["apktool", "--version"], timeout=30)
        ver = (out or err or "").strip().splitlines()
        return R.success(cap, dev.name, via=dev.via,
                         data={"version": ver[-1] if ver else "",
                               "raw": (out or "")[:2000]})
    apk = (args.get("path") or args.get("apk") or "").strip()
    if not apk or not os.path.isfile(apk):
        return R.error(cap, "缺参数 path/apk（APK 文件路径）", device=dev.name, via=dev.via)
    outdir = args.get("out") or tempfile.mkdtemp(prefix="rea_apk_")
    os.makedirs(outdir, exist_ok=True)
    # 解包（保留 smali，不反编译）
    out, err, code = _run(["apktool", "d", "-s", "-f", apk, "-o", outdir], timeout=300)
    if code != 0:
        return R.error(cap, "apktool decode 失败：%s" % (err or out)[:300],
                       device=dev.name, via=dev.via)
    data = {"apk": apk, "decoded_dir": outdir}
    # jadx 在场就顺带反编译出 java 源码（提升质量）
    if shutil.which("jadx"):
        jout = outdir + "_jadx"
        jo, je, jc = _run(["jadx", "-q", "-d", jout, apk], timeout=400)
        if jc == 0:
            data["jadx_dir"] = jout
    # 读 manifest（inspect_android_package 需要）
    if cap in ("rea.decode_android_resources", "rea.inspect_android_package",
               "rea.inspect_android_jni_bindings", "rea.inspect_android_class",
               "rea.inspect_android_method", "rea.search_android_classes",
               "rea.trace_android_references", "rea.notify"):
        manifest = os.path.join(outdir, "AndroidManifest.xml")
        if os.path.isfile(manifest):
            try:
                data["manifest"] = open(manifest, encoding="utf-8",
                                        errors="replace").read()[:4000]
                data["manifest_len"] = len(data["manifest"])
            except Exception:
                pass
        if cap == "rea.inspect_apk_signature":
            co, ce, cc = _run(["keytool", "-printcert", "-jarfile", apk], timeout=60)
            if cc == 0:
                data["cert"] = (co or "")[:4000]
            else:
                data["cert_error"] = (ce or co)[:300]
        return R.success(cap, dev.name, via=dev.via, data=data)
    # 其余 apktool 系：已解包，返回目录供上层使用
    return R.success(cap, dev.name, via=dev.via, data=data)


def _h_binwalk(dev, cap, args):
    if not shutil.which("binwalk"):
        return R.unavailable(cap, "binwalk 未安装", device=dev.name, via=dev.via)
    path = (args.get("path") or args.get("file") or "").strip()
    if not path or not os.path.isfile(path):
        return R.error(cap, "缺参数 path/file", device=dev.name, via=dev.via)
    if cap == "rea.inspect_firmware_regions":
        out, err, code = _run(["binwalk", path], timeout=120)
        return R.success(cap, dev.name, via=dev.via,
                         data={"path": path, "scan": (out or err or "")[:8000]})
    if cap == "rea.extract_firmware":
        outdir = args.get("out") or (path + "_extracted")
        os.makedirs(outdir, exist_ok=True)
        out, err, code = _run(["binwalk", "-e", path, "-C", outdir], timeout=300)
        extracted = []
        for root, _, files in os.walk(outdir):
            for f in files:
                extracted.append(os.path.relpath(os.path.join(root, f), outdir))
        return R.success(cap, dev.name, via=dev.via, data={
            "path": path, "out_dir": outdir,
            "extracted": extracted[:200], "count": len(extracted)})
    return R.unavailable(cap, "binwalk 引擎未覆盖该能力", device=dev.name, via=dev.via)


def _h_tshark(dev, cap, args):
    if not shutil.which("tshark"):
        return R.unavailable(cap, "tshark/Wireshark 未安装", device=dev.name, via=dev.via)
    path = (args.get("path") or args.get("pcap") or "").strip()
    if not path or not os.path.isfile(path):
        return R.error(cap, "缺参数 path/pcap（.pcap/.pcapng 文件路径）", device=dev.name, via=dev.via)
    if cap == "rea.inspect_pcap_capture":
        out, err, code = _run(["tshark", "-r", path, "-V"], timeout=120)
        return R.success(cap, dev.name, via=dev.via,
                         data={"path": path, "detail": (out or err or "")[:12000]})
    if cap == "rea.inspect_pcap_client":
        out, err, code = _run(
            ["tshark", "-r", path, "-T", "fields", "-e", "ip.src", "-e", "ip.dst",
             "-e", "tcp.srcport", "-e", "tcp.dstport"], timeout=120)
        flows = {}
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                flows[tuple(parts[:4])] = flows.get(tuple(parts[:4]), 0) + 1
        top = sorted(flows.items(), key=lambda kv: -kv[1])[:20]
        return R.success(cap, dev.name, via=dev.via, data={
            "path": path,
            "top_flows": [{"src": a, "dst": b, "sport": c, "dport": d, "count": n}
                          for (a, b, c, d), n in top]})
    return R.unavailable(cap, "tshark 引擎未覆盖该能力", device=dev.name, via=dev.via)


def _h_frida(dev, cap, args):
    if not shutil.which("frida"):
        return R.unavailable(cap, "frida-tools 未安装", device=dev.name, via=dev.via)
    if cap == "rea.list_frida_devices":
        out, err, code = _run(["frida-ls-devices"], timeout=30)
        return R.success(cap, dev.name, via=dev.via,
                         data={"devices": (out or err or "").strip().splitlines()[:20]})
    if cap == "rea.list_frida_processes":
        # -U = USB（手机）；若手机端 frida-server 未起会失败
        out, err, code = _run(["frida-ps", "-U"], timeout=30)
        if code != 0:
            return R.unavailable(
                cap, "枚举进程需手机端 frida-server 运行；PC 端 frida-tools 已装。"
                "失败原因：%s" % (err or out)[:200],
                device=dev.name, via=dev.via)
        return R.success(cap, dev.name, via=dev.via,
                         data={"processes": (out or "").strip().splitlines()[:50]})
    # session/script/instrument：需手机端 frida-server 运行
    return R.unavailable(
        cap, "该 frida 能力需手机端 frida-server 运行（PC 端 frida-tools 已装；"
        "server 将按手机架构 push 并启动）。当前仅设备/进程枚举可用。",
        device=dev.name, via=dev.via)
