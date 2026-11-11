#!/usr/bin/env python3
"""USB 设备管理与「通信链路开关」核心逻辑。

第一版的可操作面只有一件事：
    按设备写 /sys/bus/usb/devices/<路径>/authorized（0 = 逻辑断开，1 = 重新授权）

它等价于「软件拔插」：设备仍然插着、端口仍然供电，但 OS 侧把设备从 USB 核心上摘掉，
接口驱动解绑、/dev 节点与 netdev 消失；写回 1 后内核会重新读描述符、重新设配置、
重新绑定驱动，也就是完整走一遍枚举流程。

刻意不做（第一版）：端口断电、hub 断电、端口 disable、USBDEVFS_RESET、驱动 unbind。
这些在 README 里列为后续接口位。

只用 Python 标准库；sysfs 一律只读（除 authorized 外不写任何文件）。
"""

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone

SYSFS_USB = "/sys/bus/usb/devices"
STATE_DIR = os.environ.get("USBDEVD_STATE_DIR", "/var/lib/usbdevd")
HISTORY_PATH = os.path.join(STATE_DIR, "history.jsonl")
SNAPSHOT_PATH = os.path.join(STATE_DIR, "last-snapshot.json")

DEV_RE = re.compile(r"^\d+-[\d.]+$")
ROOT_RE = re.compile(r"^usb(\d+)$")
IFACE_RE = re.compile(r"^(\d+-[\d.]+):(\d+)\.(\d+)$")
PORT_RE = re.compile(r"^(?:usb(\d+)|\d+-[\d.]+)-port(\d+)$")

SPEED_LABEL = {
    "1.5": "1.5 Mbps（低速）",
    "12": "12 Mbps（全速）",
    "480": "480 Mbps（高速）",
    "5000": "5 Gbps（超速）",
    "10000": "10 Gbps（超速+）",
    "20000": "20 Gbps（超速++）",
}

CLASS_NAME = {
    0: "按接口定义", 1: "音频", 2: "CDC 通信", 3: "HID 输入", 5: "物理",
    6: "静态图像", 7: "打印机", 8: "大容量存储", 9: "Hub", 10: "CDC 数据",
    11: "智能卡/Billboard", 13: "内容安全", 14: "视频", 15: "个人医疗",
    16: "音视频", 17: "诊断", 224: "无线（厂商）", 239: "杂项", 254: "应用特定",
    255: "厂商自定义",
}

# 固定 VID:PID 的关键设备。键是大写的 VID:PID。
CRITICAL_IDS = {
    "0BDA:8156": ("2.5G USB 网卡（RTL8156B）",
                  "承载 usbnic-gateway（10.10.10.1/24，ipv4.method=shared）；"
                  "断开链路会同时断本机网关、SSH 与远程会话"),
    "0BDA:C811": ("WiFi 网卡（RTL8811CU）", "承载 WiFi / 软 AP 上游；断开链路会断网"),
    "0BDA:1A2B": ("WiFi 网卡（RTL8811CU，CD/DISK 模式）",
                  "这是 0bda:c811 的模式切换前状态；断开链路会让 usb_modeswitch 无法完成，WiFi 起不来"),
    "8087:0033": ("蓝牙（Intel AX211）", "蓝牙键鼠 / 音频依赖它；断开链路会失去输入设备"),
}

# 这些是「即使物理上只有一个设备也不能关」的东西：根 hub 直接拒绝。
BLOCKED_NOTE = "根 hub：关闭链路等于把整条总线上的设备一起摘掉，已禁止"


def _read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _read_bytes(path, limit=64):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _read_int(path, default=None):
    raw = _read(path)
    if not raw:
        return default
    try:
        return int(raw, 0)
    except ValueError:
        try:
            return int(raw.split()[0])
        except (ValueError, IndexError):
            return default


def _read_hex(path, default=None):
    """sysfs 里 wMaxPacketSize 之类是十六进制字符串."""
    raw = _read(path)
    if not raw:
        return default
    try:
        return int(raw, 16)
    except ValueError:
        return default


def _sanitize(raw):
    """physical_location 是 ACPI _PLD 二进制，只保留可打印 ASCII，其余转 hex."""
    if not raw:
        return ""
    try:
        text = raw.decode("ascii")
        if text.isprintable():
            return text.strip("\x00").strip()
    except UnicodeDecodeError:
        pass
    return "0x" + raw.hex()


def _iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _boot_id():
    return _read("/proc/sys/kernel/random/boot_id")


def _parse_duration_ms(text):
    """端点 interval 形如 '1ms' / '125us' / '0ms'."""
    m = re.match(r"^\s*([\d.]+)\s*(ms|us|s)\s*$", text or "")
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "us":
        return val / 1000.0
    if unit == "s":
        return val * 1000.0
    return val


def _iface_dir(dev_path, iface_name):
    return os.path.join(SYSFS_USB, dev_path, iface_name)


# ── usb.ids 名称表（可选，缺失就退回 sysfs 字符串）─────────────────────────
_USB_IDS = None


def _usb_ids():
    global _USB_IDS
    if _USB_IDS is not None:
        return _USB_IDS
    table = {}
    for candidate in ("/usr/share/misc/usb.ids", "/usr/share/hwdata/usb.ids"):
        try:
            with open(candidate, "r", errors="replace") as f:
                vendor = None
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        parts = line.rstrip().split(None, 1)
                        if len(parts) == 2 and re.match(r"^[0-9a-fA-F]{4}$", parts[0]):
                            vendor = parts[0].upper()
                            table.setdefault("V:" + vendor, parts[1].strip())
                    else:
                        m = re.match(r"^\t([0-9a-fA-F]{4})\s+(.*)$", line)
                        if m and vendor:
                            table.setdefault("P:%s:%s" % (vendor, m.group(1).upper()),
                                             m.group(2).strip())
            break
        except OSError:
            continue
    _USB_IDS = table
    return table


def _ids_name(vid, pid=None):
    table = _usb_ids()
    if pid:
        return table.get("P:%s:%s" % (vid, pid), "")
    return table.get("V:" + vid, "")


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# ── 端点 / 带宽估算 ─────────────────────────────────────────────────────────
def _scan_endpoints(dev_path, iface_name):
    """返回该接口下所有非 0 号端点的描述与估算带宽。

    估算口径：周期端点（interrupt / isoc）按 wMaxPacketSize × 8 / interval 计算
    「每秒预留字节」。这是描述符层面的上限估算，不是内核实际分配值。
    """
    out = []
    base = _iface_dir(dev_path, iface_name)
    for name in _listdir(base):
        if not name.startswith("ep_"):
            continue
        ep_dir = os.path.join(base, name)
        typ = _read(os.path.join(ep_dir, "type"))
        if not typ:
            continue
        maxp = _read_hex(os.path.join(ep_dir, "wMaxPacketSize"), 0) or 0
        interval_ms = _parse_duration_ms(_read(os.path.join(ep_dir, "interval")))
        addr = name[3:]
        item = {
            "addr": addr,
            "dir": "in" if addr.startswith("8") or addr.startswith("9")
                   else "out" if addr.startswith("0") or addr.startswith("1") else "?",
            "type": typ,
            "wMaxPacketSize": maxp,
            "interval_ms": interval_ms,
            "bandwidth_bps": 0,
        }
        if typ.lower() in ("interrupt", "isoc", "isochronous") and interval_ms > 0:
            item["bandwidth_bps"] = int(maxp * 8 * 1000.0 / interval_ms)
        out.append(item)
    return out


def _scan_interfaces(dev_path):
    out = []
    for name in _listdir(os.path.join(SYSFS_USB, dev_path)):
        m = IFACE_RE.match(name)
        if not m or m.group(1) != dev_path:
            continue
        iface_dir = _iface_dir(dev_path, name)
        drv = ""
        drv_link = os.path.join(iface_dir, "driver")
        if os.path.exists(drv_link):
            drv = os.path.basename(os.path.realpath(drv_link))
        cls = _read_int(os.path.join(iface_dir, "bInterfaceClass"), 0) or 0
        out.append({
            "name": name,
            "number": int(m.group(2)),
            "alt": int(m.group(3)),
            "class": cls,
            "class_name": CLASS_NAME.get(cls, "类 0x%02x" % cls),
            "subclass": _read_int(os.path.join(iface_dir, "bInterfaceSubClass"), 0),
            "protocol": _read_int(os.path.join(iface_dir, "bInterfaceProtocol"), 0),
            "driver": drv or "",
            "endpoints": _scan_endpoints(dev_path, name),
        })
    out.sort(key=lambda i: (i["number"], i["alt"]))
    return out


# ── Hub 端口 ────────────────────────────────────────────────────────────────
def _hub_ports(hub_path):
    """读取某个 hub 设备下所有端口目录（usb3-port1 / 3-1-port2 …）。"""
    ports = []
    root_match = ROOT_RE.match(hub_path)
    for iface_name in _listdir(os.path.join(SYSFS_USB, hub_path)):
        m = IFACE_RE.match(iface_name)
        if not m:
            continue
        # 根 hub 的接口名是 3-0:1.0（总线号-0），普通 hub 是 3-1:1.0
        if root_match:
            if m.group(1) != "%s-0" % root_match.group(1):
                continue
        elif m.group(1) != hub_path:
            continue
        if _read_int(os.path.join(SYSFS_USB, hub_path, iface_name,
                                 "bInterfaceClass"), 0) != 9:
            continue
        idir = _iface_dir(hub_path, iface_name)
        for port_name in _listdir(idir):
            pm = PORT_RE.match(port_name)
            if not pm:
                continue
            pdir = os.path.join(idir, port_name)
            state = _read(os.path.join(pdir, "state"))
            linked = ""
            try:
                linked = os.path.basename(
                    os.path.realpath(os.path.join(pdir, "device")))
            except OSError:
                linked = ""
            if linked == "device":
                linked = ""
            ports.append({
                "path": "%s-port%s" % (hub_path, pm.group(2)),
                "port": int(pm.group(2)),
                "hub": hub_path,
                "state": state or "unknown",
                "connect_type": _read(os.path.join(pdir, "connect_type")),
                "disable": _read_int(os.path.join(pdir, "disable")),
                "over_current_count": _read_int(os.path.join(pdir, "over_current_count"), 0),
                "location": _read(os.path.join(pdir, "location")),
                "physical_location": _sanitize(_read_bytes(os.path.join(pdir, "physical_location"))),
                "peer": os.path.basename(
                    os.path.realpath(os.path.join(pdir, "peer"))) if os.path.exists(
                        os.path.join(pdir, "peer")) else "",
                "waiting_for_supplier": _read(os.path.join(pdir, "waiting_for_supplier")),
                "device": linked,
                "empty": not linked,
            })
    ports.sort(key=lambda p: p["port"])
    return ports


# ── 设备扫描 ────────────────────────────────────────────────────────────────
def _scan_one(name):
    base = os.path.join(SYSFS_USB, name)
    vid = _read(os.path.join(base, "idVendor")).upper()
    pid = _read(os.path.join(base, "idProduct")).upper()
    ifaces = _scan_interfaces(name)
    drivers = sorted({i["driver"] for i in ifaces if i["driver"]})
    ports = _hub_ports(name)
    dev = {
        "path": name,
        "kind": "root" if ROOT_RE.match(name) else "device",
        "vid": vid,
        "pid": pid,
        "vidpid": ("%s:%s" % (vid, pid)) if vid else "",
        "vendor_name": _ids_name(vid) if vid else "",
        "product_name": _ids_name(vid, pid) if vid and pid else "",
        "manufacturer": _read(os.path.join(base, "manufacturer")),
        "product": _read(os.path.join(base, "product")),
        "serial": _read(os.path.join(base, "serial")),
        "busnum": _read_int(os.path.join(base, "busnum"), 0),
        "devnum": _read_int(os.path.join(base, "devnum"), 0),
        "speed": _read(os.path.join(base, "speed")),
        "speed_label": SPEED_LABEL.get(_read(os.path.join(base, "speed")), ""),
        "usb_version": _read(os.path.join(base, "version")),
        "bcdDevice": _read(os.path.join(base, "bcdDevice")),
        "bDeviceClass": _read_int(os.path.join(base, "bDeviceClass"), 0),
        "bDeviceSubClass": _read_int(os.path.join(base, "bDeviceSubClass"), 0),
        "bDeviceProtocol": _read_int(os.path.join(base, "bDeviceProtocol"), 0),
        "bNumConfigurations": _read_int(os.path.join(base, "bNumConfigurations"), 0),
        "bNumInterfaces": _read_int(os.path.join(base, "bNumInterfaces"), 0),
        "bConfigurationValue": _read(os.path.join(base, "bConfigurationValue")),
        "bMaxPower": _read(os.path.join(base, "bMaxPower")),
        "bMaxPacketSize0": _read_int(os.path.join(base, "bMaxPacketSize0"), 0),
        "bmAttributes": _read(os.path.join(base, "bmAttributes")),
        "authorized": _read_int(os.path.join(base, "authorized")),
        "removable": _read(os.path.join(base, "removable")),
        "maxchild": _read_int(os.path.join(base, "maxchild"), 0),
        "rx_lanes": _read_int(os.path.join(base, "rx_lanes")),
        "tx_lanes": _read_int(os.path.join(base, "tx_lanes")),
        "urbnum": _read_int(os.path.join(base, "urbnum"), 0),
        "power_control": _read(os.path.join(base, "power/control")),
        "runtime_status": _read(os.path.join(base, "power/runtime_status")),
        "runtime_active_time": _read(os.path.join(base, "power/runtime_active_time")),
        "runtime_suspended_time": _read(os.path.join(base, "power/runtime_suspended_time")),
        "autosuspend_delay_ms": _read_int(os.path.join(base, "power/autosuspend_delay_ms")),
        "wakeup": _read(os.path.join(base, "power/wakeup")),
        "interfaces": ifaces,
        "drivers": drivers,
        "ports": ports,
        "is_hub": bool(ports) or any(i["class"] == 9 for i in ifaces),
        "parent": _parent_path(name),
        "children": [],
        "bandwidth_bps": sum(e["bandwidth_bps"] for i in ifaces for e in i["endpoints"]),
    }
    dev["name"] = _display_name(dev)
    return dev


def _parent_path(path):
    if ROOT_RE.match(path):
        return ""
    head = path.split("-")[0]
    tail = path.split("-", 1)[1]
    parts = tail.split(".")
    if len(parts) == 1:
        return "usb%s" % head
    return "%s-%s" % (head, ".".join(parts[:-1]))


def _display_name(dev):
    if dev["kind"] == "root":
        return "xHCI 根 Hub（%s%s）" % (dev["path"],
                                       "，" + dev["speed_label"] if dev["speed_label"] else "")
    product = dev["product"] or dev["product_name"]
    vendor = dev["manufacturer"] or dev["vendor_name"]
    if product:
        if vendor and vendor not in product:
            return "%s %s" % (vendor, product)
        return product
    if dev["vidpid"] in CRITICAL_IDS:
        return CRITICAL_IDS[dev["vidpid"]][0]
    return "未知设备 %s" % (dev["vidpid"] or "?")


def scan_devices():
    devices = {}
    for name in _listdir(SYSFS_USB):
        if DEV_RE.match(name) or ROOT_RE.match(name):
            if not os.path.exists(os.path.join(SYSFS_USB, name, "idVendor")):
                continue
            devices[name] = _scan_one(name)
    for name, dev in devices.items():
        parent = dev["parent"]
        if parent in devices:
            devices[parent]["children"].append(name)
    for dev in devices.values():
        dev["children"].sort(key=lambda p: [int(x) for x in re.split(r"[-.]", p) if x.isdigit()])
    return devices


# ── 受保护设备判定 ──────────────────────────────────────────────────────────
def _mount_sources():
    sources = []
    try:
        with open("/proc/mounts", "r", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    sources.append((os.path.basename(parts[0]), parts[1]))
    except OSError:
        pass
    return sources


def _mounted_storage(dev_path):
    """USB 设备下是否有已挂载的分区。返回 [(分区, 挂载点)]."""
    hits = []
    prefix = os.path.join(SYSFS_USB, dev_path) + "/"
    for name, mountpoint in _mount_sources():
        real = os.path.realpath(os.path.join("/sys/class/block", name))
        if real.startswith(prefix):
            hits.append((name, mountpoint))
    return hits


def _has_hid(dev):
    return any(i["class"] == 3 for i in dev["interfaces"])


def _protect(dev, devices):
    reasons = []
    level = "free"

    if dev["kind"] == "root":
        return {"level": "blocked", "reasons": [BLOCKED_NOTE]}

    key = dev["vidpid"]
    if key in CRITICAL_IDS:
        label, why = CRITICAL_IDS[key]
        level = "critical"
        reasons.append("%s：%s" % (label, why))

    if _has_hid(dev):
        level = "critical"
        reasons.append("HID 输入设备：断开链路会立刻失去这块键盘/鼠标")

    if any(i["class"] == 8 for i in dev["interfaces"]):
        hits = _mounted_storage(dev["path"])
        if hits:
            level = "critical"
            reasons.append("存储正在挂载：%s；断开链路会导致 I/O 错误"
                           % "、".join("%s→%s" % h for h in hits))
        else:
            reasons.append("大容量存储：断开前请确认没有进程在读写")

    # 级联：hub 下挂有关键设备时，hub 本身也升为 critical
    if dev["children"]:
        child_levels = []
        for child in dev["children"]:
            cdev = devices.get(child)
            if not cdev:
                continue
            cprot = _protect(cdev, devices)
            child_levels.append((cdev, cprot))
            if cprot["level"] in ("critical", "blocked"):
                level = "critical"
        critical_children = [c for c, p in child_levels if p["level"] in ("critical", "blocked")]
        if critical_children:
            reasons.append("以下游设备受保护，断开 hub 会连带断开它们：%s"
                           % "、".join("%s(%s)" % (c["path"], c["name"]) for c in critical_children))
        elif level == "free":
            level = "protected"
            reasons.append("Hub：断开链路会同时断开其下 %d 个设备" % len(dev["children"]))

    if level == "free" and dev["is_hub"]:
        level = "protected"
        reasons.append("Hub：断开链路会同时断开其下所有设备（当前为空口）")

    return {"level": level, "reasons": reasons}


def protection_map(devices):
    return {path: _protect(dev, devices) for path, dev in devices.items()}


# ── 汇总视图 ────────────────────────────────────────────────────────────────
def _rows(devices, path, depth, ports_map, out):
    dev = devices[path]
    out.append({"kind": dev["kind"], "depth": depth, "path": path,
                "device": dev, "port": None})
    # 该设备作为 hub 的端口先于子设备列出
    ports = ports_map.get(path) or []
    for port in ports:
        out.append({"kind": "port", "depth": depth + 1,
                    "path": port["path"], "device": None, "port": port})
    for child in dev["children"]:
        _rows(devices, child, depth + 1, ports_map, out)


def bus_summary(devices):
    buses = {}
    for dev in devices.values():
        if dev["kind"] == "root":
            bus = dev["busnum"]
            buses[bus] = {
                "busnum": bus,
                "root": dev["path"],
                "speed": dev["speed"],
                "speed_label": dev["speed_label"],
                "controller": dev["serial"],
                "interfaces": dev.get("bNumInterfaces"),
                "maxchild": dev["maxchild"],
                "device_count": 0,
                "periodic_bps": 0,
                "bulk_endpoints": 0,
                "periodic_endpoints": 0,
            }
    for dev in devices.values():
        if dev["kind"] == "root":
            continue
        bus = dev["busnum"]
        if bus not in buses:
            continue
        buses[bus]["device_count"] += 1
        for iface in dev["interfaces"]:
            for ep in iface["endpoints"]:
                if ep["bandwidth_bps"]:
                    buses[bus]["periodic_bps"] += ep["bandwidth_bps"]
                    buses[bus]["periodic_endpoints"] += 1
                elif ep["type"].lower() == "bulk":
                    buses[bus]["bulk_endpoints"] += 1

    for info in buses.values():
        try:
            link_bps = float(info["speed"]) * 1e6
        except (TypeError, ValueError):
            link_bps = 0
        # USB 2.0 高速：周期传输最多占 80% 帧带宽；USB 3 不做周期预留，仍是尽力而为
        cap = link_bps * 0.8 if link_bps and link_bps <= 480e6 else link_bps
        info["link_bps"] = int(link_bps)
        info["periodic_capacity_bps"] = int(cap)
        info["periodic_pct"] = round(info["periodic_bps"] * 100.0 / cap, 2) if cap else 0
        info["note"] = ("周期端点按描述符估算，非内核实际分配；"
                        "bulk 端点不预留带宽" if link_bps <= 480e6 else
                        "USB3 无周期预算概念，数值仅为端点描述符上限")
    return [buses[b] for b in sorted(buses)]


def snapshot():
    devices = scan_devices()
    ports_map = {path: dev["ports"] for path, dev in devices.items() if dev["ports"]}
    prot = protection_map(devices)
    rows = []
    for name in sorted(devices, key=lambda p: [int(x) for x in re.split(r"[-.]", p) if x.isdigit()]):
        if devices[name]["parent"] in devices:
            continue
        _rows(devices, name, 0, ports_map, rows)
    for dev in devices.values():
        dev["protection"] = prot[dev["path"]]
        dev["status"] = _status(dev, prot[dev["path"]])

    flat_ports = []
    for path in ports_map:
        for port in ports_map[path]:
            port["hub_name"] = devices[path]["name"] if path in devices else path
            flat_ports.append(port)

    return {
        "ok": True,
        "now": _iso_now(),
        "epoch": time.time(),
        "host": host_info(),
        "capabilities": capabilities(devices),
        "buses": bus_summary(devices),
        "rows": rows,
        "ports": flat_ports,
        "protected": [{"path": p, "name": devices[p]["name"],
                       "vidpid": devices[p]["vidpid"], **prot[p]}
                      for p in sorted(prot) if prot[p]["level"] != "free"],
        "counts": {
            "devices": len([d for d in devices.values() if d["kind"] == "device"]),
            "hubs": len([d for d in devices.values() if d["is_hub"] and d["kind"] == "device"]),
            "ports_total": len(flat_ports),
            "ports_occupied": len([p for p in flat_ports if not p["empty"]]),
            "link_off": len([d for d in devices.values()
                             if d["kind"] == "device" and d["authorized"] == 0]),
            "critical": len([1 for d in devices.values()
                             if d["kind"] == "device" and prot[d["path"]]["level"] == "critical"]),
            "protected": len([1 for d in devices.values()
                              if d["kind"] == "device" and prot[d["path"]]["level"] == "protected"]),
        },
        "autosuspend": autosuspend_info(devices),
        "dock": dock_info(devices),
    }


def _status(dev, prot):
    """只描述健康状态；「是否受保护」由 protection 单独表达，避免两个维度混在一起。"""
    if dev["kind"] == "root":
        return {"code": "root", "label": "根 Hub"}
    if dev["authorized"] == 0:
        return {"code": "link_off", "label": "链路已关闭"}
    if not dev["bConfigurationValue"]:
        return {"code": "unconfigured", "label": "未配置（枚举未完成）"}
    if dev["is_hub"]:
        return {"code": "hub", "label": "Hub"}
    if not dev["drivers"]:
        return {"code": "no_driver", "label": "无驱动"}
    return {"code": "ok", "label": "正常"}


def host_info():
    return {
        "kernel": os.uname().release,
        "cmdline": _read("/proc/cmdline"),
        "boot_id": _boot_id(),
        "usbcore_autosuspend": _read("/sys/module/usbcore/parameters/autosuspend"),
        "usbfs_memory_mb": _read("/sys/module/usbcore/parameters/usbfs_memory_mb"),
        "debugfs_usb_readable": os.access("/sys/kernel/debug/usb/devices", os.R_OK),
        "typec_ports": _listdir("/sys/class/typec"),
        "power_supplies": _listdir("/sys/class/power_supply"),
        "root_hubs": [n for n in _listdir(SYSFS_USB) if ROOT_RE.match(n)],
        "usb_ids_entries": len(_usb_ids()),
    }


def autosuspend_info(devices):
    auto, on, suspended = [], [], []
    for path, dev in devices.items():
        if dev["kind"] == "root":
            continue
        if dev["power_control"] == "auto":
            auto.append(path)
        elif dev["power_control"] == "on":
            on.append(path)
        if dev["runtime_status"] == "suspended":
            suspended.append(path)
    return {
        "kernel_param": _read("/sys/module/usbcore/parameters/autosuspend"),
        "cmdline_override": [x for x in _read("/proc/cmdline").split() if "autosuspend" in x],
        "auto": sorted(auto),
        "on": sorted(on),
        "suspended": sorted(suspended),
        "note": "power/control=auto 允许内核自动挂起空闲设备；驱动或 NetworkManager 会把在用设备置为 on",
    }


def capabilities(devices=None):
    devices = devices or scan_devices()
    writable = []
    for path, dev in devices.items():
        if path == "usb" or dev["kind"] == "root":
            continue
        auth = os.path.join(SYSFS_USB, path, "authorized")
        if os.access(auth, os.W_OK):
            writable.append(path)
    return {
        "euid": os.geteuid(),
        "root": os.geteuid() == 0,
        "can_toggle": bool(writable),
        "writable_paths": sorted(writable)[:8],
        "writable_count": len(writable),
        "note": "" if writable else "authorized 只有 root 可写：请以 root 运行 usbdevd，或用 pkexec 手动操作",
    }


# ── 链路开关 ────────────────────────────────────────────────────────────────
def _subtree(path, devices):
    out = [path]
    for child in devices.get(path, {}).get("children", []):
        out.extend(_subtree(child, devices))
    return out


def _state_of(devices, path):
    dev = devices.get(path) or {}
    return {
        "present": bool(dev),
        "authorized": dev.get("authorized"),
        "drivers": dev.get("drivers") or [],
        "interfaces": [i["name"] for i in (dev.get("interfaces") or [])],
        "devnum": dev.get("devnum"),
        "children": dev.get("children") or [],
        "product": dev.get("product") or "",
    }


def set_link(path, enable, confirm="", actor="local-ui", note=""):
    """打开/关闭某设备的 USB 通信链路（写 authorized）."""
    devices = scan_devices()
    if path not in devices:
        return {"ok": False, "error": "找不到设备 %s（可能已经掉线）" % path}
    dev = devices[path]
    prot = _protect(dev, devices)

    if dev["kind"] == "root":
        return {"ok": False, "error": BLOCKED_NOTE, "protection": prot}

    if not enable:
        if prot["level"] == "blocked":
            return {"ok": False, "error": BLOCKED_NOTE, "protection": prot}
        if prot["level"] in ("critical", "protected") and confirm != path:
            return {
                "ok": False,
                "error": "该设备受保护，关闭链路需要二次确认：confirm 必须等于设备路径 %s" % path,
                "protection": prot,
                "need_confirm": path,
            }

    auth_path = os.path.join(SYSFS_USB, path, "authorized")
    if not os.path.exists(auth_path):
        return {"ok": False, "error": "设备没有 authorized 属性，无法操作"}
    if not os.access(auth_path, os.W_OK):
        return {"ok": False, "error": "没有写入权限（authorized 需要 root）"}

    subtree = _subtree(path, devices)
    before = {p: _state_of(devices, p) for p in subtree}
    target = 1 if enable else 0

    started = time.time()
    try:
        with open(auth_path, "w") as f:
            f.write("%d\n" % target)
    except OSError as e:
        entry = _history_entry(path, dev, "link_on" if enable else "link_off",
                               False, "写入失败：%s" % e, actor, note, prot, before, {})
        _append_history(entry)
        return {"ok": False, "error": "写入 authorized 失败：%s" % e, "protection": prot}

    # 等内核把设备树、驱动、/dev 节点处理完（最多 5 秒）
    after = {}
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.25)
        cur = scan_devices()
        after = {p: _state_of(cur, p) for p in subtree}
        if enable:
            ready = (path in cur and cur[path]["authorized"] == 1
                     and (cur[path]["bConfigurationValue"] or cur[path]["is_hub"]))
            if ready:
                break
        else:
            if after.get(path, {}).get("authorized") == 0:
                break

    missing = [p for p in subtree if not after.get(p, {}).get("present")]
    detail = ("链路已开启，内核已重新枚举" if enable
              else "链路已关闭，设备已从驱动与 /dev 上摘除")
    if enable and (path not in after or after[path].get("authorized") != 1):
        detail = "已写入 1，但 5 秒内没有观察到重新枚举成功（可稍后刷新确认）"
    if not enable and after.get(path, {}).get("authorized") != 0:
        detail = "已写入 0，但设备状态未在 5 秒内更新"

    entry = _history_entry(path, dev, "link_on" if enable else "link_off",
                           True, detail, actor, note, prot, before, after)
    _append_history(entry)

    return {
        "ok": True,
        "action": "link_on" if enable else "link_off",
        "path": path,
        "detail": detail,
        "elapsed_ms": int((time.time() - started) * 1000),
        "protection": prot,
        "before": before,
        "after": after,
        "disappeared": missing,
        "history": entry,
    }


def _history_entry(path, dev, action, ok, detail, actor, note, prot, before, after):
    return {
        "ts": _iso_now(),
        "epoch": time.time(),
        "action": action,
        "ok": ok,
        "path": path,
        "vidpid": dev.get("vidpid", ""),
        "product": dev.get("product") or dev.get("name", ""),
        "level": prot.get("level", ""),
        "actor": actor or "local-ui",
        "note": note or "",
        "detail": detail,
        "before": before,
        "after": after,
    }


def _ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    except OSError:
        pass


def _append_history(entry):
    _ensure_state_dir()
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(limit=200):
    _ensure_state_dir()
    entries = []
    try:
        with open(HISTORY_PATH, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return {"ok": True, "entries": [], "path": HISTORY_PATH}
    entries.reverse()
    return {"ok": True, "entries": entries[:limit], "path": HISTORY_PATH}


# ── 启动快照 / 与上次比对 ───────────────────────────────────────────────────
def take_snapshot():
    devices = scan_devices()
    current = {
        "ts": _iso_now(),
        "boot_id": _boot_id(),
        "devices": [{"path": p, "vidpid": d["vidpid"], "name": d["name"],
                     "product": d["product"], "serial": d["serial"],
                     "authorized": d["authorized"]}
                    for p, d in sorted(devices.items()) if d["kind"] == "device"],
    }
    previous = {}
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            previous = json.load(f) or {}
    except (OSError, ValueError):
        previous = {}

    prev_paths = {d["path"] for d in previous.get("devices") or []}
    cur_paths = {d["path"] for d in current["devices"]}
    missing = [d for d in (previous.get("devices") or []) if d["path"] not in cur_paths]
    added = [d for d in current["devices"] if d["path"] not in prev_paths]

    same_boot = bool(previous) and previous.get("boot_id") == current["boot_id"]
    # 同一次启动内多次调用不要覆盖「上次启动」的快照，否则 diff 永远为空
    if same_boot:
        missing, added = [], []

    _ensure_state_dir()
    if not same_boot:
        try:
            with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "current_ts": current["ts"],
        "previous_ts": previous.get("ts", ""),
        "previous_boot": previous.get("boot_id", ""),
        "same_boot": same_boot,
        "missing": missing,
        "added": added,
        "current_count": len(current["devices"]),
        "previous_count": len(previous.get("devices") or []),
    }


# ── journal 里的枚举 / 掉线 / 复位历史 ──────────────────────────────────────
_JOURNAL_PATTERNS = [
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): new (?:low-speed|full-speed|high-speed|SuperSpeed Plus|SuperSpeed) USB device number (\d+)"),
     "enumerate", "枚举开始"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): New USB device found, idVendor=([0-9a-fA-F]{4}), idProduct=([0-9a-fA-F]{4})"),
     "identify", "枚举成功"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): USB disconnect, device number (\d+)"),
     "disconnect", "掉线"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): reset (?:low-speed|full-speed|high-speed|SuperSpeed) USB device number (\d+)"),
     "reset", "复位"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): device descriptor read/\d+, error (-?\d+)"),
     "enum_error", "描述符读取失败"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): unable to enumerate USB device"),
     "enum_fail", "枚举失败"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): device not accepting address (\d+), error (-?\d+)"),
     "enum_fail", "地址分配失败"),
    (re.compile(r"^usb (\d+-[\d.]+|usb\d+): can't set config|^usb (\d+-[\d.]+|usb\d+): unable to set config"),
     "enum_fail", "配置失败"),
    (re.compile(r"^hub (\d+-[\d.]+|usb\d+): port (\d+) .*?(over-?current|disabled|reset)"),
     "hub_port", "Hub 端口事件"),
]

_JOURNAL_CACHE = {"ts": 0.0, "events": []}


def scan_journal(boots=3, limit=1500, force=False):
    """从 journalctl -k 汇总枚举/掉线/复位事件（只读）."""
    global _JOURNAL_CACHE
    if not force and time.time() - _JOURNAL_CACHE["ts"] < 20 and _JOURNAL_CACHE["events"]:
        return _JOURNAL_CACHE["events"]

    events = []
    for offset in range(max(1, boots)):
        boot_arg = str(-offset)
        cmd = ["journalctl", "-k", "-o", "short-iso", "--no-pager", "-b", boot_arg]
        # 当前启动取最近 limit 行；历史启动必须从头读，否则早期的枚举失败会被尾部截掉
        if offset == 0:
            cmd += ["-n", str(limit)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                env=dict(os.environ, SYSTEMD_PAGER="", SYSTEMD_COLORS="0"),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (proc.stdout or "").splitlines()[:limit * 6]:
            m = re.match(r"^(\S+) \S+ kernel: (.*)$", line)
            if not m:
                continue
            ts, msg = m.group(1), m.group(2)
            for pattern, kind, label in _JOURNAL_PATTERNS:
                hit = pattern.search(msg)
                if not hit:
                    continue
                path = hit.group(1) if hit.groups() else ""
                groups = [g for g in hit.groups()[1:] if g]
                extra = ":".join(groups) if kind == "identify" else " ".join(groups)
                events.append({
                    "ts": ts,
                    "boot": "当前启动" if offset == 0 else "上次启动" if offset == 1 else "%d 次启动前" % offset,
                    "kind": kind,
                    "path": path,
                    "label": label,
                    "detail": extra,
                    "raw": msg,
                })
                break
    events.sort(key=lambda e: e["ts"])
    _JOURNAL_CACHE = {"ts": time.time(), "events": events}
    return events


# ── 扩展坞冷启动问题：端口级重连（v1.1 接口位，默认关闭）─────────────────
# 依据：见 README「扩展坞冷启动诊断」。authorized 只在设备已有 sysfs 节点时可用；
# 「hub 本体没枚举出来」时只能靠端口 disable 1→0 强制重连。
PORT_REPLUG_ALLOWLIST = {
    "usb3-port1": "扩展坞 USB2 侧（3-1，GenesysLogic 05e3:0610）",
    "usb4-port2": "扩展坞 USB3 侧（4-2，GenesysLogic 05e3:0625）",
}
CONFIG_PATH = os.environ.get("USBDEVD_CONFIG", "/etc/usbdevd/config.json")


def load_config():
    cfg = {"allow_port_replug": False}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def _port_dir(port_key):
    """usb3-port1 -> (usb3, <sysfs>/usb3/3-0:1.0/usb3-port1)"""
    root = port_key.split("-")[0]
    return root, os.path.join(SYSFS_USB, root, "%s-0:1.0" % root[3:], port_key)


def dock_info(devices=None):
    devices = devices or scan_devices()
    ports = []
    for key, label in PORT_REPLUG_ALLOWLIST.items():
        root, pdir = _port_dir(key)
        device = ""
        try:
            device = os.path.basename(os.path.realpath(os.path.join(pdir, "device")))
            if device == "device":
                device = ""
        except OSError:
            pass
        ports.append({
            "key": key,
            "label": label,
            "root": root,
            "state": _read(os.path.join(pdir, "state")),
            "disable": _read_int(os.path.join(pdir, "disable")),
            "over_current_count": _read_int(os.path.join(pdir, "over_current_count"), 0),
            "device": device,
            "present": bool(device),
            "writable": os.access(os.path.join(pdir, "disable"), os.W_OK),
        })
    cfg = load_config()
    return {
        "ports": ports,
        "allow_port_replug": bool(cfg.get("allow_port_replug")),
        "config_path": CONFIG_PATH,
        "note": ("端口 disable 1→0 会让 xHCI 对该口做一次禁用+复位，等价于物理拔插；"
                 "它不受理 authorized，因为 hub 没枚举出来时根本没有设备节点"),
    }


def port_issues(ports, journal_events):
    """把「端口上有失败记录」和「端口状态异常」聚合成待处理项."""
    fails = {}
    for ev in journal_events:
        if ev["kind"] in ("enum_fail", "enum_error"):
            fails.setdefault(ev["path"], []).append(ev)
    issues = []
    for port in ports:
        key = port["path"].split("/")[-1]
        hit = fails.get(port["device"]) or fails.get(key) or []
        if port["device"] and hit:
            issues.append({"kind": "enum_fail", "port": key, "device": port["device"],
                           "count": len(hit), "last": hit[-1], "empty": False})
        elif not port["device"] and port["state"] not in ("not attached", "unknown", ""):
            issues.append({"kind": "state", "port": key, "device": "",
                           "count": 0, "last": None, "empty": True,
                           "state": port["state"]})
    return issues


def port_replug(port_key, confirm="", actor="local-ui", note=""):
    """端口级「软件拔插」：disable 写 1 → 等 1s → 写 0。

    默认关闭，需要在 config.json 里显式打开 allow_port_replug。
    只允许扩展坞的两个根口，避免误伤整条总线（usb3 上还有内置键盘/蓝牙）。
    """
    cfg = load_config()
    if not cfg.get("allow_port_replug"):
        return {"ok": False, "error": "该操作未启用（%s 里 allow_port_replug=false）" % CONFIG_PATH}
    if port_key not in PORT_REPLUG_ALLOWLIST:
        return {"ok": False, "error": "不在白名单内，拒绝执行：%s" % port_key}
    if confirm != port_key:
        return {"ok": False, "error": "需要二次确认：confirm 必须等于端口名 %s" % port_key,
                "need_confirm": port_key}

    root, pdir = _port_dir(port_key)
    disable = os.path.join(pdir, "disable")
    if not os.path.exists(disable):
        return {"ok": False, "error": "端口没有 disable 属性：%s" % pdir}
    if not os.access(disable, os.W_OK):
        return {"ok": False, "error": "没有写入权限（需要 root）"}

    before = {k: v for k, v in dock_info().items() if k == "ports"}
    before_ports = [p for p in before["ports"] if p["key"] == port_key]
    started = time.time()
    steps = []
    try:
        with open(disable, "w") as f:
            f.write("1\n")
        steps.append("disable=1")
        time.sleep(1.0)
        with open(disable, "w") as f:
            f.write("0\n")
        steps.append("disable=0")
    except OSError as e:
        return {"ok": False, "error": "写入失败：%s" % e, "steps": steps}

    deadline = time.time() + 8.0
    after_port = {}
    while time.time() < deadline:
        time.sleep(0.4)
        info = dock_info()
        cur = [p for p in info["ports"] if p["key"] == port_key]
        after_port = cur[0] if cur else {}
        if after_port.get("present") and after_port.get("state") in ("configured", "connected"):
            break

    detail = ("端口已重新连接，设备已重新枚举" if after_port.get("present")
              else "端口已复位，但 8 秒内没有观察到设备重新出现")
    entry = {
        "ts": _iso_now(), "epoch": time.time(), "action": "port_replug", "ok": True,
        "path": port_key, "vidpid": "", "product": PORT_REPLUG_ALLOWLIST[port_key],
        "level": "critical", "actor": actor or "local-ui", "note": note or "",
        "detail": detail, "before": before_ports, "after": after_port,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    _append_history(entry)
    return {"ok": True, "action": "port_replug", "path": port_key, "detail": detail,
            "steps": steps, "before": before_ports, "after": after_port,
            "history": entry}


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
