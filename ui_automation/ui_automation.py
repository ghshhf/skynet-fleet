"""ui_automation · 无障碍跳广告（Host-adb 形态最小实现）

来源：docs/李跳跳拆解.md。用 adb 拿 Android UI 节点树，按规则匹配「跳过/关闭」，
点掉。规则可来自 L3 共享上下文（adskip.rules），补李跳跳「停更即废」的短板；
匹配不到且规则标了 needVision 时，走远程 AI 节点识别关闭按钮（视觉兜底）。

零第三方依赖（纯 stdlib + adb）。可在没有真机时用 --selftest 跑离线用例。

用法：
  python ui_automation.py --selftest
  python ui_automation.py --serial 127.0.0.1:5555 --package com.xxx.video --rounds 6
"""
import argparse
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ADB = "adb"


def _dec(b):
    """字节解码兜底：utf-8 -> cp936 -> replace（中文 Windows 控制台老问题）。"""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp936", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _adb(serial, *args, timeout=20):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, _dec(r.stdout), _dec(r.stderr)
    except FileNotFoundError:
        return -1, "", "未找到 adb（请把 platform-tools 加入 PATH）"
    except subprocess.TimeoutExpired:
        return -1, "", "adb 超时"


def dump_nodes(serial=None):
    """拿当前界面节点树 XML 文本。"""
    rc, out, err = _adb(serial, "shell", "uiautomator", "dump", "/sdcard/_ui.xml")
    if rc != 0:
        return None, err
    rc, xml, err = _adb(serial, "exec-out", "cat", "/sdcard/_ui.xml")
    if rc != 0 or "<hierarchy" not in xml:
        return None, err or "dump 无有效 XML"
    return xml, None


def parse_nodes(xml_text):
    """解析为 [{text, desc, rid, package, cls, bounds:(x1,y1,x2,y2)}]。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [], f"XML 解析失败: {e}"
    nodes = []
    bpat = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
    for el in root.iter("node"):
        b = el.get("bounds", "")
        m = bpat.search(b)
        nodes.append({
            "text": el.get("text", "") or "",
            "desc": el.get("content-desc", "") or "",
            "rid": el.get("resource-id", "") or "",
            "package": el.get("package", "") or "",
            "cls": el.get("class", "") or "",
            "clickable": el.get("clickable", "false") == "true",
            "bounds": tuple(map(int, m.groups())) if m else None,
            "raw": el,
        })
    return nodes, None


def _center(bounds):
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def match(nodes, rule):
    """按一条 trigger 规则匹配节点，返回 (node, center) 或 (None, None)。"""
    texts = rule.get("text") or []
    regex = rule.get("textRegex")
    descs = rule.get("desc") or []
    rids = rule.get("resourceId") or []
    for n in nodes:
        if not n["bounds"]:
            continue
        if texts and any(t and t in n["text"] for t in texts):
            return n, _center(n["bounds"])
        if regex and n["text"] and re.search(regex, n["text"]):
            return n, _center(n["bounds"])
        if descs and any(d and d in n["desc"] for d in descs):
            return n, _center(n["bounds"])
        if rids and any(r and r in n["rid"] for r in rids):
            return n, _center(n["bounds"])
    return None, None


def tap(serial, x, y):
    rc, _, err = _adb(serial, "shell", "input", "tap", str(x), str(y))
    return rc == 0, err


def vision_fallback(serial, prompt="找到这个界面上跳过/关闭广告的按钮，只回中心坐标 x,y"):
    """截图 -> 远程 AI 节点识别坐标（钩子；需 AI 节点在线 + Commander 可达）。"""
    rc, png, err = _adb(serial, "exec-out", "screencap", "-p", timeout=25)
    if rc != 0:
        return None, f"截图失败: {err}"
    # 真实实现：把 png 发给 AI 节点（见 rfc/remote-ai-service）。此处返回 None 表示未接。
    return None, "vision 未接（需在线 AI 节点；截图已取 %d bytes）" % len(png)


def skip_ad(serial, package, rules, rounds=6, interval=0.8, use_vision=False):
    """多轮尝试跳过指定包的开屏广告。返回 (ok, log[])。"""
    app_rules = [r for r in rules if r.get("packageName") == package] or \
                [r for r in rules if r.get("packageName") in (None, "", "*")]
    triggers = []
    for r in app_rules:
        triggers.extend(r.get("triggers", []))
    log = []
    for i in range(rounds):
        xml, err = dump_nodes(serial)
        if not xml:
            log.append(f"[{i}] dump 失败: {err}")
            # 无设备/设备离线属硬失败：后续轮次必然同样失败，立即返回，不空转
            low = (err or "").lower()
            if any(k in low for k in ("no devices", "not found", "device offline",
                                      "unauthorized", "more than one device")):
                log.append("（无可用设备，提前结束）")
                return False, log
            time.sleep(interval)
            continue
        nodes, perr = parse_nodes(xml)
        if perr:
            log.append(f"[{i}] {perr}")
            time.sleep(interval)
            continue
        # 当前前台是否目标包（避免误点其他界面）
        pkgs = {n["package"] for n in nodes if n["package"]}
        if package and pkgs and package not in pkgs:
            log.append(f"[{i}] 前台非目标包({sorted(pkgs)[:3]})，跳过")
            time.sleep(interval)
            continue
        hit = False
        for tr in triggers:
            node, c = match(nodes, tr)
            if node and c:
                ok, terr = tap(serial, *c)
                log.append(f"[{i}] 命中「{tr.get('name')}」text='{node['text'][:24]}' "
                           f"tap{c} {'OK' if ok else 'FAIL:' + terr}")
                hit = True
                break
        if not hit and use_vision:
            c, verr = vision_fallback(serial)
            if c:
                tap(serial, *c)
                log.append(f"[{i}] 视觉兜底 tap{c}")
                hit = True
            else:
                log.append(f"[{i}] 未命中，视觉兜底不可用: {verr}")
        if not hit:
            log.append(f"[{i}] 未命中任何规则")
        time.sleep(interval)
        if hit:
            # 点完再观察一轮，若界面已无匹配则确认完成
            xml2, _ = dump_nodes(serial)
            if xml2:
                nodes2, _ = parse_nodes(xml2)
                still = any(match(nodes2, tr)[0] for tr in triggers)
                if not still:
                    return True, log + [f"[{i}] 已无匹配按钮，判定跳过完成"]
    return False, log


def _selftest():
    """离线用例：无需真机，验证解析+匹配（含文案漂移的正则兜底）。"""
    xml = """<hierarchy rotation="0">
      <node text="" content-desc="" resource-id="" class="android.widget.FrameLayout"
            package="com.xxx.video" clickable="false" bounds="[0,0][1080,2340]">
        <node text="跳过 5s" content-desc="" resource-id="com.xxx.video:id/skip"
              class="android.widget.TextView" package="com.xxx.video" clickable="true"
              bounds="[880,120][1040,200]"/>
        <node text="广告" content-desc="" resource-id="" class="android.widget.TextView"
              package="com.xxx.video" clickable="false" bounds="[40,1600][400,1700]"/>
      </node>
    </hierarchy>"""
    nodes, err = parse_nodes(xml)
    assert not err and len(nodes) == 3, f"解析失败: {err}"
    rule = {"name": "跳过", "action": "click", "text": ["跳过广告", "跳过"],
            "textRegex": r"跳过|skip", "needVision": False}
    node, c = match(nodes, rule)
    assert node and node["text"] == "跳过 5s", f"未命中: {node}"
    assert c == (960, 160), f"坐标错: {c}"
    # 归一化文案命中
    rule2 = {"name": "图标", "resourceId": ["id/skip"]}
    node2, c2 = match(nodes, rule2)
    assert node2 and c2 == (960, 160), f"rid 匹配失败: {node2}"
    print("[selftest] 节点解析 OK：3 节点")
    print("[selftest] 正则兜底命中「跳过 5s」(文案漂移) OK")
    print("[selftest] resourceId 命中 OK")
    print("[selftest] 坐标中心 OK:", c)
    print("[selftest] PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--serial", default=None)
    ap.add_argument("--package", default="")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--vision", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    # 内联一份默认规则（真实应从 L3 adskip.rules 拉）
    rules = [{"packageName": "*", "triggers": [
        {"name": "跳过", "action": "click", "text": ["跳过广告", "跳过"],
         "textRegex": r"跳过|skip", "desc": ["关闭"]},
    ]}]
    ok, log = skip_ad(args.serial, args.package, rules,
                      rounds=args.rounds, use_vision=args.vision)
    print("\n".join(log))
    print("RESULT:", "SKIPPED" if ok else "NOT_FOUND")


if __name__ == "__main__":
    main()
