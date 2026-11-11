#!/usr/bin/env bash
# 停止并移除 usbdevd.service、drop-in、polkit 规则与用户级「登录拉起」单元。
# 保留 /var/lib/usbdevd 里的历史记录。
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root：sudo $0 或 pkexec $0" >&2
  exit 1
fi

# 与安装脚本一致：pkexec 用 PKEXEC_UID，sudo 用 SUDO_USER
if [ -n "${PKEXEC_UID:-}" ] && [ "${PKEXEC_UID}" != "0" ]; then
  ACTOR="$(getent passwd "$PKEXEC_UID" | cut -d: -f1)"
elif [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  ACTOR="$SUDO_USER"
else
  ACTOR="$(id -un)"
fi
ACTOR_UID="$(id -u "$ACTOR")"
ACTOR_HOME="$(getent passwd "$ACTOR" | cut -d: -f6)"

systemctl disable --now usbdevd-boot-guard.service 2>/dev/null || true
systemctl disable --now usbdevd.service 2>/dev/null || true

if [ -n "$ACTOR_HOME" ] && [ -f "$ACTOR_HOME/.config/systemd/user/usbdevd-session.service" ]; then
  runuser -u "$ACTOR" -- env XDG_RUNTIME_DIR="/run/user/$ACTOR_UID" \
    systemctl --user disable usbdevd-session.service >/dev/null 2>&1 || true
  rm -f "$ACTOR_HOME/.config/systemd/user/usbdevd-session.service"
fi

rm -f /etc/systemd/system/usbdevd.service
rm -f /etc/systemd/system/usbdevd-boot-guard.service
rm -rf /etc/systemd/system/usbdevd.service.d
rm -f /etc/polkit-1/rules.d/49-usbdevd.rules
systemctl daemon-reload

echo "已移除 usbdevd.service、usbdevd-boot-guard.service、drop-in、polkit 规则与用户单元 usbdevd-session.service。"
echo "历史记录仍在 /var/lib/usbdevd/（含 history.jsonl、last-snapshot.json），如需清空手动删除。"
echo "配置仍在 /etc/usbdevd/config.json。"
