# usb_device_manager

本机 USB 设备的可视化与「通信链路开关」服务，端口 **8795**，页面 `http://127.0.0.1:8795/`。

和 `screen`（8765/8760 门户）、`usb_wireless_network`（8787）、`theme_manager`（8790）同级，
是一个独立 git 仓库，以 submodule 挂在 `local_service` 下。

设计目标只有一件事：把「开机没枚举成功、必须拔插一次」这件事变成软件可操作。
信息尽量全（拓扑、端口、带宽、历史），可操作面第一版只做 **authorized 0/1**。

---

## 1. 适用范围

适用：

- 查看本机 USB 拓扑：hub 层级、端口号、VID:PID、厂商/产品名、序列号、速度、USB 版本、
  驱动、供电标称、电源策略、端点与带宽估算；
- 判断设备异常：`authorized=0`、未配置、无驱动、端口 over-current、内核枚举失败；
- 对单个设备做 **逻辑链路开关**（`authorized` 0→1 / 1→0），用来替代「拔插一次」；
- 记录谁在什么时候开关了什么、结果如何，方便事后判断设备是不是被软件搞掉的；
- 诊断扩展坞冷启动枚举失败（见第 8 节）。

不适用（第一版明确不做）：

- 端口断电 / 整 hub 断电 / 端口供电开关的实际写入；
- 设备级 reset（`USBDEVFS_RESET`）、驱动 unbind/bind、`bConfigurationValue` 重设；
- 对「开机时连 hub 都没被识别」的情况直接修复 —— 见第 8 节，这需要端口级重连，
  已留好受配置门控的接口位。

---

## 2. 端口与命名

现有占用：8760 门户 / 8765 screen / 8787 usbnicd / 8790 theme_manager。
本服务用 **8795**（8791、8792、8793、8795、8796、8799 均空闲，8795 与 8790 同段且无冲突）。

| 项 | 值 |
| --- | --- |
| 监听 | `127.0.0.1:8795`（不监听外网） |
| 系统单元 | `usbdevd.service`（系统级，root） |
| 健康检查 | `GET /api/health` |
| 门户卡片 | `screen/portal/services.json` 里的 `usb-devices` |

---

## 3. 依赖

- Python 3（只用标准库，无第三方包）；
- `journalctl`（读内核枚举/掉线历史）、`udevadm`（实时 add/remove 事件）；
- `/usr/share/misc/usb.ids`（可选，用于把 VID:PID 翻成厂商/产品名，缺失时退回 sysfs 字符串）。

---

## 4. 安装 / 卸载

```bash
cd local_service/usb_device_manager

# 安装（必须 root：authorized 只有 root 可写）
sudo ./install-autostart.sh

# 只在前台跑（非 root 只能查看）
./start.sh
```

`install-autostart.sh` 会做四件事：

1. 把 `systemd/usbdevd.service` 里的 `@DIR@` 换成仓库路径，装到 `/etc/systemd/system/`；
2. 把 `systemd/49-usbdevd.rules` 的 `@USER@` 换成当前 sudo 用户，装到
   `/etc/polkit-1/rules.d/`，让门户能免密 start/stop 这一个单元（与 `49-screen-usbnicd.rules` 同思路）；
3. 建 `/var/lib/usbdevd`（操作历史、启动快照）与 `/etc/usbdevd`（配置）；
4. `daemon-reload` + `enable` + `restart`，并打印地址与日志命令。

卸载：

```bash
sudo ./uninstall-autostart.sh      # 保留 /var/lib/usbdevd 里的历史
```

---

## 5. 架构

```
backend/usbdev_core.py     纯逻辑：sysfs 扫描、拓扑、端点带宽、保护判定、journal 解析、
                           authorized 写入、历史与快照持久化。可单独当 CLI 用。
backend/usbdev_server.py   HTTP 服务（127.0.0.1:8795）、udev 实时监听、CLI 参数。
web/usb-devices.html       单文件前端（深色/浅色跟随系统，无外部依赖）。
systemd/                   系统单元 + polkit 规则模板。
scripts 即仓库根目录的 start.sh / run-service.sh / install-autostart.sh / uninstall-autostart.sh。
```

数据流：页面 8 秒轮询 `GET /api/state` → `usbdev_core.snapshot()` 现场读 sysfs（不做缓存）
→ journal 解析结果缓存 20 秒。任何写操作都会先重新 `scan_devices()`，避免拿旧快照做判断。

---

## 6. 权限模型

`/sys/bus/usb/devices/<path>/authorized` 是 `-rw-r--r-- root root`，只有 root 能写。
因此服务按**系统级单元以 root 运行**（与 `usbnicd` 一致），但把风险面压到最小：

- 只监听 `127.0.0.1`；
- 只写 `authorized`（以及启用端口重连后的 `disable`），其余 sysfs 全部只读；
- 单元里加了 `ProtectSystem=full`、`ProtectHome=read-only`、`PrivateTmp=yes`、`NoNewPrivileges=yes`；
- 写接口要求自定义头 `X-USBDev-Client: usbdev-ui` 和 Origin 白名单（`127.0.0.1` / `localhost`），
  防止任意网页通过表单跨站触发；
- 所有操作记入 `/var/lib/usbdevd/history.jsonl`，带时间、路径、VID:PID、保护等级、前后状态。

如果不想用 root 常驻，可以：非 root 前台运行只看不写；需要操作时用
`sudo python3 backend/usbdev_server.py --link-off 3-8 --yes` 这样的 CLI（见第 7 节）。

---

## 7. API 与 CLI

### HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{ok, service, root}` |
| GET | `/api/state` | 全量快照：host / capabilities / buses / rows / ports / protected / counts / autosuspend / dock / history / journal / issues / boot_diff / udev |
| GET | `/api/history?limit=200` | 操作历史（新→旧） |
| GET | `/api/journal` | 最近三次启动的枚举/掉线/复位事件 |
| POST | `/api/link` | `{path, enable, confirm, actor, note}`；关闭受保护设备时 `confirm` 必须等于设备路径 |
| POST | `/api/port-replug` | `{port, confirm}`；端口 `disable` 1→0，默认关闭（见第 8 节） |
| POST | `/api/refresh` | 强制重扫 journal 并返回全量快照 |

### CLI

```bash
python3 backend/usbdev_server.py --tree          # 拓扑（带保护标记）
python3 backend/usbdev_server.py --ports         # 所有 hub 端口
python3 backend/usbdev_server.py --protect       # 受保护设备及原因
python3 backend/usbdev_server.py --dock          # 扩展坞两个根口状态
python3 backend/usbdev_server.py --journal       # 最近枚举/掉线/复位
python3 backend/usbdev_server.py --history       # 操作历史
python3 backend/usbdev_server.py --check         # 权限自检（authorized 是否可写）
python3 backend/usbdev_server.py --state         # 全量 JSON
python3 backend/usbdev_server.py --link-off 3-8 --yes     # 关闭链路（受保护设备必须 --yes）
python3 backend/usbdev_server.py --link-on 3-8            # 打开链路
```

---

## 8. 「链路开关」的准确语义

### 8.1 它是逻辑断开，不是断电

写 `authorized=0` 后：

- 端口仍然供电，设备仍然上电，USB 线仍然插着；
- 内核卸载该设备的接口驱动，`/dev` 节点、netdev、input 设备、块设备随之消失；
- `/sys/bus/usb/devices/<path>` 节点通常仍保留，`authorized` 变成 0，设备「像被系统无视」；
- 如果是 hub，下游设备也会一起被摘掉（因此 hub 一律列为受保护）。

写 `authorized=1` 后：内核重新读取描述符、重新设置配置、重新绑定驱动，**完整走一遍枚举**。

### 8.2 和物理拔插 / 端口断电 / 设备 reset 的差别

| 手段 | 动 OS 侧 | 动设备固件 | 动供电 | 会重新枚举 | 会影响同口其它设备 |
| --- | --- | --- | --- | --- | --- |
| `authorized` 0→1 | 是 | 否 | 否 | 是 | 否（hub 除外，级联） |
| 物理拔插 | 是 | 是（掉电复位） | 是（VBUS） | 是 | 否 |
| 端口 `disable` 1→0 | 是 | 是（端口复位） | 否（多数实现不掉 VBUS） | 是 | 是（整口） |
| 端口/hub 断电 | 是 | 是 | 是 | 是 | 是 |
| `USBDEVFS_RESET` | 是 | 是（设备复位） | 否 | 否（不重读描述符） | 否 |

第一版只提供第一行。后三行只在页面/CLI 里做只读展示或接口位。

### 8.3 关闭后怎么恢复

1. 页面上把该设备的开关重新打开（`authorized=1`）；
2. 或 CLI：`python3 backend/usbdev_server.py --link-on <path>`；
3. 或直接 `echo 1 | sudo tee /sys/bus/usb/devices/<path>/authorized`；
4. 或物理拔插（永远有效）。

关闭后设备在 sysfs 的路径不变，所以**只要节点还在，就一定能用同一个路径打开**。
这也是它比拔插更方便的地方：路径不会因为重新插到别的口而变化。

### 8.4 它的边界（必须知道）

`authorized` 只能作用于**已经在 sysfs 里有节点**的设备。如果设备在开机时根本没枚举出来，
`/sys/bus/usb/devices/3-1` 这个目录不存在，就没有对象可写 —— 此时软件链路开关无能为力，
需要端口级重连（8.5）或物理拔插。

### 8.5 端口重连（默认关闭的接口位）

`/sys/bus/usb/devices/usbX/X-0:1.0/usbX-portN/disable` 写 `1`、等 1 秒、写 `0`，
会让 xHCI 禁用该端口再重新使能，并使能时做一次真正的端口复位 —— 效果等价于拔插。

它只允许两个白名单端口（扩展坞的 `usb3-port1` / `usb4-port2`），并且默认关闭：

```bash
sudo tee /etc/usbdevd/config.json >/dev/null <<'JSON'
{ "allow_port_replug": true }
JSON
sudo systemctl restart usbdevd
```

打开后页面「扩展坞冷启动」卡片会出现「端口重连」按钮，需要输入端口名二次确认。
不建议默认打开的原因：它会断开该口下所有设备（网卡/键鼠/WiFi），
只有确认「端口下已经看不到设备」时才该用。

---

## 9. 受保护设备

判定在 `usbdev_core._protect()`，分四级：

| 等级 | 含义 | 关闭链路的要求 |
| --- | --- | --- |
| `blocked` | 根 hub | 直接拒绝 |
| `critical` | 关键设备（网卡/WiFi/蓝牙/HID/已挂载存储/含关键下游的 hub） | 必须二次确认（`confirm` = 设备路径），页面显示断网/断输入警告 |
| `protected` | 普通 hub、未挂载的存储 | 必须二次确认 |
| `free` | 叶子设备 | 可直接操作 |

本机当前的关键设备：

| 路径 | 设备 | 原因 |
| --- | --- | --- |
| `4-2.2` | RTL8156B 2.5G 网卡 | 承载 `usbnic-gateway`（10.10.10.1/24，shared）；断链路 = 断网关/SSH/远程会话 |
| `3-1.3` | RTL8811CU WiFi | 承载 WiFi / 软 AP |
| `3-1.1` | MCHOSE 键盘 | HID 输入 |
| `3-1.4` | 罗技接收器（PRO X + 鼠标） | HID 输入 |
| `3-5` | ITE 内置键盘 | HID 输入 |
| `3-10` | Intel AX211 蓝牙 | 蓝牙键鼠/音频 |
| `3-1` / `4-2` | 扩展坞 hub | 下游含网卡/键盘/WiFi，级联保护 |

存储是否受保护是**动态**判定的：读 `/proc/mounts`，只要该 USB 设备下有分区被挂载，
就升为 `critical` 并列出挂载点。

---

## 10. 只读信息与平台局限

页面/接口能拿到的：

- 设备：VID:PID、厂商/产品/序列号、速度、USB 版本、bcdDevice、配置值、接口数与端点；
- 端口：`state`、`connect_type`、`disable`、`over_current_count`、`location`、`peer`（USB2/USB3 双枚举配对）；
- 电源：`power/control`、`runtime_status`、`autosuspend_delay_ms`、`wakeup`、标称 `bMaxPower`；
- 带宽：按端点 `type` / `wMaxPacketSize` / `interval` 估算的周期预留（明确标注是估算）；
- 历史：`journalctl -k` 最近三次启动的枚举/掉线/复位/枚举失败，加 udev 实时 add/remove。

拿不到的（如实标注，不做假数据）：

- **没有 per-port 电压/电流传感器**：`/sys/class/typec/` 为空，`/sys/class/power_supply/` 只有适配器与电池，
  所以「端口供电」只能给标称 `bMaxPower` + over-current 计数 + 电源策略状态；
- `/sys/kernel/debug/usb/devices` 需要 root 且 debugfs 挂载才有，本服务不依赖它；
- BIOS 里的 xHCI hand-off / Legacy USB / Fast Boot 在 Linux 下读不到，只能进 BIOS 看。

---

## 11. 扩展坞冷启动诊断（这台机器的重点问题）

### 11.1 设备

| 项 | 值 |
| --- | --- |
| USB2 侧 | `05e3:0610` GenesysLogic USB2.1 Hub，`bcdDevice=94.06`，`3-1`，root port `usb3-port1` |
| USB3 侧 | `05e3:0625` GenesysLogic USB3.2 Hub，`bcdDevice=94.06`，`4-2`，root port `usb4-port2` |
| 供电 | `bmAttributes=0xe0`（自供电）、`bMaxPower=0mA` → 自带外部电源，不靠主机 VBUS |
| 双枚举 | 是，同一上游口的 USB2/USB3 两套 hub（端口的 `peer` 互相指向） |

### 11.2 已证实的现象（boot -2，2026-11-10 20:13）

```
usb 3-1: new low-speed USB device number 2 using xhci_hcd     ← 高速 hub 被误判成低速
usb 3-1: device descriptor read/64, error -71                 ← ×4
usb usb3-port1: attempt power cycle                           ← 内核自动 power cycle，无效
usb 3-1: device not accepting address 4, error -71
usb 3-1: device not accepting address 5, error -71
usb usb3-port1: unable to enumerate USB device                ← 内核放弃
（71 秒内没有任何重试）
usb 3-1: new high-speed USB device number 10 using xhci_hcd    ← 物理拔插
usb 3-1: New USB device found, idVendor=05e3, idProduct=0610
hub 3-1:1.0: 4 ports detected
```

- 失败的是**整个 hub 本体**，不是下游设备：失败期间连 `hub 3-1:1.0: 4 ports detected` 都没有；
- 全量日志里 `over-current` 与 `Cannot enable. Maybe the USB cable is bad?` 均为 0 次；
- 7 次开机中只有 boot -2 首次识别为 `low-speed`，其余都是 `high-speed`；
- 冷启动并非必然失败（约 40% 命中），属于上电时序竞态；
- 与 autosuspend 无关（失败发生在运行时 PM 介入之前）。

### 11.3 结论

已证实：主机侧枚举失败后**不会自愈**，且内核已自动尝试过一次端口 power cycle，仍然无效。

推断（依据充分）：扩展坞自供电，主机 VBUS 掉电不能复位它；开机瞬间坞内电源/固件尚未就绪，
上游 D+/D- 电平无效（因此被误判成 low-speed）。主机放弃后把端口置为 disabled，
而 hub 侧没有看到 VBUS 掉电、不认为自己被拔出，于是不会再发 connect 事件 —— 双方互等，
只有物理断开一次数据线才能解开。

### 11.4 对服务的影响

- `authorized` 0→1 **不能**解决这个场景（失败时没有 sysfs 节点可写）；
- 解法是端口级重连（8.5），只针对 `usb3-port1` / `usb4-port2`；
- 该能力默认关闭，需要显式打开配置；打开后必须先确认端口下确实没有设备，避免误断网/输入。

### 11.5 需要人工配合的验证

1. 下次冷启动若复现：先不要拔插，用 `--dock` 确认 `usb3-port1` 无设备，再启用
   `allow_port_replug` 并点一次「端口重连」，看是否等效拔插；
2. 进 BIOS 检查：`XHCI Hand-off`（建议 Enabled）、`Legacy USB Support`（Enabled/Auto）、
   `Fast Boot`（建议关闭）、`USB power in S5/S4`（建议关闭）。

---

## 12. 回滚

```bash
sudo ./uninstall-autostart.sh
```

- 停止并 disable `usbdevd.service`，删除单元与 polkit 规则；
- 不动 `screen` / `theme_manager` / `usb_wireless_network` 的任何文件；
- `/var/lib/usbdevd/` 保留（历史记录与快照），需要时手动删；
- 若曾打开 `allow_port_replug`，回滚前把 `/etc/usbdevd/config.json` 改回 `false` 即可，
  不会留下任何 sysfs 副作用（`disable` 写 0 后即为默认状态）。

门户侧回滚：删掉 `screen/portal/services.json` 里 `usb-devices` 那一段即可，5 秒内热加载。

---

## 13. 已知限制 / 后续接口位

- 端口重连只白名单扩展坞两个根口，其它端口未开放；
- 不支持对外网监听，也没有鉴权（依赖 127.0.0.1 + 自定义头 + Origin 检查）；
- udev 事件只存在内存里，服务重启即丢（历史仍可从 journal 补）；
- 端点带宽是描述符估算，不是内核实际分配；
- 无 per-port 电流/电压实测（硬件不暴露）。
