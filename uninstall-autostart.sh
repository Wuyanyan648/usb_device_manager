#!/usr/bin/env bash
# 停止并移除 usbdevd.service 与 polkit 规则。保留 /var/lib/usbdevd 里的历史记录。
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root：sudo $0" >&2
  exit 1
fi

systemctl disable --now usbdevd.service 2>/dev/null || true
rm -f /etc/systemd/system/usbdevd.service
rm -f /etc/polkit-1/rules.d/49-usbdevd.rules
systemctl daemon-reload

echo "已移除 usbdevd.service 与 polkit 规则。"
echo "历史记录仍在 /var/lib/usbdevd/（含 history.jsonl、last-snapshot.json），如需清空手动删除。"
