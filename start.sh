#!/usr/bin/env bash
# 前台启动（Ctrl-C 结束）。非 root 只能查看，install-autostart.sh 装成系统级单元后有 root 权限。
# 换端口: PORT=8796 ./start.sh      只看树: ./start.sh --tree
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8795}"
if [ "$(id -u)" -ne 0 ]; then
  echo "[start] 非 root 运行：页面可看，链路开关会提示没有权限。" >&2
  echo "[start] 需要可写：sudo ./install-autostart.sh（系统级单元）" >&2
fi
exec python3 "$DIR/backend/usbdev_server.py" "$@"
