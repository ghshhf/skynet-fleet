"""把「人格」铺到手机上：运行时 + 记忆包 + 权重清单，只传增量。

为什么需要这个工具而不是一条 adb push：
  ① Termux 的 HOME 只有 com.termux 自己可写，adb push 直接推不进去，
     必须 `push /data/local/tmp → run-as com.termux cp` 两跳；
  ② 记忆每天在涨，全量推会越来越慢。所以先拉对端 manifest 做 diff，
     只推缺的分片（内容寻址保证「同一片」在两端是同一个 id）；
  ③ 推完必须能验证：digest 对不上就是没说清，不能报成功。

命令：
    python tools/provision_phone.py status
    python tools/provision_phone.py push-lib
    python tools/provision_phone.py sync  dafeiyu --from personas/dafeiyu
    python tools/provision_phone.py diff  dafeiyu --from personas/dafeiyu
    python tools/provision_phone.py pull-manifest dafeiyu
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import adbx                                     # noqa: E402
from fleet import memory_pack as M              # noqa: E402

PHONE_LIB = "/data/user/0/com.termux/files/home/skynet/lib"
PHONE_MEM = "/data/user/0/com.termux/files/home/skynet/memory"
PHONE_MODELS = "/data/user/0/com.termux/files/home/models"
STAGE = "/data/local/tmp/skynet_stage"

LIB_FILES = [os.path.join(ROOT, "fleet", "memory_pack.py"),
             os.path.join(ROOT, "scripts", "phone", "persona.py")]


def _termux_json(script: str, timeout=120):
    """在手机上跑一段 sh，把它打印的 JSON 读回来。"""
    rc, out, err = adbx.termux(script, timeout=timeout)
    out = (out or "").strip()
    if not out:
        return None, f"rc={rc} err={(err or '').strip()[:200]}"
    try:
        return json.loads(out.splitlines()[-1]), ""
    except ValueError:
        return None, f"回执不是 JSON：{out[:200]}"


def _stage_push(local_paths):
    """先把一批本地文件推到 staging（shell 可写），返回 [(staged, name)]。"""
    adbx.sh(f"rm -rf {STAGE} && mkdir -p {STAGE}")
    out = []
    for p in local_paths:
        if not os.path.exists(p):
            raise SystemExit(f"文件不存在：{p}")
        name = os.path.basename(p)
        rc, o, e = adbx.push(p, f"{STAGE}/{name}")
        if rc != 0:
            raise SystemExit(f"push 失败 {name}: {e.strip()[:200]}")
        out.append((f"{STAGE}/{name}", name))
    return out


def _termux_copy(pairs, dest_dir):
    """把 staging 里的文件以 termux 身份搬进 dest_dir。"""
    lines = [f"mkdir -p {dest_dir}"]
    for staged, name in pairs:
        lines.append(f"cp -f {staged} {dest_dir}/{name}")
    lines.append(f"ls -l {dest_dir}")
    return adbx.termux("\n".join(lines))


# ---------------------------------------------------------------- 命令

def cmd_status(args):
    script = r"""
echo "-- mem --"; grep -E 'MemTotal|MemAvailable' /proc/meminfo
echo "-- models --"; ls -l "$HOME/models" 2>/dev/null || echo "(无 models 目录)"
echo "-- lib --"; ls -l "$HOME/skynet/lib" 2>/dev/null || echo "(未装运行时)"
echo "-- packs --"; ls -1 "$HOME/skynet/memory" 2>/dev/null || echo "(无记忆包)"
echo "-- runtimes --"; for b in llama-cli llama-server llama-bench ffmpeg; do p=$(command -v $b 2>/dev/null); echo "$b=${p:-MISSING}"; done
"""
    rc, out, err = adbx.termux(script)
    sys.stdout.write(out)
    if err.strip():
        sys.stderr.write(err)
    return rc


def cmd_push_lib(args):
    pairs = _stage_push(LIB_FILES)
    rc, out, err = _termux_copy(pairs, PHONE_LIB)
    sys.stdout.write(out)
    # 装完立刻自检：能 import、能报版本，才算真装好
    rc2, o2, e2 = adbx.termux(
        f"cd {PHONE_LIB} && python3 -c "
        f"\"import memory_pack as M; print('memory_pack OK', M.KINDS)\"")
    sys.stdout.write(o2)
    if rc2 != 0:
        sys.stderr.write(e2)
        return rc2
    return rc


EMPTY_MANIFEST = '{"pack":"","shards":[]}'


def _remote_manifest_script(name):
    """读手机侧 pack.json 的 id 清单。

    这段是「PC 拼 sh 给手机跑」，所以 f-string 里的花括号一律要成对转义；
    踩过坑：写成单花括号会被当成表达式，报的是
    「unexpected character after line continuation」，跟 sh 一点关系没有。
    """
    return (f"if [ -f {PHONE_MEM}/{name}/pack.json ]; then "
            f"python3 -c \"import json;d=json.load(open('{PHONE_MEM}/{name}/pack.json'));"
            f"print(json.dumps({{'pack':d.get('pack'),"
            f"'shards':[{{'id':s['id']}} for s in d.get('shards',[])]}}))\"; "
            f"else echo '{EMPTY_MANIFEST}'; fi")


def cmd_pull_manifest(args):
    data, why = _termux_json(_remote_manifest_script(args.name))
    if data is None:
        print(f"[X] 读不到手机侧清单：{why}")
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


def _local_pack(src):
    if not os.path.isdir(src):
        raise SystemExit(f"本地记忆包目录不存在：{src}")
    pack = M.Pack.load(src)
    if not len(pack):
        raise SystemExit(f"本地记忆包是空的：{src}（先建好 pack.json 或 shards/）")
    return pack


def _remote_manifest(name):
    data, _ = _termux_json(_remote_manifest_script(name))
    return data or {"pack": "", "shards": []}


def cmd_diff(args):
    pack = _local_pack(args.src)
    remote = _remote_manifest(args.name)
    d = pack.diff(remote)
    print(json.dumps({"local": pack.digest(), "local_count": len(pack),
                      "remote": remote.get("pack"), "need": len(d["need"]),
                      "local_extra": len(d["local_extra"]),
                      "in_sync": d["in_sync"]}, ensure_ascii=False, indent=1))
    return 0 if d["in_sync"] else 3


def cmd_sync(args):
    pack = _local_pack(args.src)
    remote = _remote_manifest(args.name)
    d = pack.diff(remote)
    print(f"[i] 本地 {len(pack)} 片 {pack.digest()}；手机 {remote.get('pack') or '(空)'}")
    if d["in_sync"]:
        print("[=] 已同步，无需传输")
        return 0
    if not d["need"]:
        print(f"[!] 手机比本地多 {len(d['local_extra'])} 片（手机上自己长出来的），"
              f"本次不回收；要合并请用 pull-manifest 后手工 merge")
        return 0

    # 只传缺的分片 + 一份完整 pack.json（对端靠它建索引）
    tmp = tempfile.mkdtemp(prefix="mem_sync_")
    try:
        staged_root = os.path.join(tmp, args.name)
        os.makedirs(os.path.join(staged_root, "shards"), exist_ok=True)
        for sid in d["need"]:
            s = pack.shards[sid]
            with open(os.path.join(staged_root, "shards", sid + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        with open(os.path.join(staged_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"pack": pack.digest(), "name": pack.name,
                       "sent": d["need"]}, f, ensure_ascii=False)

        pairs = _stage_push([os.path.join(staged_root, f) for f in
                             ["manifest.json"]]
                            + [os.path.join(staged_root, "shards", x + ".json")
                               for x in d["need"]])
        lines = [f"mkdir -p {PHONE_MEM}/{args.name}/shards"]
        for staged, name in pairs:
            dest = (f"{PHONE_MEM}/{args.name}/manifest.json" if name == "manifest.json"
                    else f"{PHONE_MEM}/{args.name}/shards/{name}")
            lines.append(f"cp -f {staged} {dest}")
        # 手机侧用同一份 memory_pack 合并后重算 digest：不接受「传完就算成功」
        lines.append(
            f"cd {PHONE_LIB} && python3 -c \""
            f"import memory_pack as M;"
            f"p=M.Pack.load('{PHONE_MEM}/{args.name}');"
            f"p.save('{PHONE_MEM}/{args.name}');"
            f"print(p.digest(), len(p))\"")
        rc, out, err = adbx.termux("\n".join(lines))
        sys.stdout.write(out)
        if rc != 0:
            sys.stderr.write(err)
            print(f"[X] 同步失败 rc={rc}")
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = _remote_manifest(args.name)
    ok = after.get("pack") == pack.digest()
    print(f"[{'=' if ok else 'X'}] 传输 {len(d['need'])} 片；手机 digest="
          f"{after.get('pack')} local={pack.digest()}")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="把人格（记忆包+运行时）铺到手机")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("push-lib").set_defaults(fn=cmd_push_lib)
    for name, fn in (("sync", cmd_sync), ("diff", cmd_diff)):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--from", dest="src", required=True)
        p.set_defaults(fn=fn)
    p = sub.add_parser("pull-manifest")
    p.add_argument("name")
    p.set_defaults(fn=cmd_pull_manifest)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
