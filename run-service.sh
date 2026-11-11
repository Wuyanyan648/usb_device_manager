#!/usr/bin/env bash
# 常驻运行（无浏览器），崩溃后 2 秒重启。给手动调试用；
# 正式常驻走 install-autostart.sh 装的系统级单元 systemd/usbdevd.service。
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORT="${PORT:-8795}"
while true; do
  python3 "$DIR/backend/usbdev_server.py" --no-browser
  code=$?
  if [ "$code" -eq 3 ]; then
    echo "[run-service] 端口 $PORT 已被占用，视为已有实例，退出。" >&2
    exit 0
  fi
  echo "[run-service] usbdev_server 退出（code=$code），2 秒后重启…" >&2
  sleep 2
done
