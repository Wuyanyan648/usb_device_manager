#!/usr/bin/env bash
# 安装并启用系统级单元 usbdevd.service（127.0.0.1:8795，开机自启）。
#
# 为什么要 root：开关 USB 通信链路要写 /sys/bus/usb/devices/*/authorized，
# 该文件只有 root 可写。单元只监听 127.0.0.1，除 authorized 外不写任何 sysfs。
# 同时安装 polkit 规则，让门户能免密 start/stop 这一个单元（与 usbnicd 一致）。
#
# 可直接 `sudo ./install-autostart.sh`，也可以 `pkexec ./install-autostart.sh`
# （桌面弹窗输密码；此时身份来自 PKEXEC_UID）。
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root：sudo $0 或 pkexec $0" >&2
  exit 1
fi

# 识别「真正的目标用户」：pkexec 用 PKEXEC_UID，sudo 用 SUDO_USER
if [ -n "${PKEXEC_UID:-}" ] && [ "${PKEXEC_UID}" != "0" ]; then
  ACTOR="$(getent passwd "$PKEXEC_UID" | cut -d: -f1)"
elif [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  ACTOR="$SUDO_USER"
else
  ACTOR="$(id -un)"
fi
ACTOR_UID="$(id -u "$ACTOR")"
ACTOR_HOME="$(getent passwd "$ACTOR" | cut -d: -f6)"

chmod +x "$DIR/start.sh" "$DIR/run-service.sh" 2>/dev/null || true

# 先停掉可能在跑的前台实例，避免端口冲突
pkill -f "[u]sbdev_server.py" 2>/dev/null || true
systemctl stop usbdevd.service 2>/dev/null || true
sleep 0.3

install -d -m 0755 /etc/systemd/system
sed "s|@DIR@|$DIR|g" "$DIR/systemd/usbdevd.service" > /etc/systemd/system/usbdevd.service
chmod 644 /etc/systemd/system/usbdevd.service

# drop-in：注销（user@UID 停止）时跟着停，与 usbnicd 的策略一致
install -d -m 0755 /etc/systemd/system/usbdevd.service.d
sed "s|%UID%|$ACTOR_UID|g" "$DIR/systemd/usbdevd.service.d/10-user-session.conf" \
  > /etc/systemd/system/usbdevd.service.d/10-user-session.conf
chmod 644 /etc/systemd/system/usbdevd.service.d/10-user-session.conf

install -d -m 0755 /etc/polkit-1/rules.d
sed "s|@USER@|$ACTOR|g" "$DIR/systemd/49-usbdevd.rules" \
  > /etc/polkit-1/rules.d/49-usbdevd.rules
chmod 644 /etc/polkit-1/rules.d/49-usbdevd.rules

# 状态目录（操作历史 + 启动快照）与配置目录
install -d -m 0750 /var/lib/usbdevd
install -d -m 0755 /etc/usbdevd
if [ ! -f /etc/usbdevd/config.json ]; then
  cat > /etc/usbdevd/config.json <<'JSON'
{
  "auto_replug_on_boot": true,
  "boot_grace_seconds": 30,
  "boot_replug_attempts": 2,
  "boot_settle_seconds": 8,
  "allow_port_replug": false
}
JSON
  chmod 644 /etc/usbdevd/config.json
fi

# 开机自动检查扩展坞（oneshot）：坞没枚举起就对 usb3-port1 做一次 disable 1→0
sed "s|@DIR@|$DIR|g" "$DIR/systemd/usbdevd-boot-guard.service" \
  > /etc/systemd/system/usbdevd-boot-guard.service
chmod 644 /etc/systemd/system/usbdevd-boot-guard.service

systemctl daemon-reload
systemctl enable usbdevd.service >/dev/null
systemctl enable usbdevd-boot-guard.service >/dev/null

# 用户级「登录后拉起」桥：系统级 unit 注销即停，靠它在下次登录恢复
if [ -n "$ACTOR_HOME" ] && [ -d "$ACTOR_HOME" ]; then
  install -d -m 0755 "$ACTOR_HOME/.config/systemd/user"
  sed "s|@DIR@|$DIR|g" "$DIR/systemd/usbdevd-session.service" \
    > "$ACTOR_HOME/.config/systemd/user/usbdevd-session.service"
  chmod 644 "$ACTOR_HOME/.config/systemd/user/usbdevd-session.service"
  chown -R "$ACTOR_UID:$(id -gn "$ACTOR")" "$ACTOR_HOME/.config/systemd/user/usbdevd-session.service"
  runuser -u "$ACTOR" -- env XDG_RUNTIME_DIR="/run/user/$ACTOR_UID" \
    systemctl --user enable usbdevd-session.service >/dev/null 2>&1 \
    || echo "[warn] 无法为用户 $ACTOR 启用 usbdevd-session.service，登录后手动执行：" \
            "systemctl --user enable usbdevd-session.service" >&2
fi

systemctl restart usbdevd.service
sleep 1

echo "已安装系统单元: /etc/systemd/system/usbdevd.service"
echo "开机自动重连:  /etc/systemd/system/usbdevd-boot-guard.service（已 enable，默认开启）"
echo "注销即停:      /etc/systemd/system/usbdevd.service.d/10-user-session.conf（PartOf=user@$ACTOR_UID.service）"
echo "登录拉起:      $ACTOR_HOME/.config/systemd/user/usbdevd-session.service"
echo "polkit 规则:    /etc/polkit-1/rules.d/49-usbdevd.rules（放行用户 $ACTOR）"
echo "状态目录:      /var/lib/usbdevd（0750）"
echo "配置:          /etc/usbdevd/config.json"
echo "界面:           http://127.0.0.1:8795/"
echo "状态:           systemctl status usbdevd"
echo "日志:           journalctl -u usbdevd -n 50 --no-pager"
echo "操作记录:       /var/lib/usbdevd/history.jsonl"
echo "卸载:           sudo $DIR/uninstall-autostart.sh"
echo
echo "端口重连（扩展坞）默认关闭：要启用把 /etc/usbdevd/config.json 的"
echo "allow_port_replug 改成 true，然后 systemctl restart usbdevd。"
echo "开机自动检查默认开启（auto_replug_on_boot=true）；要关掉改成 false 或"
echo "systemctl disable usbdevd-boot-guard.service。"
