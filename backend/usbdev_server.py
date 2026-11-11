#!/usr/bin/env python3
"""USB 设备管理服务（127.0.0.1:8795）。

只暴露两件事：
- 只读：拓扑、端口状态、端点/带宽估算、autosuspend、journal 里的枚举/掉线/复位历史；
- 唯一的写操作：按设备开关 USB 通信链路（authorized 0/1）。

authorized 只有 root 可写，所以本服务按系统级单元运行（与 usbnicd 一致）。
除 authorized 外不写任何 sysfs；操作记录追加到 /var/lib/usbdevd/history.jsonl。

只用 Python 标准库。
"""

import collections
import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import usbdev_core as core

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(ROOT), "web")
DEFAULT_PORT = 8795
_LOCK = threading.Lock()
_UDEV = {"started": False, "events": collections.deque(maxlen=300), "error": ""}


class _UdevWatcher(threading.Thread):
    """轻量监听 udev 的 usb 子系统 add/remove，补上内核日志里看不到的插拔。"""

    daemon = True

    def run(self):
        if not os.path.exists("/usr/bin/udevadm") and not os.path.exists("/bin/udevadm"):
            _UDEV["error"] = "udevadm 不可用"
            return
        try:
            proc = subprocess.Popen(
                ["udevadm", "monitor", "--udev", "--subsystem-match=usb", "--property"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError as e:
            _UDEV["error"] = str(e)
            return
        _UDEV["started"] = True
        item = {}
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("KERNEL[") or line.startswith("UDEV  ["):
                if item:
                    _push(item)
                head = line.split("] ", 1)
                rest = head[1].split() if len(head) > 1 else []
                item = {"ts": core._iso_now(),
                        "action": rest[0] if rest else "",
                        "devpath": rest[1] if len(rest) > 1 else ""}
            elif "=" in line and item is not None:
                k, _, v = line.partition("=")
                if k == "DEVPATH":
                    item["devpath"] = v
                elif k == "DEVNAME":
                    item["devname"] = v
                elif k == "ID_VENDOR_ID":
                    item["vid"] = v.upper()
                elif k == "ID_MODEL_ID":
                    item["pid"] = v.upper()
                elif k == "PRODUCT":
                    item["product"] = v
        if item:
            _push(item)


def _push(item):
    path = item.get("devpath", "")
    if not path:
        return
    name = path.rsplit("/", 1)[-1]
    if ":" in name:
        name = name.split(":")[0]
    item["path"] = name
    if _UDEV["events"] and _UDEV["events"][-1].get("devpath") == path \
            and _UDEV["events"][-1].get("action") == item.get("action"):
        return
    _UDEV["events"].append(item)


def start_watcher():
    if _UDEV["started"]:
        return
    _UdevWatcher().start()


def state_payload(with_journal=True):
    snap = core.snapshot()
    snap["history"] = core.read_history(limit=80)
    snap["protection_policy"] = {
        "engine": "authorized 0/1（逻辑链路开关）",
        "levels": {
            "blocked": "禁止操作（根 hub）",
            "critical": "关键设备：必须二次确认（confirm 填设备路径）",
            "protected": "受保护：必须二次确认",
            "free": "可直接操作",
        },
    }
    snap["udev"] = {"started": _UDEV["started"], "error": _UDEV["error"],
                    "events": list(_UDEV["events"])[-60:]}
    if with_journal:
        events = core.scan_journal()
        snap["journal"] = events[-400:]
        snap["issues"] = core.port_issues(snap["ports"], events)
        snap["boot_diff"] = core.take_snapshot()
    return snap


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _guard(self):
        """只允许本机页面发起写操作：自定义头 + Origin 白名单。"""
        if self.headers.get("X-USBDev-Client") != "usbdev-ui":
            self._json({"ok": False, "error": "缺少 X-USBDev-Client 头"}, 403)
            return False
        origin = self.headers.get("Origin") or ""
        if origin:
            host = origin.split("//", 1)[-1].split(":")[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                self._json({"ok": False, "error": "不允许的来源：%s" % origin}, 403)
                return False
        return True

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/link", "/api/refresh", "/api/port-replug"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._guard():
            return
        body = self._read_json()
        if path == "/api/refresh":
            core.scan_journal(force=True)
            self._json(state_payload())
            return

        if path == "/api/port-replug":
            port_key = str(body.get("port") or "").strip()
            confirm = str(body.get("confirm") or "")
            actor = str(body.get("actor") or "portal")[:64]
            note = str(body.get("note") or "")[:200]
            with _LOCK:
                result = core.port_replug(port_key, confirm=confirm, actor=actor, note=note)
            print("[usbdevd] port-replug %s -> %s" % (port_key, "ok" if result.get("ok")
                                                      else result.get("error")), file=sys.stderr)
            result["state"] = state_payload(with_journal=False)
            self._json(result)
            return

        target = str(body.get("path") or "").strip()
        enable = bool(body.get("enable"))
        confirm = str(body.get("confirm") or "")
        actor = str(body.get("actor") or "portal")[:64]
        note = str(body.get("note") or "")[:200]
        if not target:
            self._json({"ok": False, "error": "缺少 path"}, 400)
            return
        with _LOCK:
            try:
                result = core.set_link(target, enable, confirm=confirm,
                                       actor=actor, note=note)
            except Exception as e:  # noqa: BLE001 —— 失败必须回给前端
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
                return
        print("[usbdevd] %s %s -> %s" % ("on" if enable else "off", target,
                                         "ok" if result.get("ok") else result.get("error")),
              file=sys.stderr)
        if result.get("ok"):
            result["state"] = state_payload(with_journal=False)
        self._json(result)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            self._json({"ok": True, "service": "usb-device-manager",
                        "root": os.geteuid() == 0})
            return
        if path in ("/api/state", "/api/status"):
            try:
                self._json(state_payload())
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
            return
        if path == "/api/history":
            limit = 100
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("limit="):
                        try:
                            limit = max(1, min(1000, int(kv[6:])))
                        except ValueError:
                            pass
            self._json(core.read_history(limit=limit))
            return
        if path == "/api/journal":
            self._json({"ok": True, "events": core.scan_journal()[-400:]})
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/", "/usb-devices.html"):
            self.path = "/usb-devices.html"
        super().do_GET()


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print_tree(snap):
    for row in snap["rows"]:
        indent = "  " * row["depth"]
        if row["kind"] == "port":
            p = row["port"]
            flag = "空" if p["empty"] else (p["device"] or "")
            print("%s[端口 %-3s] %-12s disable=%s oc=%d %s"
                  % (indent, p["port"], p["state"], p["disable"],
                     p["over_current_count"], flag))
            continue
        d = row["device"]
        prot = d["protection"]["level"]
        mark = {"blocked": "[禁止]", "critical": "[关键]",
                "protected": "[保护]", "free": "      "}[prot]
        print("%s%s %-14s %-10s %-9s %-10s auth=%s %-6s %s"
              % (indent, mark, d["path"], d["vidpid"] or "-",
                 (d["speed"] + "M") if d["speed"] else "-",
                 d["status"]["label"], d["authorized"],
                 d["power_control"] or "-", d["name"]))


def _print_protect(snap):
    for item in snap["protected"]:
        print("[%s] %-12s %-10s %s" % (item["level"], item["path"],
                                       item["vidpid"] or "-", item["name"]))
        for r in item["reasons"]:
            print("    - %s" % r)


def main(argv):
    if "--tree" in argv:
        _print_tree(core.snapshot())
        return 0
    if "--protect" in argv:
        _print_protect(core.snapshot())
        return 0
    if "--ports" in argv:
        for p in core.snapshot()["ports"]:
            print("%-14s state=%-14s connect=%-10s disable=%s oc=%d %s"
                  % (p["path"], p["state"], p["connect_type"], p["disable"],
                     p["over_current_count"], p["device"] or "（空）"))
        return 0
    if "--state" in argv:
        print(json.dumps(core.snapshot(), ensure_ascii=False, indent=2))
        return 0
    if "--check" in argv:
        cap = core.capabilities()
        print(json.dumps(cap, ensure_ascii=False, indent=2))
        return 0
    if "--history" in argv:
        res = core.read_history(limit=200)
        for e in res["entries"]:
            print("%s %-8s %-12s %-10s %s %s"
                  % (e["ts"], e["action"], e["path"], e["vidpid"],
                     "ok" if e["ok"] else "fail", e["detail"]))
        return 0
    if "--journal" in argv:
        for e in core.scan_journal()[-80:]:
            print("%s %-12s %-10s %-12s %s" % (e["ts"], e["kind"], e["path"],
                                               e["detail"], e["label"]))
        return 0
    if "--dock" in argv:
        info = core.dock_info()
        for p in info["ports"]:
            print("%-12s state=%-12s disable=%s 设备=%s 可写=%s  %s"
                  % (p["key"], p["state"], p["disable"], p["device"] or "（无）",
                     p["writable"], p["label"]))
        print("手动端口重连已启用：%s（配置：%s）" % (info["allow_port_replug"], info["config_path"]))
        return 0
    if "--boot-guard" in argv:
        dry = "--dry-run" in argv
        res = core.run_boot_guard(dry_run=dry)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("result") not in ("failed",) else 1
    if "--guard-status" in argv:
        print(json.dumps(core.read_boot_guard(), ensure_ascii=False, indent=2))
        return 0
    if "--replug-port" in argv:
        i = argv.index("--replug-port")
        if i + 1 >= len(argv):
            print("用法: --replug-port usb3-port1 --yes", file=sys.stderr)
            return 2
        port_key = argv[i + 1]
        res = core.port_replug(port_key, confirm=port_key if "--yes" in argv else "",
                               actor="cli")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if "--link-off" in argv or "--link-on" in argv:
        key = "--link-off" if "--link-off" in argv else "--link-on"
        i = argv.index(key)
        if i + 1 >= len(argv):
            print("用法: %s <设备路径，如 4-2.2>" % key, file=sys.stderr)
            return 2
        path = argv[i + 1]
        enable = key == "--link-on"
        confirm = path if "--yes" in argv else ""
        res = core.set_link(path, enable, confirm=confirm, actor="cli")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
            continue
        if a == "--no-browser":
            open_browser = False
        i += 1

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print("端口 %d 无法监听（%s）。可能已有实例，用 `ss -lntp | grep %d` 确认。"
              % (port, e, port), file=sys.stderr)
        return 3

    start_watcher()
    diff = core.take_snapshot()
    if diff["missing"] and not diff["same_boot"]:
        missing = "、".join("%s(%s)" % (d["path"], d["vidpid"]) for d in diff["missing"])
        print("[usbdevd] 与上次启动相比缺少设备：%s" % missing, file=sys.stderr)

    url = "http://127.0.0.1:%d/" % port
    print("USB 设备管理: %s（pid=%d，euid=%d）" % (url, os.getpid(), os.geteuid()))
    print("操作记录: %s" % core.HISTORY_PATH)
    if os.geteuid() != 0:
        print("警告：非 root 运行，只能查看，无法开关链路。", file=sys.stderr)
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
