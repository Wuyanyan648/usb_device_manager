/* USB 实物拓扑：把 /api/status 的 rows（sysfs 真实层级）画成设备本身的样子。
 * 控制器 → 根口 → hub → 子设备，线缆上标端口号，节点角上标速率。
 * 纯前端渲染，不写死任何设备；无数据时显示空态。 */
(function () {
  "use strict";

  const NODE_W = 200, NODE_H = 58, ROW = 72, PAD = 22;
  const HOST_W = 140;
  const XS = [290, 550, 810, 1070, 1330];

  /* VID:PID 直接判定（优先于关键词），未收录的走关键词/驱动/接口类 */
  const TYPE_MAP = {
    vidpid: {
      "046D:C547": "receiver", "046D:C52B": "receiver", "046D:C534": "receiver",
      "0BDA:8156": "ethernet", "0BDA:8153": "ethernet", "0BDA:8152": "ethernet",
      "0BDA:8812": "wifi", "0BDA:B812": "wifi", "0BDA:C820": "wifi",
      "8087:0033": "bt", "8087:0029": "bt", "8087:0AAA": "bt",
      "41E4:2120": "keyboard", "05AC:0250": "keyboard",
      "0951:1666": "storage", "0781:5581": "storage", "090C:1000": "storage",
      "046D:0825": "camera", "0BDA:5846": "camera",
      "0B0E:0305": "audio", "046D:0A9C": "audio",
      "067B:2303": "serial", "1A86:7523": "serial", "10C4:EA60": "serial",
      "05E3:0610": "hub", "05E3:0608": "hub", "05E3:0625": "hub",
      "2109:2813": "hub", "2109:0813": "hub",
    },
    keywords: {
      "键盘": "keyboard", keyboard: "keyboard", "keychron": "keyboard",
      "接收器": "receiver", receiver: "receiver", unifying: "receiver", bolt: "receiver",
      "鼠标": "mouse", mouse: "mouse",
      "无线网卡": "wifi", wifi: "wifi", wireless: "wifi", wlan: "wifi", "802.11": "wifi",
      "蓝牙": "bt", bluetooth: "bt",
      "有线网卡": "ethernet", ethernet: "ethernet", lan: "ethernet", "rj45": "ethernet",
      "存储": "storage", storage: "storage", "flash": "storage", "ssd": "storage", "disk": "storage",
      "摄像头": "camera", camera: "camera", webcam: "camera", uvc: "camera",
      "耳机": "audio", headset: "audio", audio: "audio", speaker: "audio",
      "手柄": "gamepad", gamepad: "gamepad", joystick: "gamepad",
      "打印机": "printer", printer: "printer",
      "串口": "serial", serial: "serial",
      hub: "hub",
    },
  };
  window.USB_TYPE_MAP = TYPE_MAP;

  const TYPE_LABEL = {
    hub: "Hub", keyboard: "键盘", mouse: "鼠标", receiver: "无线接收器",
    ethernet: "有线网卡", wifi: "无线网卡", bt: "蓝牙", storage: "存储",
    camera: "摄像头", audio: "音频", printer: "打印机", serial: "串口",
    gamepad: "手柄", phone: "手机", hid: "HID 设备", unknown: "USB 设备",
    controller: "控制器", host: "本机", ghost: "未识别",
  };

  const TYPE_TINT = {
    hub: "#7c8aa0", keyboard: "#8b93a7", mouse: "#8b93a7", receiver: "#8b93a7",
    ethernet: "#e0b34d", wifi: "#8a6cff", bt: "#41d1c4", storage: "#5b8cff",
    camera: "#ff8f6b", audio: "#ff7fae", printer: "#9aa4b8", serial: "#69c46a",
    gamepad: "#5b8cff", phone: "#8e99ad", hid: "#8b93a7", unknown: "#6f7a8d",
    controller: "#5b8cff", host: "#8a6cff", ghost: "#ff6b6b",
  };

  const esc = (s) => String(s == null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function clip(s, n) {
    s = String(s == null ? "" : s).trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  /* 按显示宽度截断：中日韩字符算 2 个单位，避免长名字撑出节点 */
  function clipUnits(s, maxUnits) {
    s = String(s == null ? "" : s).trim();
    let used = 0, out = "";
    for (const ch of s) {
      const w = /[\u2e80-\u9fff\uff00-\uffef]/.test(ch) ? 2 : 1;
      if (used + w > maxUnits) return out + "…";
      out += ch; used += w;
    }
    return out;
  }

  function speedText(dev) {
    const raw = Number(dev && dev.speed);
    if (!raw) return "";
    if (raw >= 20000) return "20G";
    if (raw >= 10000) return "10G";
    if (raw >= 5000) return "5G";
    if (raw >= 480) return "480M";
    if (raw >= 12) return "12M";
    return "1.5M";
  }

  /* ---------- 类型识别：hub / 描述符 / 厂商关键词 / 驱动 / 接口类 ---------- */

  function driverSet(dev) {
    const out = new Set();
    (dev.drivers || []).forEach((d) => out.add(String(d).toLowerCase()));
    (dev.interfaces || []).forEach((i) => i && i.driver && out.add(String(i.driver).toLowerCase()));
    return out;
  }

  function typeOf(dev) {
    if (!dev) return "unknown";
    if (Number(dev.bDeviceClass) === 9 || dev.is_hub) return "hub";
    const vp = String(dev.vidpid || "").toUpperCase();
    if (TYPE_MAP.vidpid[vp]) return TYPE_MAP.vidpid[vp];
    const text = [dev.name, dev.product, dev.product_name, dev.manufacturer, dev.vendor_name]
      .filter(Boolean).join(" ").toLowerCase();
    const table = TYPE_MAP.keywords;
    for (const key of Object.keys(table)) {
      if (text.includes(String(key).toLowerCase())) return table[key];
    }
    const drivers = driverSet(dev);
    if (["r8152", "ax88179_178a", "cdc_ether", "rndis_host", "asix"].some((d) => drivers.has(d))) return "ethernet";
    if (["rtw88_8821cu", "rtl8xxxu", "mt76x0u", "mt7601u", "ath9k_htc", "brcmfmac"].some((d) => drivers.has(d))) return "wifi";
    if (drivers.has("btusb")) return "bt";
    if (["usb-storage", "uas", "usb_storage"].some((d) => drivers.has(d))) return "storage";
    if (drivers.has("uvcvideo")) return "camera";
    if (drivers.has("snd-usb-audio")) return "audio";
    if (drivers.has("usbhid")) return String(text).includes("mouse") ? "mouse" : "hid";
    if (Number(dev.bDeviceClass) === 8) return "storage";
    return "unknown";
  }

  /* ---------- 造型：每种设备一个 56×40 的小图形 ---------- */

  function glyph(type, cx, cy) {
    const t = (s) => `<g transform="translate(${cx},${cy})">${s}</g>`;
    switch (type) {
      case "hub":
        return t(`
          <rect x="-27" y="-13" width="54" height="26" rx="6" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".55"/>
          <rect x="-24" y="-10.5" width="48" height="6" rx="2.5" fill="#fff" opacity=".14"/>
          <rect x="-21" y="2" width="8" height="7" rx="1.5" fill="#0b0f17" stroke="#46536a" stroke-width=".8"/>
          <rect x="-10" y="2" width="8" height="7" rx="1.5" fill="#0b0f17" stroke="#46536a" stroke-width=".8"/>
          <rect x="1" y="2" width="8" height="7" rx="1.5" fill="#0b0f17" stroke="#46536a" stroke-width=".8"/>
          <rect x="12" y="2" width="8" height="7" rx="1.5" fill="#0b0f17" stroke="#46536a" stroke-width=".8"/>
          <circle cx="23" cy="-6" r="1.7" fill="#2fd6a3"/>`);
      case "keyboard":
        return t(`
          <rect x="-28" y="-11" width="56" height="24" rx="3.5" fill="url(#pDark)" stroke="#0b0f17" stroke-opacity=".6"/>
          <rect x="-27" y="-10" width="54" height="5" rx="2" fill="#fff" opacity=".09"/>
          ${[0, 1, 2].map((r) => {
            let keys = "";
            for (let i = 0; i < 9; i++) {
              const w = (r === 2 && i === 4) ? 12 : 5;
              const x = -25 + i * 5.6;
              keys += `<rect x="${x}" y="${-3 + r * 5}" width="${w}" height="4" rx="1" fill="#3d4759"/>`;
            }
            return keys;
          }).join("")}`);
      case "receiver":
      case "mouse":
        return t(`
          <rect x="-16" y="-7" width="24" height="15" rx="3" fill="url(#pDark)" stroke="#0b0f17" stroke-opacity=".6"/>
          <rect x="7" y="-4.5" width="11" height="10" rx="1.5" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".35"/>
          <rect x="-11" y="0" width="7" height="3" rx="1" fill="#fff" opacity=".18"/>
          <circle cx="-13" cy="-3" r="1.3" fill="#2fd6a3"/>`);
      case "ethernet":
        return t(`
          <path d="M -22 -9 h20 l9 5 v9 h-29 z" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".45"/>
          <rect x="7" y="-6" width="6" height="12" rx="1" fill="#c8a24a"/>
          ${[0, 1, 2, 3].map((i) => `<rect x="${9}" y="${-4.5 + i * 3}" width="4" height="1.3" fill="#ffe9a8"/>`).join("")}
          <path d="M -22 2 q -8 12 -18 16" fill="none" stroke="#4d5a70" stroke-width="4" stroke-linecap="round"/>
          <rect x="-22" y="-6" width="4" height="12" rx="1" fill="#fff" opacity=".2"/>`);
      case "wifi":
        return t(`
          <rect x="-19" y="-5" width="30" height="15" rx="3" fill="url(#pDark)" stroke="#0b0f17" stroke-opacity=".6"/>
          <path d="M 14 -2 a 10 10 0 0 1 0 13" fill="none" stroke="#8a6cff" stroke-width="2.4" stroke-linecap="round"/>
          <path d="M 19 -6 a 15 15 0 0 1 0 21" fill="none" stroke="#8a6cff" stroke-width="2" stroke-linecap="round" opacity=".55"/>
          <rect x="1" y="-1" width="8" height="7" rx="1" fill="#fff" opacity=".14"/>`);
      case "bt":
        return t(`
          <rect x="-13" y="-11" width="26" height="22" rx="4" fill="url(#pChip)" stroke="#0b0f17" stroke-opacity=".5"/>
          <path d="M0 -7 v14 l6 -4 -12 -6" fill="none" stroke="#41d1c4" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          ${[-8, -3, 3, 8].map((y) => `<rect x="-17" y="${y}" width="4" height="1.6" fill="#8e99ad"/>`).join("")}`);
      case "storage":
        return t(`
          <rect x="-26" y="-13" width="52" height="26" rx="4" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".45"/>
          <rect x="-23" y="-10" width="46" height="9" rx="2" fill="#fff" opacity=".10"/>
          <rect x="-23" y="-1" width="34" height="2" fill="#0b0f17" opacity=".35"/>
          <circle cx="19" cy="6" r="2.1" fill="#5b8cff"/>
          <path d="M 27 0 q 9 6 2 14" fill="none" stroke="#4d5a70" stroke-width="3.5" stroke-linecap="round"/>`);
      case "camera":
        return t(`
          <rect x="-22" y="-11" width="44" height="22" rx="5" fill="url(#pDark)" stroke="#0b0f17" stroke-opacity=".55"/>
          <circle cx="0" cy="0" r="9" fill="url(#pMetal)"/>
          <circle cx="0" cy="0" r="5.5" fill="#0b0f17"/>
          <circle cx="-1.6" cy="-1.8" r="1.8" fill="#fff" opacity=".55"/>
          <circle cx="16" cy="-6" r="1.5" fill="#2fd6a3"/>`);
      case "audio":
        return t(`
          <path d="M -14 2 v-3 a 14 14 0 0 1 28 0 v3" fill="none" stroke="#ff7fae" stroke-width="3" stroke-linecap="round"/>
          <rect x="-18" y="0" width="8" height="13" rx="3" fill="url(#pDark)"/>
          <rect x="10" y="0" width="8" height="13" rx="3" fill="url(#pDark)"/>`);
      case "controller":
        return t(`
          <rect x="-24" y="-12" width="48" height="24" rx="3" fill="url(#pChip)" stroke="#0b0f17" stroke-opacity=".5"/>
          <rect x="-18" y="-6" width="20" height="12" rx="2" fill="#5b8cff" opacity=".22"/>
          <text x="2" y="4" font-size="9" fill="#8e99ad" font-family="monospace">USB</text>
          ${[-8, -3, 2, 7].map((y) => `<rect x="-29" y="${y}" width="5" height="2" fill="#8e99ad"/><rect x="24" y="${y}" width="5" height="2" fill="#8e99ad"/>`).join("")}`);
      case "host":
        return t(`
          <rect x="-25" y="-15" width="50" height="24" rx="3" fill="url(#pDark)" stroke="#0b0f17" stroke-opacity=".55"/>
          <rect x="-21" y="-11" width="42" height="16" rx="2" fill="#5b8cff" opacity=".18"/>
          <path d="M -30 11 h60 l-4 6 h-52 z" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".4"/>
          <rect x="-14" y="11" width="28" height="2" fill="#0b0f17" opacity=".3"/>`);
      case "ghost":
        return t(`
          <rect x="-22" y="-10" width="44" height="20" rx="4" fill="none" stroke="#ff6b6b"
                stroke-width="1.6" stroke-dasharray="4 3"/>
          <text x="0" y="4" font-size="11" fill="#ff6b6b" text-anchor="middle">?</text>`);
      default:
        return t(`
          <rect x="-20" y="-9" width="30" height="17" rx="2.5" fill="url(#pMetal)" stroke="#0b0f17" stroke-opacity=".4"/>
          <rect x="9" y="-6" width="9" height="11" rx="1" fill="#3d4759"/>
          ${[0, 1, 2, 3].map((i) => `<rect x="-16" y="${-6 + i * 3.4}" width="10" height="1.6" rx=".8" fill="#fff" opacity=".28"/>`).join("")}`);
    }
  }

  /* ---------- 建树：严格按 rows 的 path 推导父子关系 ---------- */

  function parentOf(path) {
    if (/^usb\d+$/.test(path)) return "host";
    if (path.includes(".")) return path.slice(0, path.lastIndexOf("."));
    const m = String(path).match(/^(\d+)-/);
    return m ? "usb" + m[1] : "host";
  }

  function build(rows, opts) {
    const nodes = new Map();
    const portOfChild = new Map();
    (rows || []).forEach((row) => {
      if (row.kind === "port" && row.port && row.port.device) portOfChild.set(row.port.device, row.port);
    });
    (rows || []).forEach((row) => {
      if (row.kind !== "device" && row.kind !== "root") return;
      const dev = row.device || {};
      const node = {
        path: row.path,
        dev,
        depth: Number(row.depth) || 0,
        type: row.kind === "root" ? "controller" : typeOf(dev),
        port: portOfChild.get(row.path) || null,
        children: [],
        ghost: false,
      };
      nodes.set(row.path, node);
    });
    // 缺失设备：boot_diff.missing 与未识别的坞端口 → 画成虚线幽灵节点
    const ghostDepth = (p) => (String(p).includes(".") ? String(p).split(".").length : 1);
    (opts && opts.missing || []).forEach((path) => {
      if (!path || nodes.has(path)) return;
      nodes.set(path, {
        path, dev: { name: "开机时存在、现在缺失" }, depth: ghostDepth(path), type: "ghost",
        port: null, children: [], ghost: true,
      });
    });
    (opts && opts.ghosts || []).forEach((g) => {
      if (!g || !g.path || nodes.has(g.path)) return;
      nodes.set(g.path, {
        path: g.path, dev: { name: g.name || "未识别" }, depth: ghostDepth(g.path), type: "ghost",
        port: null, children: [], ghost: true, note: g.note || "",
      });
    });
    const root = { path: "host", type: "host", children: [], depth: -1, dev: { name: "本机" } };
    nodes.forEach((node) => {
      const p = parentOf(node.path);
      const parent = p === "host" ? root : nodes.get(p) || root;
      node.parent = parent.path;
      parent.children.push(node);
    });
    return { root, nodes };
  }

  function sortChildren(node) {
    node.children.sort((a, b) => {
      const pa = a.port ? Number(a.port.port) : 99;
      const pb = b.port ? Number(b.port.port) : 99;
      if (pa !== pb) return pa - pb;
      return String(a.path).localeCompare(String(b.path), "en", { numeric: true });
    });
    node.children.forEach(sortChildren);
  }

  function layout(root) {
    root.children.sort((a, b) => Number(String(a.path).replace("usb", "")) - Number(String(b.path).replace("usb", "")));
    let cursor = PAD + NODE_H / 2;
    const walk = (node) => {
      node.children.forEach(sortChildren);
      if (!node.children.length) {
        node.y = cursor;
        cursor += ROW;
        return;
      }
      node.children.forEach(walk);
      node.y = (node.children[0].y + node.children[node.children.length - 1].y) / 2;
    };
    root.children.forEach((c, i) => {
      if (i) cursor += 14;
      walk(c);
    });
    const maxDepth = (n) => (n.children.length ? 1 + Math.max(...n.children.map(maxDepth)) : 0);
    const depth = Math.max(...root.children.map(maxDepth), 1);
    const width = XS[Math.min(depth, XS.length - 1)] + NODE_W + PAD;
    root.y = root.children.length
      ? (root.children[0].y + root.children[root.children.length - 1].y) / 2
      : cursor / 2;
    return { width, height: Math.max(cursor + PAD, 160) };
  }

  function xOf(node) {
    const d = node.path === "host" ? -1 : Math.min(node.depth, XS.length - 1);
    return d < 0 ? PAD : XS[d];
  }

  function cable(x1, y1, x2, y2, on) {
    const mx = (x1 + x2) / 2;
    const color = on ? "#4d5a70" : "#5c3a3a";
    return `<path d="M ${x1} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"
      fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round"/>
      <path d="M ${x1} ${y1 - 1.2} C ${mx} ${y1 - 1.2} ${mx} ${y2 - 1.2} ${x2} ${y2 - 1.2}"
      fill="none" stroke="#ffffff" stroke-opacity=".14" stroke-width="1.4" stroke-linecap="round"/>`;
  }

  function nodeBox(node, x, y) {
    const w = node.path === "host" ? HOST_W : NODE_W;
    const h = NODE_H;
    const dev = node.dev || {};
    const type = node.type;
    const tint = TYPE_TINT[type] || "#6f7a8d";
    const name = type === "controller"
      ? `USB${dev.busnum ?? ""} 根 Hub`
      : clipUnits(dev.name || dev.product_name || dev.product || TYPE_LABEL[type] || "未知设备", 15);
    const speed = speedText(dev);
    const subBits = type === "ghost" ? ["未枚举"] : [speed, TYPE_LABEL[type]].filter(Boolean);
    if (node.port) subBits.push("端口 " + node.port.port);
    if (type === "controller" && dev.maxchild) subBits.push(dev.maxchild + " 口");
    const off = Number(dev.authorized) === 0;
    const bad = !node.ghost && dev.status && dev.status.code && !["ok", "hub", "root"].includes(dev.status.code);
    const cls = ["phys-node", node.ghost ? "ghost" : "", off ? "off" : "", bad ? "bad" : ""].join(" ");
    return `
      <g class="${cls}" data-path="${esc(node.path)}" data-type="${esc(type)}">
        <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12"
          fill="var(--panel-2)" stroke="${node.ghost ? "#ff6b6b" : "var(--border)"}"
          stroke-width="1" ${node.ghost ? 'stroke-dasharray="5 4"' : ""}/>
        <rect x="${x}" y="${y}" width="4" height="${h}" rx="2" fill="${tint}" opacity=".85"/>
        ${glyph(type, x + 34, y + h / 2)}
        <text x="${x + 66}" y="${y + 24}" font-size="12.5" fill="var(--text)" font-weight="600">${esc(name)}</text>
        <text x="${x + 66}" y="${y + 40}" font-size="10.5" fill="var(--muted)">${esc(clipUnits(subBits.join(" · "), 21))}</text>
        ${off ? `<text x="${x + w - 8}" y="${y + 14}" font-size="10" fill="#ff6b6b" text-anchor="end">关闭</text>` : ""}
        ${bad ? `<circle cx="${x + w - 10}" cy="${y + 10}" r="3" fill="#f5b84b"/>` : ""}
        ${node.ghost ? `<text x="${x + w - 8}" y="${y + 14}" font-size="10" fill="#ff6b6b" text-anchor="end">缺失</text>` : ""}
      </g>`;
  }

  function portTag(node, x1, y1) {
    if (!node.port) return "";
    // 贴着父节点右边缘放，避免和子节点/另一条线缆撞在一起
    const tx = x1 + 12, ty = y1 - 9;
    return `<g class="phys-port">
      <rect x="${tx}" y="${ty - 8}" width="26" height="15" rx="7" fill="var(--panel)" stroke="var(--border)"/>
      <text x="${tx + 13}" y="${ty + 3}" font-size="9.5" fill="var(--muted)" text-anchor="middle">P${esc(node.port.port)}</text>
    </g>`;
  }

  function renderSvg(rows, opts) {
    const { root, nodes } = build(rows || [], opts || {});
    const size = layout(root);
    const parts = [];
    const report = [];
    const walk = (node) => {
      const x = xOf(node);
      node.children.forEach((child) => {
        const cx = xOf(child);
        const y1 = node.y, y2 = child.y;
        const right = x + (node.path === "host" ? HOST_W : NODE_W);
        parts.push(cable(right, y1, cx, y2, !child.ghost));
        parts.push(portTag(child, right, y1));
        walk(child);
      });
      parts.push(nodeBox(node, x, node.y - NODE_H / 2));
      report.push({
        path: node.path, type: node.type, parent: node.parent || null,
        port: node.port ? Number(node.port.port) : null,
        depth: node.depth, ghost: !!node.ghost,
      });
    };
    walk(root);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size.width}" height="${size.height}"
      viewBox="0 0 ${size.width} ${size.height}" role="img" aria-label="USB 实物拓扑">
      <defs>
        <linearGradient id="pMetal" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#c6cfdb"/><stop offset="1" stop-color="#8c97a8"/>
        </linearGradient>
        <linearGradient id="pDark" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#3a4456"/><stop offset="1" stop-color="#232b3a"/>
        </linearGradient>
        <linearGradient id="pChip" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#2f3a4d"/><stop offset="1" stop-color="#1d2532"/>
        </linearGradient>
      </defs>
      ${parts.join("\n")}
    </svg>`;
    return { svg, report, size, nodes };
  }

  function render(host, rows, opts) {
    if (!host) return null;
    if (!rows || !rows.length) {
      host.innerHTML = '<div class="phys-empty">没有读到 USB 设备</div>';
      return null;
    }
    const out = renderSvg(rows, opts);
    host.innerHTML = out.svg;
    window.__physMapReport = { nodes: out.report, size: out.size };
    return out;
  }

  window.USBPhysMap = { render, typeOf, speedText };
})();
