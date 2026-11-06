#!/bin/bash
# 部署手机节点：推代码 → 自测 → 重启 → 等端口起来
#
# 用法：
#   export PHONE_SERIAL=<adb devices 里那串序列号>   # 必填，仓库里不留真实序列号
#   export FLEET_TOKEN=<访问令牌>                     # 可选，不给则用手机上的 ~/.fleet-token
#   ./phone/deploy.sh
#
# 前置：手机已装 Termux，容器（proot-distro debian）已按 docs 里的步骤建好。
set -u
S="${PHONE_SERIAL:?请先 export PHONE_SERIAL=<adb 序列号>}"
TH=/data/data/com.termux/files/home
ROOTFS=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/debian/rootfs
ENVV="PREFIX=/data/data/com.termux/files/usr HOME=$TH LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib PATH=/data/data/com.termux/files/usr/bin:/system/bin TMPDIR=/data/data/com.termux/files/usr/tmp"
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/agent.py"
START="$HERE/start.sh"

echo "[1/4] 推送 agent.py + start.sh"
adb -s "$S" shell "run-as com.termux sh -c 'mkdir -p $ROOTFS/opt/fleet-agent && cat > $ROOTFS/opt/fleet-agent/agent.py'" < "$SRC" || exit 1
adb -s "$S" shell "run-as com.termux sh -c 'cat > $ROOTFS/opt/fleet-agent/start.sh && chmod 755 $ROOTFS/opt/fleet-agent/start.sh'" < "$START" || exit 1

echo "[2/4] 容器内自测"
adb -s "$S" shell "run-as com.termux env $ENVV sh -c 'proot-distro login debian -- /usr/bin/python3 /opt/fleet-agent/agent.py --selftest 2>&1'" | tail -8
rc=${PIPESTATUS[0]}
if [ "$rc" != "0" ]; then echo "自测未通过，不启动"; exit 1; fi

echo "[3/4] 停旧进程"
# 只杀命令行里带 /opt/fleet-agent/agent.py 的那个，手机自己跑的服务（http.server / phone_worker）一律不动。
# 注意：proot 死后 python 会被 init 收养继续占端口，所以必须按 cmdline 找 PID 直接 kill -9。
OLD=$(adb -s "$S" shell "run-as com.termux sh -c 'for d in /proc/[0-9]*; do p=\${d#/proc/}; c=\$(cat \$d/cmdline 2>/dev/null | tr \"\\0\" \" \"); case \"\$c\" in *fleet-agent/agent.py*) echo \$p;; esac; done'" | tr -d '\r')
for p in $OLD; do
  echo "  kill $p"
  adb -s "$S" shell "run-as com.termux kill -9 $p" >/dev/null 2>&1
done
sleep 2
adb -s "$S" shell "ss -tln 2>/dev/null | grep -q 8770" && { echo "8770 仍被占用，停止"; exit 1; }

echo "[4/4] 启动"
adb -s "$S" shell "run-as com.termux env $ENVV sh -c 'nohup setsid proot-distro login debian -- /bin/sh /opt/fleet-agent/start.sh > $TH/agent.log 2>&1 & echo started'" | head -2
for i in 1 2 3 4 5 6; do
  sleep 1
  if adb -s "$S" shell "ss -tln 2>/dev/null | grep -q 8770 && echo up" | grep -q up; then
    echo "端口 8770 已就绪"
    adb -s "$S" shell "run-as com.termux sh -c 'tail -2 $TH/agent.log'"
    exit 0
  fi
done
echo "端口未起来，日志："
adb -s "$S" shell "run-as com.termux sh -c 'tail -15 $TH/agent.log'"
exit 1
