#!/usr/bin/env python3
"""
SmartSpotter  –  WSJT-X & DX Cluster Spot Bridge for FlexRadio
Version 3.1  (2026-06-10)

Forwards decoded spots from WSJT-X and any DX Spider cluster to FlexRadio
SmartSDR via the SmartSDR TCP API.  Compatible with FLEX-6000, FLEX-8000,
and Aurora series radios.  All modes supported.

Features:
  - Discovers FlexRadio SmartSDR radios via UDP broadcast
  - Manual IP/hostname entry with last-used memory
  - Settings dialog (callsign, filter mode, multicast address/port, colors, etc.)
  - Dark-themed activity log
  - Mini-mode: 300 × 60 compact strip in the SAME window (never gets lost)
  - File → Create Desktop Shortcut  (requires: pip install pywin32 winshell)
  - DX Cluster telnet client (DX Spider compatible) with auto-reconnect
  - Desktop icon (requires: pip install pillow  – falls back gracefully)
"""

import tkinter as tk
from tkinter import ttk, messagebox
import socket, struct, time, re, threading, json, os, sys
from pathlib import Path
import signal

# ── App metadata ───────────────────────────────────────────────────────────────
APP_NAME    = "SmartSpotter"
APP_VERSION = "3.1"

# ── Persistent config ──────────────────────────────────────────────────────────
CONFIG_DIR  = Path.home() / ".wsjtx_flex_bridge"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    # FlexRadio
    "flex_ip":        "",
    "flex_port":      4992,
    # WSJT-X multicast
    "mcast_grp":      "224.0.0.1",
    "mcast_port":     2237,
    # Operator
    "my_callsign":    "N0CALL",
    "filter_mode":    "cq",          # cq | pota | none
    "spot_lifetime":  120,
    "min_snr":        -35,
    "comment_ts":     True,
    # Colors
    "color_personal": "#FF0000",
    "color_pota":     "#00FF00",
    "color_dx":       "#00CCFF",
    # DX Cluster
    "dx_host":           "",
    "dx_port":           7300,
    "dx_callsign":       "",
    "dx_password":       "",
    "dx_enabled":        False,
    "dx_auto_reconnect": True,
    "dx_reconnect_delay": 30,        # seconds between reconnect attempts
    "dx_spot_lifetime":  300,
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
            cfg = dict(DEFAULTS)
            cfg.update(d)
            return cfg
        except Exception:
            pass
    return dict(DEFAULTS)


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── App icon ───────────────────────────────────────────────────────────────────
def _build_icon(size=32):
    """
    Draw a simple antenna/radio-wave icon and return (PhotoImage, ico_path|None).
    ico_path is written when Pillow is available – used by the desktop shortcut.
    """
    ico_path = None

    # ── Pillow path (proper ICO for taskbar / shortcut) ──────────────────────
    try:
        from PIL import Image, ImageDraw
        sizes = [16, 32, 48]
        images = []
        for s in sizes:
            img = Image.new("RGBA", (s, s), (26, 58, 120, 255))
            d   = ImageDraw.Draw(img)
            cx  = s // 2
            # Vertical mast
            d.rectangle([cx - 1, s // 4, cx + 1, s * 3 // 4], fill=(255, 255, 255, 255))
            # Cross-bar
            d.rectangle([s // 4, s // 4, s * 3 // 4, s // 4 + 1], fill=(255, 255, 255, 255))
            # Three arc-wave strokes (simplified as ellipse outlines)
            for r in (s // 6, s // 4, s // 3):
                d.arc([cx - r, s // 2 - r, cx + r, s // 2 + r],
                      start=200, end=340, fill=(0, 200, 255, 220), width=max(1, s // 20))
            images.append(img)

        ico_path = CONFIG_DIR / "wsjtx_bridge.ico"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        images[1].save(str(ico_path), format="ICO",
                       sizes=[(s, s) for s in sizes],
                       append_images=images[::2])

        # Build tkinter PhotoImage from 32x32
        import io
        buf = io.BytesIO()
        images[1].save(buf, format="PNG")
        buf.seek(0)
        photo = tk.PhotoImage(data=buf.read())
        return photo, ico_path

    except ImportError:
        pass

    # ── Fallback: pure tkinter PhotoImage ────────────────────────────────────
    img = tk.PhotoImage(width=32, height=32)
    for y in range(32):
        for x in range(32):
            img.put("#1a3a78", to=(x, y))
    # Mast
    for y in range(8, 24):
        img.put("#ffffff", to=(15, y))
        img.put("#ffffff", to=(16, y))
    # Cross-bar
    for x in range(8, 24):
        img.put("#ffffff", to=(x, 8))
    # Simple "wave" dots
    for x in range(4, 12):
        img.put("#00ccff", to=(x, 18))
    for x in range(20, 28):
        img.put("#00ccff", to=(x, 18))
    return img, ico_path


# ── WSJT-X QString parser (schema 2) ──────────────────────────────────────────
def parse_qstring(data, offset, buf_len):
    if offset + 4 > buf_len:
        return "[short]", offset
    length = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    if length == 0xFFFFFFFF:
        return "", offset
    if offset + length > buf_len:
        return "[trunc]", buf_len
    try:
        return data[offset : offset + length].decode("utf-8").rstrip("\x00"), offset + length
    except UnicodeDecodeError:
        return "[bad-utf8]", offset + length


# ── FlexRadio UDP Discovery ────────────────────────────────────────────────────
class FlexDiscovery(threading.Thread):
    PORT = 4992

    def __init__(self, callback):
        super().__init__(daemon=True, name="flex-discovery")
        self.callback = callback
        self._stop    = threading.Event()

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind(("", self.PORT))
        except Exception:
            return

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                text = data.decode("ascii", "ignore").strip()
                info = self._parse(text, addr[0])
                if info:
                    self.callback(info)
            except socket.timeout:
                continue
            except Exception:
                continue
        try:
            sock.close()
        except Exception:
            pass

    def _parse(self, text, src_ip):
        kv = {}
        for pair in text.split():
            if "=" in pair:
                k, _, v = pair.partition("=")
                kv[k.lower()] = v
        model = kv.get("model", "")
        if not kv or (not model and "ip" not in kv):
            return None
        return {
            "model":    model or "Unknown",
            "nickname": kv.get("nickname", kv.get("callsign", "")),
            "ip":       kv.get("ip", src_ip),
            "version":  kv.get("version", ""),
        }

    def stop(self):
        self._stop.set()


# ── DX Cluster Telnet Client ───────────────────────────────────────────────────
class DXClusterClient(threading.Thread):
    """
    Connects to a DX Spider (or compatible) cluster via raw TCP, handles the
    telnet IAC negotiation, logs in with the operator callsign, parses incoming
    DX spots, and fires on_spot() for each valid spot.

    Spot format parsed (flexible):
        DX de <spotter>:   <freq kHz>  <callsign>   <comment>   <HHMMz>
    """

    # Telnet control bytes
    IAC  = 255
    DONT = 254
    DO   = 253
    WONT = 252
    WILL = 251
    SB   = 250
    SE   = 240

    # DX Spider spot pattern – tolerant of varying whitespace
    _SPOT_RE = re.compile(
        r"DX\s+de\s+(\S+?):\s+"        # spotter callsign
        r"(\d+(?:\.\d+)?)\s+"          # frequency in kHz
        r"([A-Z0-9/]+)"                # spotted callsign
        r"(.*?)"                        # comment (optional)
        r"\s+(\d{4}Z)",                 # time
        re.IGNORECASE,
    )

    def __init__(self, cfg, on_spot, on_log, on_status):
        super().__init__(daemon=True, name="dx-cluster")
        self.cfg       = cfg
        self.on_spot   = on_spot
        self.on_log    = on_log
        self.on_status = on_status
        self._stop     = threading.Event()
        self._sock     = None
        self._buf      = b""

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run(self):
        while not self._stop.is_set():
            if not self.cfg.get("dx_enabled"):
                time.sleep(1)
                continue

            host = self.cfg.get("dx_host", "").strip()
            port = int(self.cfg.get("dx_port", 7300))
            if not host:
                time.sleep(2)
                continue

            self._connect_and_receive(host, port)

            if self._stop.is_set():
                break

            auto = self.cfg.get("dx_auto_reconnect", True)
            if not auto:
                self.on_status("dx", "Disconnected (auto-reconnect off)", "#FF4444")
                break

            delay = int(self.cfg.get("dx_reconnect_delay", 30))
            self.on_status("dx", f"Reconnecting in {delay}s…", "#FFAA00")
            for _ in range(delay * 10):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def _connect_and_receive(self, host, port):
        self.on_status("dx", f"Connecting to {host}:{port}…", "#FFAA00")
        self.on_log(f"DX Cluster: connecting to {host}:{port}", "system")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((host, port))
            s.settimeout(2.0)
            self._sock = s
            self._buf  = b""
        except Exception as e:
            self.on_log(f"DX Cluster connect error: {e}", "warn")
            self.on_status("dx", "Connection failed", "#FF4444")
            return

        self.on_status("dx", f"Connected – logging in…", "#FFAA00")
        logged_in   = False
        login_sent  = False
        passwd_sent = False

        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    self.on_log("DX Cluster: server closed connection", "warn")
                    break
                self._buf += chunk
            except socket.timeout:
                # Nothing received – check if we need to send login
                pass
            except Exception as e:
                self.on_log(f"DX Cluster recv error: {e}", "warn")
                break

            # Process telnet IAC sequences and extract text lines
            text, self._buf = self._strip_telnet(self._buf)
            lines = text.split("\n")
            # Last element may be partial; keep it in a local carry
            if lines:
                partial = lines.pop()
            else:
                partial = ""

            # Login prompts often arrive without a trailing newline (bare prompts).
            # Check the partial (incomplete) line as well as complete lines.
            if not login_sent:
                probe = (partial + " ".join(lines)).lower()
                if any(kw in probe for kw in ("login:", "call:", "enter your call")):
                    call = self.cfg.get("dx_callsign", "").strip()
                    if call:
                        self._send(call + "\r\n")
                        login_sent = True
                        lines = []   # consumed

            if login_sent and not passwd_sent:
                probe = (partial + " ".join(lines)).lower()
                if "password" in probe:
                    pw = self.cfg.get("dx_password", "").strip()
                    self._send((pw or "") + "\r\n")
                    passwd_sent = True
                    lines = []

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                low = line.lower()

                # Detect successful login (DX Spider sends its version / welcome)
                if not logged_in and ("dxspider" in low or "welcome" in low
                                      or "dx cluster" in low or ">>" in line):
                    logged_in = True
                    self.on_log("DX Cluster: logged in", "system")
                    self.on_status("dx", f"Connected – {host}:{port}", "#00AA00")

                # Parse DX spots
                spot = self._parse_spot(line)
                if spot:
                    if not logged_in:
                        logged_in = True
                        self.on_status("dx", f"Connected – {host}:{port}", "#00AA00")
                    self.on_spot(spot)
                else:
                    # Show cluster announcements in log (WCY, WWV, announce, etc.)
                    if line and not line.startswith("CC"):
                        self.on_log(f"DX: {line[:100]}", "info")

            # Re-attach partial line for next iteration
            self._buf = partial.encode("utf-8", "replace") + self._buf if partial else self._buf

        self._close()
        self.on_status("dx", "Disconnected", "#FF4444")

    def _strip_telnet(self, buf):
        """Remove IAC sequences, respond to WILL/DO with WONT/DONT, return plain text."""
        out   = bytearray()
        reply = bytearray()
        i     = 0
        while i < len(buf):
            b = buf[i]
            if b == self.IAC and i + 1 < len(buf):
                cmd = buf[i + 1]
                if cmd in (self.WILL, self.WONT, self.DO, self.DONT) and i + 2 < len(buf):
                    opt = buf[i + 2]
                    # Refuse all options
                    if cmd == self.WILL:
                        reply += bytes([self.IAC, self.DONT, opt])
                    elif cmd == self.DO:
                        reply += bytes([self.IAC, self.WONT, opt])
                    i += 3
                elif cmd == self.SB:
                    # Skip sub-negotiation until SE
                    j = i + 2
                    while j < len(buf) - 1:
                        if buf[j] == self.IAC and buf[j + 1] == self.SE:
                            j += 2
                            break
                        j += 1
                    i = j
                elif cmd == self.IAC:
                    out.append(self.IAC)
                    i += 2
                else:
                    i += 2  # skip unknown 2-byte sequence
            else:
                out.append(b)
                i += 1

        if reply and self._sock:
            try:
                self._sock.sendall(bytes(reply))
            except Exception:
                pass

        try:
            return out.decode("utf-8", "replace"), b""
        except Exception:
            return out.decode("latin-1", "replace"), b""

    def _parse_spot(self, line):
        """Parse a DX de … spot line. Returns spot dict or None."""
        m = self._SPOT_RE.search(line)
        if not m:
            return None
        spotter, freq_khz, callsign, comment, ztime = m.groups()
        try:
            freq_mhz = float(freq_khz) / 1000.0
        except ValueError:
            return None
        if not (1.0 < freq_mhz < 2000.0):
            return None
        comment = comment.strip()
        mode    = self._guess_mode(freq_mhz, comment)
        return {
            "callsign": callsign.upper(),
            "freq":     freq_mhz,
            "mode":     mode,
            "spotter":  spotter.upper(),
            "comment":  f"{comment}  de {spotter}  {ztime}".strip(),
            "source":   "DXCluster",
        }

    @staticmethod
    def _guess_mode(freq_mhz, comment):
        """Infer mode from comment keywords or frequency."""
        c = comment.upper()
        for kw in ("FT8", "FT4", "CW", "SSB", "PSK", "RTTY", "JS8", "DIGI"):
            if kw in c:
                return kw
        # Common FT8 sub-bands
        ft8_bands = [1.840, 3.573, 7.074, 10.136, 14.074, 18.100, 21.074,
                     24.915, 28.074, 50.313]
        for f in ft8_bands:
            if abs(freq_mhz - f) < 0.010:
                return "FT8"
        return "USB"

    def _send(self, text):
        try:
            self._sock.sendall(text.encode("ascii", "replace"))
        except Exception as e:
            self.on_log(f"DX Cluster send error: {e}", "warn")

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def stop(self):
        self._stop.set()
        self._close()


# ── WSJT-X UDP Multicast Listener ─────────────────────────────────────────────
class WSJTXListener(threading.Thread):
    MODIFIERS = {"POTA", "SOTA", "DX", "NA", "EU", "AS", "AF", "OC", "SA"}

    def __init__(self, cfg, on_decode, on_status, on_log):
        super().__init__(daemon=True, name="wsjtx-listener")
        self.cfg       = cfg
        self.on_decode = on_decode
        self.on_status = on_status
        self.on_log    = on_log
        self._stop     = threading.Event()
        self._dial_freq    = {}   # instance_id → Hz (keeps slices isolated)
        self._current_mode = {}   # instance_id → mode string

    def run(self):
        sock = None
        while not self._stop.is_set():
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.settimeout(1.0)
                    sock.bind(("", self.cfg["mcast_port"]))
                    mreq = struct.pack("4sl", socket.inet_aton(self.cfg["mcast_grp"]),
                                      socket.INADDR_ANY)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    self.on_log(
                        f"WSJT-X: listening {self.cfg['mcast_grp']}:{self.cfg['mcast_port']}",
                        "system",
                    )
                    self.on_status("wsjtx", "Listening", "#00AA00")
                except Exception as e:
                    self.on_log(f"WSJT-X bind error: {e}", "warn")
                    self.on_status("wsjtx", "Error", "#FF4444")
                    if sock:
                        try:
                            sock.close()
                        except Exception:
                            pass
                    sock = None
                    for _ in range(50):
                        if self._stop.is_set():
                            return
                        time.sleep(0.1)
                    continue

            try:
                data, _ = sock.recvfrom(1500)
                parsed = self._parse(data)
                if parsed and parsed["type"] == "decode":
                    self.on_decode(parsed)
            except socket.timeout:
                continue
            except Exception as e:
                self.on_log(f"WSJT-X recv error: {e}", "warn")
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None
                self.on_status("wsjtx", "Reconnecting", "#FFAA00")

        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _parse(self, data):
        buf_len = len(data)
        if buf_len < 20:
            return None
        offset = 0
        magic = struct.unpack_from(">I", data, offset)[0]; offset += 4
        if magic != 0xADBCCBDA:
            return None
        _schema   = struct.unpack_from(">I", data, offset)[0]; offset += 4
        msg_type  = struct.unpack_from(">I", data, offset)[0]; offset += 4
        inst_id, offset = parse_qstring(data, offset, buf_len)

        if msg_type == 1:  # Status
            dial_raw = struct.unpack_from(">Q", data, offset)[0]; offset += 8
            mode_str, offset = parse_qstring(data, offset, buf_len)
            self._dial_freq[inst_id] = dial_raw
            if mode_str and mode_str != "~":
                self._current_mode[inst_id] = mode_str.upper().strip()
            return {"type": "status"}

        if msg_type == 2:  # Decode
            _new    = struct.unpack_from(">?", data, offset)[0]; offset += 1
            _tms    = struct.unpack_from(">I", data, offset)[0]; offset += 4
            snr     = struct.unpack_from(">i", data, offset)[0]; offset += 4
            _dt     = struct.unpack_from(">d", data, offset)[0]; offset += 8
            df      = struct.unpack_from(">I", data, offset)[0]; offset += 4
            mode_str, offset = parse_qstring(data, offset, buf_len)
            message, offset  = parse_qstring(data, offset, buf_len)

            mode      = mode_str.upper().strip() if mode_str and mode_str != "~" else self._current_mode.get(inst_id, "FT8")
            parts     = re.split(r"\s+", message.strip())
            callsign  = None
            msg_upper = message.upper()

            if len(parts) >= 3:
                if parts[0].upper() == "CQ":
                    callsign = (parts[2]
                                if len(parts) >= 4 and parts[1].upper() in self.MODIFIERS
                                else parts[1])
                else:
                    callsign = parts[1] if re.match(r"^[A-Z0-9/]{3,15}$", parts[1]) else None
            if not callsign and parts:
                callsign = parts[0] if re.match(r"^[A-Z0-9/]{3,15}$", parts[0]) else None

            _dial     = self._dial_freq.get(inst_id, 0)
            freq_mhz  = (_dial + df) / 1e6 if _dial > 0 else 0.0
            my_call   = self.cfg.get("my_callsign", "").upper()
            min_snr   = self.cfg.get("min_snr", -35)
            filt      = self.cfg.get("filter_mode", "cq")
            ts        = time.strftime("%H:%M") if self.cfg.get("comment_ts") else ""

            if my_call and len(parts) >= 2:
                if my_call in {p.upper() for p in parts[1:]}:
                    return {
                        "type":     "decode",
                        "callsign": callsign or parts[0],
                        "freq":     freq_mhz,
                        "mode":     mode,
                        "comment":  f"{message} SNR {snr:+d} {ts}".strip(),
                        "color":    self.cfg.get("color_personal", "#FF0000"),
                        "label":    "CALLING YOU",
                        "source":   "WSJTX",
                    }

            if snr < min_snr:
                return None
            if filt == "cq" and not msg_upper.startswith("CQ "):
                return None
            if filt == "pota" and "CQ POTA" not in msg_upper:
                return None

            if callsign and len(callsign) >= 4 and 1 < freq_mhz < 1000:
                color = (self.cfg.get("color_pota", "#00FF00")
                         if "CQ POTA" in msg_upper else None)
                return {
                    "type":     "decode",
                    "callsign": callsign,
                    "freq":     freq_mhz,
                    "mode":     mode,
                    "comment":  f"{message} SNR {snr:+d} {ts}".strip(),
                    "color":    color,
                    "label":    "POTA" if "CQ POTA" in msg_upper else "Spot",
                    "source":   "WSJTX",
                }
        return None

    def stop(self):
        self._stop.set()


# ── FlexRadio TCP Connection ───────────────────────────────────────────────────
class FlexConnection:
    def __init__(self, cfg, on_log, on_status):
        self.cfg       = cfg
        self.on_log    = on_log
        self.on_status = on_status
        self._sock     = None
        self._lock     = threading.Lock()
        self._cmd_seq  = 0
        self._sent     = {}   # (callsign, freq_key) → timestamp

    def connect(self, ip):
        self.disconnect()
        port = self.cfg.get("flex_port", 4992)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((ip, port))
            welcome = s.recv(1024).decode("ascii", "ignore").strip()
            self.on_log(f"Flex: {welcome[:100]}", "system")
            s.sendall(f'C0|client program "{APP_NAME}"\n'.encode())
            s.recv(1024)
            self._sock    = s
            self._cmd_seq = 0
            self.on_status("flex", f"Connected ({ip})", "#00AA00")
            return True
        except Exception as e:
            self.on_log(f"Flex connect error: {e}", "warn")
            self.on_status("flex", "Not connected", "#FF4444")
            return False

    def disconnect(self):
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self.on_status("flex", "Not connected", "#FF4444")

    @property
    def connected(self):
        return self._sock is not None

    def send_spot(self, callsign, freq_mhz, mode, comment, color=None,
                  source="WSJTX", lifetime=None):
        if lifetime is None:
            lifetime = self.cfg.get("spot_lifetime", 120)
        now = time.time()
        key = (callsign.upper(), round(freq_mhz, 5))

        if key in self._sent and now - self._sent[key] < lifetime:
            self.on_log(f"Dup skipped: {callsign} @ {freq_mhz:.6f}", "info")
            return

        with self._lock:
            if not self._sock:
                self.on_log("Spot dropped – Flex not connected", "warn")
                return
            try:
                self._cmd_seq += 1
                parts = [
                    f"C{self._cmd_seq}|spot add",
                    f"rx_freq={freq_mhz:.6f}",
                    f"callsign={callsign}",
                    f"mode={mode}",
                    f"source={source}",
                    f"comment={comment}",
                    f"lifetime_seconds={lifetime}",
                ]
                if color:
                    parts.append(f"color={color}")
                self._sock.sendall((" ".join(parts) + "\n").encode())
                resp = self._sock.recv(4096).decode("ascii", "ignore").strip()
                if "R" in resp:
                    self._sent[key] = now
            except Exception as e:
                self.on_log(f"Flex send error: {e}", "warn")
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
                self.on_status("flex", "Disconnected", "#FF4444")


# ── Settings Dialog ────────────────────────────────────────────────────────────
class SettingsDialog(tk.Toplevel):
    WSJT_FIELDS = [
        ("My Callsign",            "my_callsign",    "str"),
        ("Filter Mode",            "filter_mode",    "choice:cq|pota|none"),
        ("WSJT-X Multicast Group", "mcast_grp",      "str"),
        ("WSJT-X UDP Port",        "mcast_port",     "int"),
        ("Flex TCP Port",          "flex_port",      "int"),
        ("Spot Lifetime (s)",      "spot_lifetime",  "int"),
        ("Min SNR",                "min_snr",        "int"),
        ("Timestamp in comment",   "comment_ts",     "bool"),
        ("Color – Calling Me",     "color_personal", "str"),
        ("Color – POTA",           "color_pota",     "str"),
    ]

    DX_FIELDS = [
        ("DX Cluster Host / FQDN", "dx_host",           "str"),
        ("Port",                   "dx_port",           "int"),
        ("Login Callsign",         "dx_callsign",       "str"),
        ("Password (optional)",    "dx_password",       "str"),
        ("DX Spot Lifetime (s)",   "dx_spot_lifetime",  "int"),
        ("Auto-reconnect",         "dx_auto_reconnect", "bool"),
        ("Reconnect Delay (s)",    "dx_reconnect_delay","int"),
        ("Color – DX Cluster",     "color_dx",          "str"),
    ]

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Settings")
        self.resizable(False, False)
        self.configure(bg="#2b2b2b")
        self.cfg    = dict(cfg)
        self.result = None
        self._build()
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def _build(self):
        BG, FG = "#2b2b2b", "#ffffff"
        pad = {"padx": 8, "pady": 3}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self._vars = {}
        for tab_label, fields in [("WSJT-X / Radio", self.WSJT_FIELDS),
                                   ("DX Cluster",     self.DX_FIELDS)]:
            tab = tk.Frame(nb, bg=BG, padx=8, pady=8)
            nb.add(tab, text=tab_label)
            for row, (label, key, typ) in enumerate(fields):
                tk.Label(tab, text=label + ":", bg=BG, fg="#aaaaaa",
                         anchor="e", width=26).grid(row=row, column=0, sticky="e", **pad)
                if typ.startswith("choice:"):
                    choices = typ.split(":")[1].split("|")
                    var = tk.StringVar(value=str(self.cfg.get(key, "")))
                    w = ttk.Combobox(tab, textvariable=var, values=choices,
                                     width=20, state="readonly")
                elif typ == "bool":
                    var = tk.BooleanVar(value=bool(self.cfg.get(key, False)))
                    w = tk.Checkbutton(tab, variable=var, bg=BG, fg=FG,
                                       selectcolor="#3a3a3a", activebackground=BG)
                else:
                    show = "*" if key == "dx_password" else ""
                    var  = tk.StringVar(value=str(self.cfg.get(key, "")))
                    w    = tk.Entry(tab, textvariable=var, width=22, show=show,
                                   bg="#3a3a3a", fg=FG, insertbackground=FG, relief="flat")
                w.grid(row=row, column=1, sticky="w", **pad)
                self._vars[key] = (var, typ)

        bf = tk.Frame(self, bg=BG)
        bf.pack(pady=(0, 8))
        tk.Button(bf, text="OK",     bg="#27ae60", fg=FG, relief="flat",
                  padx=16, command=self._ok).pack(side="left", padx=4)
        tk.Button(bf, text="Cancel", bg="#555555", fg=FG, relief="flat",
                  padx=12, command=self.destroy).pack(side="left", padx=4)

    def _ok(self):
        cfg = dict(self.cfg)
        for key, (var, typ) in self._vars.items():
            v = var.get()
            if typ == "int":
                try:
                    cfg[key] = int(v)
                except ValueError:
                    messagebox.showerror("Invalid value",
                                         f'"{v}" is not a valid integer for "{key}".',
                                         parent=self)
                    return
            elif typ == "bool":
                cfg[key] = bool(v)
            else:
                cfg[key] = v
        self.result = cfg
        self.destroy()


# ── Main Application ───────────────────────────────────────────────────────────
class App(tk.Tk):
    FULL_W, FULL_H = 580, 760
    MINI_W, MINI_H = 300,  62

    BG      = "#2b2b2b"
    BG_DARK = "#1c1c1c"
    FG      = "#ffffff"
    FG_DIM  = "#aaaaaa"
    ACCENT  = "#c0392b"
    GREEN   = "#27ae60"

    def __init__(self):
        super().__init__()
        self.cfg        = load_config()
        self.flex_conn  = None
        self.wsjtx_thr  = None
        self.disc_thr   = None
        self.dx_thr     = None
        self._mini      = False
        self._radios    = {}
        self._log_lines = []
        self._ico_path  = None

        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.resizable(False, False)
        self.geometry(f"{self.FULL_W}x{self.FULL_H}")
        self.configure(bg=self.BG)

        self._apply_style()
        self._set_icon()
        self._build_menu()
        self._build_ui()

        self.flex_conn = FlexConnection(
            self.cfg,
            on_log    = self._log,
            on_status = self._update_status,
        )
        self._start_wsjtx()
        self._start_discovery()
        self._start_dx_cluster()

        signal.signal(signal.SIGINT, lambda *_: self.after(0, self._on_close))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Style / Icon ──────────────────────────────────────────────────────────
    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        BG, FG = self.BG, self.FG
        style.configure(".",              background=BG, foreground=FG)
        style.configure("TFrame",         background=BG)
        style.configure("TLabel",         background=BG, foreground=FG)
        style.configure("TLabelframe",    background=BG, foreground=self.FG_DIM)
        style.configure("TLabelframe.Label",
                        background=BG, foreground=self.FG_DIM, font=("Segoe UI", 8))
        style.configure("TNotebook",      background=BG)
        style.configure("TNotebook.Tab",  background="#3a3a3a", foreground=FG,
                        padding=[8, 4])
        style.map("TNotebook.Tab",        background=[("selected", "#555555")])
        style.configure("Treeview",
                        background=self.BG_DARK, fieldbackground=self.BG_DARK,
                        foreground=FG, rowheight=22, font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background="#3a3a3a", foreground=FG, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1a5fa8")])
        style.configure("TScrollbar", background="#3a3a3a", troughcolor=self.BG_DARK)

    def _set_icon(self):
        try:
            photo, ico_path = _build_icon(32)
            self._icon_img  = photo
            self._ico_path  = ico_path
            self.iconphoto(True, photo)
            if ico_path and sys.platform == "win32":
                try:
                    self.iconbitmap(str(ico_path))
                except Exception:
                    pass
        except Exception:
            pass

    # ── Menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self):
        MB_BG, MB_AB = "#3a3a3a", "#555555"
        mb = tk.Menu(self, bg=MB_BG, fg=self.FG, activebackground=MB_AB,
                     activeforeground=self.FG, relief="flat")
        self.config(menu=mb)

        def sub():
            return tk.Menu(mb, tearoff=0, bg=MB_BG, fg=self.FG,
                           activebackground=MB_AB, activeforeground=self.FG)

        file_m = sub()
        file_m.add_command(label="Create Desktop Shortcut", command=self._create_shortcut)
        file_m.add_separator()
        file_m.add_command(label="Exit", command=self._on_close)
        mb.add_cascade(label="File", menu=file_m)

        settings_m = sub()
        settings_m.add_command(label="Settings…", command=self._open_settings)
        mb.add_cascade(label="Settings", menu=settings_m)

        help_m = sub()
        help_m.add_command(label=f"About {APP_NAME}", command=self._about)
        mb.add_cascade(label="Help", menu=help_m)

    # ── Main UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        BG, FG, DIM = self.BG, self.FG, self.FG_DIM

        self._full_frame = tk.Frame(self, bg=BG)
        self._full_frame.pack(fill="both", expand=True, padx=10, pady=8)

        # ── Discovered Radios ─────────────────────────────────────────────────
        rf = ttk.LabelFrame(self._full_frame, text="Discovered Radios", padding=(6, 4))
        rf.pack(fill="x", pady=(0, 6))

        cols = ("Model", "Name / Callsign", "IP Address", "Firmware")
        self.radio_tree = ttk.Treeview(rf, columns=cols, show="headings",
                                       height=3, selectmode="browse")
        for col, w in zip(cols, (90, 130, 145, 100)):
            self.radio_tree.heading(col, text=col)
            self.radio_tree.column(col, width=w, anchor="w")
        self.radio_tree.pack(fill="x")
        self.radio_tree.bind("<<TreeviewSelect>>", self._on_radio_select)

        scan_bar = tk.Frame(rf, bg=BG)
        scan_bar.pack(fill="x", pady=(4, 0))
        self._radio_count_var = tk.StringVar(value="Scanning for radios…")
        tk.Label(scan_bar, textvariable=self._radio_count_var,
                 bg=BG, fg=DIM, font=("Segoe UI", 8)).pack(side="left")
        self._make_btn(scan_bar, "Scan", self._rescan, bg="#3a3a3a").pack(side="right")

        # ── FlexRadio Connect ─────────────────────────────────────────────────
        cf = ttk.LabelFrame(self._full_frame, text="FlexRadio Connect", padding=(6, 6))
        cf.pack(fill="x", pady=(0, 6))

        ip_row = tk.Frame(cf, bg=BG)
        ip_row.pack(fill="x")
        tk.Label(ip_row, text="Host / IP:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        self._ip_var = tk.StringVar(value=self.cfg.get("flex_ip", ""))
        tk.Entry(ip_row, textvariable=self._ip_var, bg="#3a3a3a", fg=FG,
                 insertbackground=FG, relief="flat", font=("Segoe UI", 9),
                 width=22).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._conn_btn = tk.Button(ip_row, text="Connect",
                                   bg=self.GREEN, fg=FG, relief="flat",
                                   font=("Segoe UI", 9, "bold"), padx=10,
                                   command=self._toggle_connect)
        self._conn_btn.pack(side="right")

        # ── DX Cluster Connect ────────────────────────────────────────────────
        dxf = ttk.LabelFrame(self._full_frame, text="DX Cluster", padding=(6, 6))
        dxf.pack(fill="x", pady=(0, 6))

        dx_row1 = tk.Frame(dxf, bg=BG)
        dx_row1.pack(fill="x")

        tk.Label(dx_row1, text="Host:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self._dx_host_var = tk.StringVar(value=self.cfg.get("dx_host", ""))
        tk.Entry(dx_row1, textvariable=self._dx_host_var, bg="#3a3a3a", fg=FG,
                 insertbackground=FG, relief="flat", font=("Segoe UI", 9),
                 width=20).pack(side="left", padx=(0, 6))

        tk.Label(dx_row1, text="Port:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self._dx_port_var = tk.StringVar(value=str(self.cfg.get("dx_port", 7300)))
        tk.Entry(dx_row1, textvariable=self._dx_port_var, bg="#3a3a3a", fg=FG,
                 insertbackground=FG, relief="flat", font=("Segoe UI", 9),
                 width=6).pack(side="left", padx=(0, 6))

        dx_row2 = tk.Frame(dxf, bg=BG)
        dx_row2.pack(fill="x", pady=(4, 0))

        tk.Label(dx_row2, text="Callsign:", bg=BG, fg=FG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self._dx_call_var = tk.StringVar(value=self.cfg.get("dx_callsign", ""))
        tk.Entry(dx_row2, textvariable=self._dx_call_var, bg="#3a3a3a", fg=FG,
                 insertbackground=FG, relief="flat", font=("Segoe UI", 9),
                 width=12).pack(side="left", padx=(0, 10))

        self._dx_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("dx_enabled", False)))
        tk.Checkbutton(dx_row2, text="Enable", variable=self._dx_enabled_var,
                       bg=BG, fg=FG, selectcolor="#3a3a3a", activebackground=BG,
                       font=("Segoe UI", 9),
                       command=self._on_dx_enable_toggle).pack(side="left", padx=(0, 8))

        self._dx_conn_btn = tk.Button(dx_row2, text="Connect DX",
                                      bg=self.GREEN, fg=FG, relief="flat",
                                      font=("Segoe UI", 9, "bold"), padx=10,
                                      command=self._toggle_dx_connect)
        self._dx_conn_btn.pack(side="right")

        # ── Connection Status ─────────────────────────────────────────────────
        sf = ttk.LabelFrame(self._full_frame, text="Connection Status", padding=(6, 4))
        sf.pack(fill="x", pady=(0, 6))
        self._flex_sv,  self._flex_lbl  = self._status_row(sf, "Radio:")
        self._wsjtx_sv, self._wsjtx_lbl = self._status_row(sf, "WSJT-X:")
        self._dx_sv,    self._dx_lbl    = self._status_row(sf, "DX Cluster:")

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = tk.Frame(self._full_frame, bg=BG)
        tb.pack(fill="x", pady=(0, 4))
        self._mini_btn = self._make_btn(tb, "⬇  Mini View", self._toggle_mini, bg="#3a3a3a")
        self._mini_btn.pack(side="right")
        self._make_btn(tb, "🔗  Desktop Shortcut", self._create_shortcut,
                       bg="#3a3a3a").pack(side="left")

        # ── Activity Log ──────────────────────────────────────────────────────
        lf = ttk.LabelFrame(self._full_frame, text="Activity Log", padding=(4, 4))
        lf.pack(fill="both", expand=True)
        log_wrap = tk.Frame(lf, bg=self.BG_DARK)
        log_wrap.pack(fill="both", expand=True)
        self._log_text = tk.Text(
            log_wrap, height=14, bg=self.BG_DARK, fg="#cccccc",
            font=("Consolas", 8), state="disabled", wrap="none",
            relief="flat", bd=0,
        )
        self._log_text.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(log_wrap, command=self._log_text.yview)
        vsb.pack(side="right", fill="y")
        self._log_text.config(yscrollcommand=vsb.set)
        for tag, fg in [("info",   "#cccccc"), ("spot",   "#00cc44"),
                        ("dx",     "#00ccff"), ("call",   "#ff4444"),
                        ("warn",   "#ffaa00"), ("system", "#4488ff")]:
            self._log_text.tag_configure(tag, foreground=fg)

        # ── Mini-mode strip ───────────────────────────────────────────────────
        self._mini_frame = tk.Frame(self, bg=self.BG_DARK)

        mini_hdr = tk.Frame(self._mini_frame, bg="#3a3a3a", height=18)
        mini_hdr.pack(fill="x")
        mini_hdr.pack_propagate(False)
        tk.Label(mini_hdr, text=f"  {APP_NAME}  v{APP_VERSION}",
                 bg="#3a3a3a", fg="#888888",
                 font=("Segoe UI", 7)).pack(side="left")
        for sym, cmd in [("✕", self._on_close), ("▲", self._toggle_mini)]:
            b = tk.Label(mini_hdr, text=sym, bg="#3a3a3a", fg="#aaaaaa",
                         font=("Segoe UI", 9), cursor="hand2", padx=6)
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())

        self._mini_log = tk.Text(
            self._mini_frame, height=2, bg=self.BG_DARK, fg="#00cc44",
            font=("Consolas", 7), state="disabled", wrap="none",
            relief="flat", bd=0,
        )
        self._mini_log.pack(fill="both", expand=True, padx=3, pady=(1, 2))

    # ── Mini toggle ───────────────────────────────────────────────────────────
    def _toggle_mini(self):
        if not self._mini:
            self._mini = True
            self._full_frame.pack_forget()
            self._mini_log.config(state="normal")
            self._mini_log.delete("1.0", "end")
            for line in self._log_lines[-6:]:
                self._mini_log.insert("end", line + "\n")
            self._mini_log.see("end")
            self._mini_log.config(state="disabled")
            self._mini_frame.pack(fill="both", expand=True)
            self.geometry(f"{self.MINI_W}x{self.MINI_H}")
        else:
            self._mini = False
            self._mini_frame.pack_forget()
            self._full_frame.pack(fill="both", expand=True, padx=10, pady=8)
            self.geometry(f"{self.FULL_W}x{self.FULL_H}")

    def _mini_append(self, line):
        ml = self._mini_log
        ml.config(state="normal")
        ml.insert("end", line + "\n")
        ml.see("end")
        cnt = int(ml.index("end-1c").split(".")[0])
        if cnt > 40:
            ml.delete("1.0", f"{cnt - 40}.0")
        ml.config(state="disabled")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _status_row(self, parent, label):
        row = tk.Frame(parent, bg=self.BG)
        row.pack(fill="x", pady=1)
        tk.Label(row, text=label, bg=self.BG, fg=self.FG_DIM,
                 width=12, anchor="e", font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        var = tk.StringVar(value="Not connected")
        lbl = tk.Label(row, textvariable=var, bg=self.BG, fg="#FF4444",
                       anchor="w", font=("Segoe UI", 9, "bold"))
        lbl.pack(side="left")
        return var, lbl

    def _make_btn(self, parent, text, cmd, bg="#555555"):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=self.FG,
                         relief="flat", font=("Segoe UI", 8), padx=8, pady=2,
                         cursor="hand2", activebackground="#666666",
                         activeforeground=self.FG)

    # ── Thread management ─────────────────────────────────────────────────────
    def _start_wsjtx(self):
        self.wsjtx_thr = WSJTXListener(
            self.cfg,
            on_decode = self._on_decode,
            on_status = self._update_status,
            on_log    = self._log,
        )
        self.wsjtx_thr.start()

    def _start_discovery(self):
        self.disc_thr = FlexDiscovery(self._on_radio_found)
        self.disc_thr.start()

    def _start_dx_cluster(self):
        if self.dx_thr and self.dx_thr.is_alive():
            self.dx_thr.stop()
        self.dx_thr = DXClusterClient(
            self.cfg,
            on_spot   = self._on_dx_spot,
            on_log    = self._log,
            on_status = self._update_status,
        )
        self.dx_thr.start()

    def _rescan(self):
        self._radios.clear()
        for item in self.radio_tree.get_children():
            self.radio_tree.delete(item)
        self._radio_count_var.set("Scanning for radios…")
        if self.disc_thr and self.disc_thr.is_alive():
            self.disc_thr.stop()
        self._start_discovery()

    # ── Thread → GUI callbacks ────────────────────────────────────────────────
    def _on_radio_found(self, info):
        self.after(0, self._gui_add_radio, info)

    def _on_decode(self, spot):
        self.after(0, self._gui_send_spot, spot)

    def _on_dx_spot(self, spot):
        self.after(0, self._gui_send_dx_spot, spot)

    def _log(self, msg, tag="info"):
        self.after(0, self._gui_log, msg, tag)

    def _update_status(self, which, text, color):
        self.after(0, self._gui_status, which, text, color)

    # ── GUI-thread updates ────────────────────────────────────────────────────
    def _gui_add_radio(self, info):
        ip = info.get("ip", "")
        if not ip or ip in self._radios:
            return
        self._radios[ip] = info
        self.radio_tree.insert("", "end", iid=ip, values=(
            info.get("model", ""), info.get("nickname", ""),
            ip, info.get("version", ""),
        ))
        n = len(self._radios)
        self._radio_count_var.set(f"{n} radio{'s' if n != 1 else ''} found")

    def _gui_send_spot(self, spot):
        if not self.flex_conn.connected:
            return
        label = spot.get("label", "Spot")
        tag   = "call" if label == "CALLING YOU" else "spot"
        self._gui_log(
            f"{label}: {spot['callsign']}  {spot['freq']:.6f} MHz  [{spot['mode']}]",
            tag,
        )
        threading.Thread(
            target=self.flex_conn.send_spot,
            args=(spot["callsign"], spot["freq"], spot["mode"], spot["comment"]),
            kwargs={"color": spot.get("color"), "source": "WSJTX"},
            daemon=True,
        ).start()

    def _gui_send_dx_spot(self, spot):
        color = self.cfg.get("color_dx", "#00CCFF")
        lifetime = int(self.cfg.get("dx_spot_lifetime", 300))
        self._gui_log(
            f"DX: {spot['callsign']}  {spot['freq']:.3f} MHz  [{spot['mode']}]"
            f"  de {spot.get('spotter', '?')}",
            "dx",
        )
        if self.flex_conn.connected:
            threading.Thread(
                target=self.flex_conn.send_spot,
                args=(spot["callsign"], spot["freq"], spot["mode"], spot["comment"]),
                kwargs={"color": color, "source": "DXCluster", "lifetime": lifetime},
                daemon=True,
            ).start()

    def _gui_log(self, msg, tag="info"):
        ts   = time.strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > 600:
            self._log_lines = self._log_lines[-600:]

        t = self._log_text
        t.config(state="normal")
        t.insert("end", line + "\n", tag)
        t.see("end")
        cnt = int(t.index("end-1c").split(".")[0])
        if cnt > 400:
            t.delete("1.0", f"{cnt - 400}.0")
        t.config(state="disabled")

        if self._mini:
            self._mini_append(line)

    def _gui_status(self, which, text, color):
        if which == "flex":
            self._flex_sv.set(text);  self._flex_lbl.config(fg=color)
        elif which == "wsjtx":
            self._wsjtx_sv.set(text); self._wsjtx_lbl.config(fg=color)
        elif which == "dx":
            self._dx_sv.set(text);    self._dx_lbl.config(fg=color)

    # ── UI actions ────────────────────────────────────────────────────────────
    def _on_radio_select(self, _event):
        sel = self.radio_tree.selection()
        if sel:
            self._ip_var.set(sel[0])

    def _toggle_connect(self):
        if self.flex_conn.connected:
            self.flex_conn.disconnect()
            self._conn_btn.config(text="Connect", bg=self.GREEN)
        else:
            ip = self._ip_var.get().strip()
            if not ip:
                messagebox.showwarning("No address",
                                       "Enter the radio's IP address or select one above.")
                return
            self.cfg["flex_ip"] = ip
            save_config(self.cfg)
            self._conn_btn.config(text="Connecting…", bg="#555555", state="disabled")
            self.update()

            def _do():
                ok = self.flex_conn.connect(ip)
                self.after(0, self._post_connect, ok)

            threading.Thread(target=_do, daemon=True).start()

    def _post_connect(self, ok):
        self._conn_btn.config(state="normal")
        self._conn_btn.config(
            text="Disconnect" if ok else "Connect",
            bg=self.ACCENT if ok else self.GREEN,
        )

    def _on_dx_enable_toggle(self):
        """Sync the Enable checkbox → cfg and restart the DX thread."""
        enabled = self._dx_enabled_var.get()
        self.cfg["dx_enabled"] = enabled
        self._sync_dx_fields_to_cfg()
        save_config(self.cfg)
        if enabled:
            self._start_dx_cluster()
        else:
            self._update_status("dx", "Disabled", "#888888")

    def _toggle_dx_connect(self):
        """Manually trigger a DX cluster connect/restart."""
        self._sync_dx_fields_to_cfg()
        self.cfg["dx_enabled"] = True
        self._dx_enabled_var.set(True)
        save_config(self.cfg)
        self._start_dx_cluster()

    def _sync_dx_fields_to_cfg(self):
        """Pull inline DX Cluster fields into cfg."""
        self.cfg["dx_host"]     = self._dx_host_var.get().strip()
        self.cfg["dx_callsign"] = self._dx_call_var.get().strip()
        try:
            self.cfg["dx_port"] = int(self._dx_port_var.get())
        except ValueError:
            pass

    def _open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.result:
            self.cfg.update(dlg.result)
            save_config(self.cfg)
            # Sync inline DX fields back to UI from saved cfg
            self._dx_host_var.set(self.cfg.get("dx_host", ""))
            self._dx_port_var.set(str(self.cfg.get("dx_port", 7300)))
            self._dx_call_var.set(self.cfg.get("dx_callsign", ""))
            self._dx_enabled_var.set(bool(self.cfg.get("dx_enabled", False)))
            # Restart threads with new settings
            if self.wsjtx_thr:
                self.wsjtx_thr.stop()
            self.flex_conn.cfg = self.cfg
            self._start_wsjtx()
            self._start_dx_cluster()
            self._log("Settings updated", "system")

    def _create_shortcut(self):
        try:
            import winshell
            from win32com.client import Dispatch
        except ImportError:
            messagebox.showinfo(
                "Missing packages",
                "Install the required packages first:\n\n"
                "    pip install pywin32 winshell\n\n"
                "Then use File → Create Desktop Shortcut again.",
            )
            return
        try:
            script  = os.path.abspath(sys.argv[0])
            lnk     = os.path.join(winshell.desktop(), f"{APP_NAME}.lnk")
            shell   = Dispatch("WScript.Shell")
            sc      = shell.CreateShortCut(lnk)
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            sc.Targetpath       = str(pythonw if pythonw.exists() else sys.executable)
            sc.Arguments        = f'"{script}"'
            sc.WorkingDirectory = os.path.dirname(script)
            sc.Description      = f"{APP_NAME} v{APP_VERSION}"
            # Use the generated ICO for the shortcut icon
            if self._ico_path and self._ico_path.exists():
                sc.IconLocation = f"{self._ico_path},0"
            sc.save()
            messagebox.showinfo("Shortcut created",
                                f"Desktop shortcut created:\n{lnk}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not create shortcut:\n{e}")

    def _about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME}  v{APP_VERSION}\n\n"
            "Forwards WSJT-X decoded spots and DX Cluster spots to\n"
            "FlexRadio SmartSDR via the SmartSDR TCP API.\n\n"
            "DX Cluster: telnet to any DX Spider node.\n"
            "Install Pillow for a proper desktop icon:\n"
            "    pip install pillow\n\n"
            f"Config: {CONFIG_FILE}",
        )

    def _on_close(self):
        save_config(self.cfg)
        if self.wsjtx_thr:
            self.wsjtx_thr.stop()
        if self.disc_thr:
            self.disc_thr.stop()
        if self.dx_thr:
            self.dx_thr.stop()
        if self.flex_conn:
            self.flex_conn.disconnect()
        try:
            self.destroy()
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass
    try:
        app = App()
        app.mainloop()
    except KeyboardInterrupt:
        pass
