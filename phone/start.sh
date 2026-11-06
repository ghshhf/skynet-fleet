#!/bin/sh
# 容器内启动脚本（跑在手机本地的 Debian rootfs 里，不依赖任何外部主机）。
#
# 令牌不从仓库里带：优先环境变量 FLEET_TOKEN，其次手机上的 $HOME/.fleet-token。
# 两个都没有就以「无鉴权模式」启动——只监听回环，避免裸奔在局域网。
if [ -z "${FLEET_TOKEN:-}" ] && [ -f "$HOME/.fleet-token" ]; then
  FLEET_TOKEN=$(cat "$HOME/.fleet-token")
  export FLEET_TOKEN
fi

export FLEET_NODE_ID="${FLEET_NODE_ID:-phone}"
export FLEET_PORT="${FLEET_PORT:-8770}"
export FLEET_SANDBOX="${FLEET_SANDBOX:-/root/work}"

if [ -z "${FLEET_TOKEN:-}" ]; then
  export FLEET_BIND=127.0.0.1
  echo "[start] 无令牌，只监听回环" >&2
fi

mkdir -p "$FLEET_SANDBOX"
exec /usr/bin/python3 /opt/fleet-agent/agent.py
