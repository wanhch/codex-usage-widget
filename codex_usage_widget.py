import base64
import ctypes
import hashlib
import json
import math
import os
import pathlib
import queue
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import Menu


TARGET_URL = "https://chatgpt.com/codex/cloud/settings/analytics#usage"
if getattr(sys, "frozen", False):
    APP_DIR = pathlib.Path(sys.executable).resolve().parent
else:
    APP_DIR = pathlib.Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "edge-profile"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "widget.log"
CAPTURE_PATH = APP_DIR / "last_capture.txt"

DEFAULT_CONFIG = {
    "poll_seconds": 30,
    "remote_debugging_port": 39225,
    "refresh_page_each_poll": False,
    "reload_wait_seconds": 4,
    "browser_mode": "visible",
    "minimize_edge_after_data": True,
    "close_edge_on_exit": True,
    "system_poll_seconds": 1,
    "widget_width": 286,
    "widget_height": 78,
    "widget_x": None,
    "widget_y": None,
}


EXTRACT_JS = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : "";
  const lines = bodyText.split(/\n+/).map(s => s.trim()).filter(Boolean);
  const hit = /(codex|usage|limit|5\s*[- ]?\s*h(?:our)?s?|weekly|week|remaining|reset|用量|限额|限制|小时|小時|一周|每周|每週|周|週)/i;
  const picked = [];
  for (let i = 0; i < lines.length; i++) {
    if (!hit.test(lines[i])) continue;
    for (let j = Math.max(0, i - 3); j < Math.min(lines.length, i + 7); j++) {
      picked.push(lines[j]);
    }
    picked.push("---");
  }
  const uniq = [];
  const seen = new Set();
  for (const line of picked) {
    const key = line.slice(0, 260);
    if (!seen.has(key)) {
      seen.add(key);
      uniq.push(key);
    }
  }
  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    text: bodyText.slice(0, 120000),
    focusedText: uniq.slice(0, 260).join("\n"),
    capturedAt: new Date().toISOString()
  };
})();
"""


def log(message):
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as exc:
            log(f"config load failed: {exc}")
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"config save failed: {exc}")


def find_edge():
    candidates = [
        pathlib.Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pathlib.Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    found = shutil.which("msedge")
    if found:
        candidates.insert(0, pathlib.Path(found))
    for item in candidates:
        if item and item.exists():
            return str(item)
    raise RuntimeError("Microsoft Edge was not found.")


def http_json(port, path, method="GET", timeout=3):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def is_cdp_ready(port):
    try:
        data = http_json(port, "/json/version", timeout=1)
        return "webSocketDebuggerUrl" in data or "Browser" in data
    except Exception:
        return False


def port_is_open(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def find_free_port(start):
    for port in list(range(start, start + 40)) + list(range(41000, 41100)):
        if not port_is_open(port):
            return port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def launch_edge(port, mode="hidden"):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    edge = find_edge()
    mode = (mode or "hidden").lower()
    args = [
        edge,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-features=Translate,MediaRouter,AutofillServerCommunication",
        "--metrics-recording-only",
        "--window-size=1100,760",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ]
    if mode == "visible":
        args.append(f"--app={TARGET_URL}")
    elif mode == "headless":
        args.extend([
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            TARGET_URL,
        ])
    else:
        args.extend([
            "--start-minimized",
            "--window-position=-32000,-32000",
            f"--app={TARGET_URL}",
        ])
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def ensure_browser(cfg):
    port = int(cfg.get("remote_debugging_port") or DEFAULT_CONFIG["remote_debugging_port"])
    mode = (cfg.get("browser_mode") or DEFAULT_CONFIG["browser_mode"]).lower()
    if not is_cdp_ready(port):
        if port_is_open(port):
            port = find_free_port(port + 1)
            cfg["remote_debugging_port"] = port
            save_config(cfg)
        launch_edge(port, mode=mode)
    deadline = time.time() + 18
    while time.time() < deadline:
        if is_cdp_ready(port):
            if mode == "visible":
                try:
                    focus_visible_collector(port)
                except Exception as exc:
                    log(f"focus visible collector failed: {exc}")
            elif mode == "hidden":
                minimize_collector(port)
            return port
        time.sleep(0.4)
    raise RuntimeError("Edge DevTools endpoint did not become ready.")


def ensure_target(port):
    targets = []
    try:
        targets = http_json(port, "/json/list", timeout=2)
    except Exception:
        pass
    for target in targets:
        url = target.get("url", "")
        if target.get("type") == "page" and "chatgpt.com/codex/cloud/settings/analytics" in url:
            return target
    encoded = urllib.parse.quote(TARGET_URL, safe="")
    for method in ("PUT", "GET"):
        try:
            target = http_json(port, f"/json/new?{encoded}", method=method, timeout=3)
            if isinstance(target, dict) and target.get("webSocketDebuggerUrl"):
                return target
        except Exception:
            pass
    try:
        version = http_json(port, "/json/version", timeout=2)
        ws_url = version.get("webSocketDebuggerUrl")
        if ws_url:
            with DevToolsClient(ws_url, timeout=4) as cdp:
                cdp.call("Target.createTarget", {"url": TARGET_URL}, timeout=4)
    except Exception as exc:
        log(f"create target failed: {exc}")
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            targets = http_json(port, "/json/list", timeout=2)
            for target in targets:
                url = target.get("url", "")
                if target.get("type") == "page" and "chatgpt.com/codex/cloud/settings/analytics" in url:
                    return target
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Could not open the Codex analytics page.")


class DevToolsClient:
    def __init__(self, ws_url, timeout=10):
        self.ws_url = ws_url
        self.timeout = timeout
        self.sock = None
        self.next_id = 1

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self):
        parsed = urllib.parse.urlparse(self.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.sendall(request)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                break
            header += chunk
            if len(header) > 16384:
                break
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            sock.close()
            raise RuntimeError("WebSocket upgrade failed.")
        self.sock = sock

    def close(self):
        if not self.sock:
            return
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def call(self, method, params=None, timeout=10):
        msg_id = self.next_id
        self.next_id += 1
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send_text(json.dumps(payload, separators=(",", ":")))
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            self.sock.settimeout(remaining)
            message = self._recv_text()
            if not message:
                continue
            data = json.loads(message)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data
        raise TimeoutError(f"CDP call timed out: {method}")

    def _send_text(self, text):
        payload = text.encode("utf-8")
        frame = bytearray()
        frame.append(0x81)
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", length)
        mask = os.urandom(4)
        frame += mask
        frame += bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(frame)

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("WebSocket closed.")
            data += chunk
        return data

    def _recv_text(self):
        chunks = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by remote.")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8", "replace")

    def _send_pong(self, payload):
        frame = bytearray([0x8A])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        else:
            return
        mask = os.urandom(4)
        frame += mask
        frame += bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(frame)


def normalize_lines(text):
    return [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]


def line_matches(line, patterns):
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns)


def build_block(lines, patterns, stop_patterns=None):
    best = None
    best_score = -1
    for i, line in enumerate(lines):
        if not line_matches(line, patterns):
            continue
        start = i
        end = min(len(lines), i + 9)
        if stop_patterns:
            for j in range(i + 1, end):
                if line_matches(lines[j], stop_patterns):
                    end = j
                    break
        block_lines = lines[start:end]
        blob = "\n".join(block_lines)
        score = 10
        score += len(re.findall(r"\d", blob))
        score += 4 if re.search(r"(usage|limit|used|remaining|reset|用量|限额|限制|剩余|重置)", blob, re.I) else 0
        score -= 2 if len(blob) > 900 else 0
        if score > best_score:
            best_score = score
            best = block_lines
    return best or []


def extract_usage(block_lines, title):
    block = "\n".join(block_lines)
    compact = " ".join(block_lines)
    result = {
        "title": title,
        "value": "Waiting",
        "detail": "No matching usage text yet",
        "percent": None,
        "reset_text": "",
        "reset_fraction": None,
        "raw": block[:1200],
    }
    if not block_lines:
        return result

    fraction_patterns = [
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?![\d.])",
        r"(?<![\d.])(\d+(?:\.\d+)?)\s+(?:of|out of)\s+(\d+(?:\.\d+)?)(?![\d.])",
        r"(?:used|已用|使用)\D{0,24}(\d+(?:\.\d+)?)\D{0,12}(?:of|/|out of)\D{0,12}(\d+(?:\.\d+)?)",
    ]
    for pattern in fraction_patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if not match:
            continue
        used = float(match.group(1))
        limit = float(match.group(2))
        if limit <= 0:
            continue
        pct = max(0.0, min(100.0, used / limit * 100.0))
        result["value"] = f"{format_number(used)} / {format_number(limit)}"
        result["percent"] = pct
        result["detail"] = find_detail_line(block_lines)
        enrich_usage_result(result, title)
        return result

    percent_match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", compact)
    if percent_match:
        pct = max(0.0, min(100.0, float(percent_match.group(1))))
        result["value"] = f"{format_number(pct)}% used"
        result["percent"] = pct
        result["detail"] = find_detail_line(block_lines)
        enrich_usage_result(result, title)
        return result

    numeric_lines = [
        line for line in block_lines
        if re.search(r"\d", line) and not re.fullmatch(r"5\s*(?:h|hours?|小时|小時)?", line, re.I)
    ]
    if numeric_lines:
        result["value"] = trim_text(numeric_lines[0], 30)
        result["detail"] = trim_text(find_detail_line(block_lines), 58)
    else:
        useful = [line for line in block_lines if not line_matches(line, [r"^codex$", r"^usage$"])]
        result["value"] = trim_text(useful[0] if useful else block_lines[0], 30)
        result["detail"] = trim_text(find_detail_line(block_lines), 58)
    enrich_usage_result(result, title)
    return result


def format_number(value):
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def trim_text(text, max_len):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)].rstrip() + "..."


def find_detail_line(lines):
    for pattern in [
        r"(reset|resets|renews|重置|恢复)",
        r"(remaining|left|available|剩余|可用)",
        r"(limit|usage|used|限额|限制|用量|已用)",
    ]:
        for line in lines:
            if re.search(pattern, line, re.IGNORECASE):
                return line
    return lines[0] if lines else ""


def enrich_usage_result(result, title):
    reset_text, reset_fraction = parse_reset_info(result.get("detail", ""), title)
    result["reset_text"] = reset_text
    result["reset_fraction"] = reset_fraction


def parse_reset_info(text, title):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return "", None
    lower = text.lower()
    total_minutes = 5 * 60 if title == "5h" else 7 * 24 * 60
    minutes = parse_duration_minutes(lower)
    if minutes is None:
        minutes = parse_weekday_minutes(lower)
    if minutes is None:
        minutes = parse_datetime_minutes(text)
    if minutes is None and "tomorrow" in lower:
        minutes = 24 * 60
    if minutes is None:
        return compact_reset_text(text), None
    minutes = max(0, min(total_minutes, minutes))
    return format_reset_minutes(minutes), minutes / total_minutes


def parse_duration_minutes(text):
    units = {
        "minute": 1,
        "min": 1,
        "m": 1,
        "hour": 60,
        "hr": 60,
        "h": 60,
        "day": 24 * 60,
        "d": 24 * 60,
    }
    total = 0.0
    found = False
    pattern = r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)\b"
    for number, unit in re.findall(pattern, text, re.IGNORECASE):
        value = float(number)
        unit = unit.lower().rstrip("s")
        total += value * units.get(unit, 0)
        found = True
    return int(round(total)) if found else None


def parse_weekday_minutes(text):
    weekdays = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    for name, day in weekdays.items():
        if not re.search(rf"\b{name}\b", text):
            continue
        now = datetime.now()
        days = (day - now.weekday()) % 7
        if days == 0:
            days = 7
        return days * 24 * 60
    return None


def parse_datetime_minutes(text):
    now = datetime.now()
    candidates = []
    for pattern in [
        r"\b([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))\b",
        r"\b([A-Z][a-z]{2,8}\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))\b",
    ]:
        candidates.extend(re.findall(pattern, text))
    for candidate in candidates:
        for fmt in ["%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p"]:
            try:
                target = datetime.strptime(candidate, fmt)
                return minutes_until(target, now)
            except ValueError:
                pass

    match = re.search(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", text, re.IGNORECASE)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        period = match.group(3).upper()
        if period == "PM" and hour != 12:
            hour += 12
        if period == "AM" and hour == 12:
            hour = 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return minutes_until(target, now)
    return None


def minutes_until(target, now):
    return max(0, int(round((target - now).total_seconds() / 60)))


def compact_reset_text(text):
    match = re.search(
        r"(?:in|left|remaining|resets?|renews?)\s+(.{1,16})",
        text,
        re.IGNORECASE,
    )
    if match:
        return trim_text(match.group(1), 7)
    return ""


def format_reset_minutes(minutes):
    if minutes <= 0:
        return "now"
    if minutes < 60:
        return f"{int(minutes)}m"
    if minutes < 24 * 60:
        hours = minutes // 60
        mins = minutes % 60
        if mins >= 10:
            return f"{int(hours)}h{int(mins)}m"
        return f"{int(hours)}h"
    days = minutes // (24 * 60)
    hours = (minutes % (24 * 60)) // 60
    if hours >= 6:
        return f"{int(days)}d{int(hours)}h"
    return f"{int(days)}d"


def parse_capture(capture):
    text = (capture.get("focusedText") or "").strip()
    full_text = capture.get("text") or ""
    if len(text) < 60:
        text = full_text
    lines = normalize_lines(text)
    five_patterns = [
        r"\b5\s*[- ]?\s*h(?:our)?s?\b",
        r"\bfive\s*[- ]?\s*hour",
        r"5\s*(?:小时|小時)",
    ]
    week_patterns = [
        r"\bweekly\b",
        r"\bweek\b",
        r"(?:一周|每周|每週|本周|周|週)",
    ]
    five = extract_usage(build_block(lines, five_patterns, week_patterns), "5h")
    week = extract_usage(build_block(lines, week_patterns, five_patterns), "Week")
    return five, week


def looks_like_login(capture):
    url = (capture.get("url") or "").lower()
    title = (capture.get("title") or "").lower()
    text = (capture.get("text") or capture.get("focusedText") or "").lower()
    if "auth" in url or "login" in url:
        return True
    return any(
        marker in text or marker in title
        for marker in [
            "log in",
            "sign up",
            "verify you are human",
            "checking your browser",
            "just a moment",
            "cloudflare",
        ]
    )


def looks_like_browser_challenge(capture):
    title = (capture.get("title") or "").lower()
    text = (capture.get("text") or capture.get("focusedText") or "").lower()
    return (
        "just a moment" in title
        or "checking your browser" in text
        or "verify you are human" in text
        or "cloudflare" in text
    )


def scrape_once(port, cfg, allow_reload):
    target = ensure_target(port)
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Target has no WebSocket debugger URL.")
    with DevToolsClient(ws_url) as cdp:
        if allow_reload and cfg.get("refresh_page_each_poll"):
            try:
                cdp.call("Page.reload", {"ignoreCache": False}, timeout=3)
                time.sleep(float(cfg.get("reload_wait_seconds", 4)))
            except Exception as exc:
                log(f"reload failed: {exc}")
        response = cdp.call(
            "Runtime.evaluate",
            {
                "expression": EXTRACT_JS,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=10,
        )
        value = response.get("result", {}).get("result", {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("Could not read page text.")
        try:
            CAPTURE_PATH.write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return value


def minimize_collector(port):
    try:
        target = ensure_target(port)
        with DevToolsClient(target["webSocketDebuggerUrl"], timeout=4) as cdp:
            win = cdp.call("Browser.getWindowForTarget", {}, timeout=4)
            window_id = win.get("result", {}).get("windowId")
            if window_id is not None:
                cdp.call(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                    timeout=4,
                )
    except Exception as exc:
        log(f"minimize failed: {exc}")


def wait_until_cdp_stops(port, timeout=6):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_cdp_ready(port):
            return True
        time.sleep(0.2)
    return False


def focus_visible_collector(port):
    target = ensure_target(port)
    with DevToolsClient(target["webSocketDebuggerUrl"], timeout=4) as cdp:
        win = cdp.call("Browser.getWindowForTarget", {}, timeout=4)
        window_id = win.get("result", {}).get("windowId")
        if window_id is not None:
            cdp.call(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "windowState": "normal",
                        "left": 120,
                        "top": 80,
                        "width": 1100,
                        "height": 760,
                    },
                },
                timeout=4,
            )
        cdp.call("Runtime.evaluate", {"expression": "window.focus();"}, timeout=2)


def show_collector(port):
    try:
        port = int(port)
        try:
            focus_visible_collector(port)
            return
        except Exception as exc:
            log(f"focus existing collector failed: {exc}")
        close_collector(port)
        wait_until_cdp_stops(port)
        launch_edge(port, mode="visible")
        deadline = time.time() + 18
        while time.time() < deadline:
            if is_cdp_ready(port):
                focus_visible_collector(port)
                return
            time.sleep(0.4)
        raise RuntimeError("Visible Edge collector did not become ready.")
    except Exception as exc:
        log(f"show collector failed: {exc}")
        try:
            edge = find_edge()
            subprocess.Popen([edge, f"--user-data-dir={PROFILE_DIR}", f"--app={TARGET_URL}"])
        except Exception:
            pass


def close_collector(port):
    try:
        version = http_json(port, "/json/version", timeout=1)
        ws_url = version.get("webSocketDebuggerUrl")
        if ws_url:
            with DevToolsClient(ws_url, timeout=3) as cdp:
                cdp.call("Browser.close", {}, timeout=3)
    except Exception as exc:
        log(f"close collector failed: {exc}")


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def filetime_to_int(filetime):
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


class SystemSampler:
    def __init__(self):
        self.last_idle = None
        self.last_kernel = None
        self.last_user = None

    def sample(self):
        return {
            "cpu": self.cpu_percent(),
            "memory": self.memory_percent(),
        }

    def cpu_percent(self):
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None

        idle_value = filetime_to_int(idle)
        kernel_value = filetime_to_int(kernel)
        user_value = filetime_to_int(user)
        if self.last_idle is None:
            self.last_idle = idle_value
            self.last_kernel = kernel_value
            self.last_user = user_value
            return None

        idle_delta = idle_value - self.last_idle
        kernel_delta = kernel_value - self.last_kernel
        user_delta = user_value - self.last_user
        self.last_idle = idle_value
        self.last_kernel = kernel_value
        self.last_user = user_value

        total = kernel_delta + user_delta
        if total <= 0:
            return None
        busy = max(0, total - idle_delta)
        return max(0.0, min(100.0, busy * 100.0 / total))

    def memory_percent(self):
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        if not ok:
            return None
        return float(status.dwMemoryLoad)


class UsageWidget:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.cfg = load_config()
        self.port = int(self.cfg.get("remote_debugging_port") or DEFAULT_CONFIG["remote_debugging_port"])
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.edge_minimized = False
        self.drag = None
        self.last = None
        self.system_sampler = SystemSampler()

        self.root = tk.Tk()
        self.root.title("Codex Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#101214")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.w = int(self.cfg.get("widget_width") or DEFAULT_CONFIG["widget_width"])
        self.h = int(self.cfg.get("widget_height") or DEFAULT_CONFIG["widget_height"])
        if self.w < DEFAULT_CONFIG["widget_width"] or self.h < DEFAULT_CONFIG["widget_height"] or self.w > 420 or self.h > 150:
            self.w = DEFAULT_CONFIG["widget_width"]
            self.h = DEFAULT_CONFIG["widget_height"]
            self.cfg["widget_width"] = self.w
            self.cfg["widget_height"] = self.h
            save_config(self.cfg)
        self.canvas = tk.Canvas(
            self.root,
            width=self.w,
            height=self.h,
            highlightthickness=0,
            bg="#101214",
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._place_window()
        self._build_canvas()
        self._build_menu()
        self._bind_events()
        self._start_worker()
        self.root.after(250, self._drain_events)
        self.root.after(300, self._update_system_metrics)

    def _place_window(self):
        x = self.cfg.get("widget_x")
        y = self.cfg.get("widget_y")
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, sw - self.w - 18)
            y = max(0, sh - self.h - 56)
        self.root.geometry(f"{self.w}x{self.h}+{int(x)}+{int(y)}")

    def _build_canvas(self):
        c = self.canvas
        self.bg = rounded_rect(c, 1, 1, self.w - 1, self.h - 1, 8, fill="#14181b", outline="#2b3238")
        self.status_dot = c.create_oval(7, 6, 14, 13, fill="#f6c177", outline="")
        self.close_btn = c.create_text(self.w - 10, 10, text="x", anchor="center", fill="#d7dee2", font=("Segoe UI Semibold", 9), tags=("close",))

        self.cpu_gauge = self._create_metric_gauge(
            cx=42,
            cy=49,
            radius=26,
            label="CPU",
            color="#ffb86b",
        )
        self.mem_gauge = self._create_metric_gauge(
            cx=101,
            cy=49,
            radius=26,
            label="MEM",
            color="#c792ea",
        )
        self.five_ring = self._create_ring(
            cx=169,
            cy=42,
            radius=24,
            label="5h",
            color="#62c6ff",
            light_color="#14181b",
            timer_color="#237da3",
        )
        self.week_ring = self._create_ring(
            cx=234,
            cy=42,
            radius=24,
            label="7d",
            color="#5be49b",
            light_color="#14181b",
            timer_color="#238a54",
        )

    def _create_metric_gauge(self, cx, cy, radius, label, color):
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        bg = self.canvas.create_arc(
            *box,
            start=205,
            extent=-230,
            style="arc",
            outline="#283139",
            width=4,
        )
        arc = self.canvas.create_arc(
            *box,
            start=205,
            extent=0,
            style="arc",
            outline=color,
            width=4,
        )
        needle = self.canvas.create_line(
            cx,
            cy,
            cx,
            cy - radius + 7,
            fill="#eef2f3",
            width=2,
            capstyle=tk.ROUND,
        )
        hub = self.canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill="#eef2f3", outline="")
        label_id = self.canvas.create_text(
            cx,
            cy - 3,
            text=label,
            anchor="center",
            fill="#9aa7af",
            font=("Segoe UI Semibold", 7),
        )
        value_id = self.canvas.create_text(
            cx,
            cy + 15,
            text="--",
            anchor="center",
            fill="#ffffff",
            font=("Segoe UI Semibold", 9),
        )
        return {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "color": color,
            "bg": bg,
            "arc": arc,
            "needle": needle,
            "hub": hub,
            "label": label_id,
            "value": value_id,
        }

    def _create_ring(self, cx, cy, radius, label, color, light_color, timer_color):
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        bg = self.canvas.create_oval(*box, outline="#273038", width=5)
        inner_radius = radius - 8
        inner_box = (
            cx - inner_radius,
            cy - inner_radius,
            cx + inner_radius,
            cy + inner_radius,
        )
        timer_bg = self.canvas.create_oval(*inner_box, fill=light_color, outline="")
        timer_fill = self.canvas.create_arc(
            *inner_box,
            start=90,
            extent=0,
            style=tk.PIESLICE,
            fill=timer_color,
            outline=timer_color,
        )
        ring = self.canvas.create_line(
            *self._ring_points(cx, cy, radius, 0),
            fill=color,
            width=5,
            capstyle=tk.ROUND,
            smooth=True,
            splinesteps=24,
        )
        label_id = self.canvas.create_text(
            cx,
            cy - 11,
            text=label,
            anchor="center",
            fill="#d9edf5",
            font=("Segoe UI Semibold", 8),
        )
        value_id = self.canvas.create_text(
            cx,
            cy,
            text="--",
            anchor="center",
            fill="#ffffff",
            font=("Segoe UI Semibold", 10),
        )
        reset_id = self.canvas.create_text(
            cx,
            cy + 12,
            text="",
            anchor="center",
            fill="#d6e3e8",
            font=("Segoe UI Semibold", 7),
        )
        return {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "bg": bg,
            "timer_bg": timer_bg,
            "timer_fill": timer_fill,
            "ring": ring,
            "label": label_id,
            "value": value_id,
            "reset": reset_id,
        }

    def _ring_points(self, cx, cy, radius, percent):
        percent = max(0.0, min(100.0, float(percent or 0)))
        if percent <= 0:
            return [cx, cy - radius, cx, cy - radius]
        steps = max(3, int(96 * percent / 100))
        coords = []
        for i in range(steps + 1):
            progress = (percent / 100) * (i / steps)
            angle = math.radians(90 - 360 * progress)
            coords.extend([
                cx + radius * math.cos(angle),
                cy - radius * math.sin(angle),
            ])
        return coords

    def _update_system_metrics(self):
        if self.stop_event.is_set():
            return
        sample = self.system_sampler.sample()
        self._set_metric_gauge(self.cpu_gauge, sample.get("cpu"))
        self._set_metric_gauge(self.mem_gauge, sample.get("memory"))
        interval = max(1, int(self.cfg.get("system_poll_seconds") or 1))
        self.root.after(interval * 1000, self._update_system_metrics)

    def _set_metric_gauge(self, gauge, percent):
        if percent is None:
            self.canvas.itemconfigure(gauge["value"], text="--")
            self.canvas.itemconfigure(gauge["arc"], extent=0)
            x2, y2 = self._gauge_needle_point(gauge, 0)
        else:
            percent = max(0.0, min(100.0, float(percent)))
            self.canvas.itemconfigure(gauge["value"], text=f"{percent:.0f}%")
            self.canvas.itemconfigure(gauge["arc"], extent=-230 * percent / 100)
            x2, y2 = self._gauge_needle_point(gauge, percent)
        self.canvas.coords(gauge["needle"], gauge["cx"], gauge["cy"], x2, y2)

    def _gauge_needle_point(self, gauge, percent):
        percent = max(0.0, min(100.0, float(percent or 0)))
        angle = math.radians(205 - 230 * percent / 100)
        length = gauge["radius"] - 8
        return (
            gauge["cx"] + length * math.cos(angle),
            gauge["cy"] - length * math.sin(angle),
        )

    def _build_menu(self):
        self.menu = Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh_now)
        self.menu.add_command(label="Open collector page", command=lambda: show_collector(self.port))
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.close)

    def _bind_events(self):
        self.canvas.tag_bind("close", "<Button-1>", lambda event: self.close())
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag_window)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Double-Button-1>", lambda event: show_collector(self.port))

    def _show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _start_drag(self, event):
        tags = self.canvas.gettags("current")
        if any(tag in tags for tag in ("close", "refresh", "open")):
            self.drag = None
            return
        self.drag = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_window(self, event):
        if not self.drag:
            return
        sx, sy, wx, wy = self.drag
        nx = wx + (event.x_root - sx)
        ny = wy + (event.y_root - sy)
        self.root.geometry(f"+{nx}+{ny}")

    def _end_drag(self, event):
        if not self.drag:
            return
        self.drag = None
        self.cfg["widget_x"] = self.root.winfo_x()
        self.cfg["widget_y"] = self.root.winfo_y()
        save_config(self.cfg)

    def _start_worker(self):
        thread = threading.Thread(target=self._worker, name="collector", daemon=True)
        thread.start()

    def _worker(self):
        first_poll = True
        while not self.stop_event.is_set():
            try:
                self.port = ensure_browser(self.cfg)
                capture = scrape_once(self.port, self.cfg, allow_reload=not first_poll)
                if (
                    self.cfg.get("browser_mode") == "hidden"
                    and looks_like_browser_challenge(capture)
                ):
                    close_collector(self.port)
                    if not wait_until_cdp_stops(self.port):
                        self.port = find_free_port(self.port + 1)
                        self.cfg["remote_debugging_port"] = self.port
                        save_config(self.cfg)
                    launch_edge(self.port, mode="hidden")
                    deadline = time.time() + 18
                    while time.time() < deadline and not is_cdp_ready(self.port):
                        time.sleep(0.4)
                    capture = scrape_once(self.port, self.cfg, allow_reload=False)
                five, week = parse_capture(capture)
                login = looks_like_login(capture)
                payload = {
                    "ok": True,
                    "login": login,
                    "five": five,
                    "week": week,
                    "url": capture.get("url", ""),
                    "title": capture.get("title", ""),
                    "updated": datetime.now().strftime("%H:%M:%S"),
                }
                self.events.put(payload)
                if (
                    not login
                    and self.cfg.get("minimize_edge_after_data")
                    and self.cfg.get("browser_mode") == "visible"
                    and not self.edge_minimized
                    and (five.get("percent") is not None or week.get("percent") is not None)
                ):
                    minimize_collector(self.port)
                    self.edge_minimized = True
            except Exception as exc:
                log(traceback.format_exc())
                self.events.put({
                    "ok": False,
                    "error": str(exc),
                    "updated": datetime.now().strftime("%H:%M:%S"),
                })
            first_poll = False
            wait_for = max(5, int(self.cfg.get("poll_seconds") or 30))
            deadline = time.time() + wait_for
            while time.time() < deadline and not self.stop_event.is_set():
                if self.refresh_event.wait(0.2):
                    self.refresh_event.clear()
                    break

    def refresh_now(self):
        self.refresh_event.set()
        self.canvas.itemconfigure(self.status_dot, fill="#f6c177")

    def _drain_events(self):
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            self._apply_event(item)
        if not self.stop_event.is_set():
            self.root.after(250, self._drain_events)

    def _apply_event(self, item):
        self.last = item
        if not item.get("ok"):
            self.canvas.itemconfigure(self.status_dot, fill="#ff6b6b")
            self._set_period("five", {"value": "Error", "detail": trim_text(item.get("error", ""), 58), "percent": None})
            self._set_period("week", {"value": "Collector", "detail": "Right-click > Open collector page", "percent": None})
            return
        if item.get("login"):
            self.canvas.itemconfigure(self.status_dot, fill="#f6c177")
            self._set_period("five", {"value": "Login needed", "detail": "Finish ChatGPT login in the Edge window", "percent": None})
            self._set_period("week", item["week"])
        else:
            self.canvas.itemconfigure(self.status_dot, fill="#5be49b")
            self._set_period("five", item["five"])
            self._set_period("week", item["week"])

    def _set_period(self, key, data):
        if key == "five":
            ring, color = self.five_ring, "#62c6ff"
        else:
            ring, color = self.week_ring, "#5be49b"
        pct = data.get("percent")
        if pct is None:
            text = self._compact_status_text(data.get("value", ""))
            progress = 0
            fill = "#3a4247"
        else:
            pct = max(0, min(100, float(pct)))
            text = f"{pct:.0f}%"
            progress = pct
            fill = color
        self.canvas.coords(
            ring["ring"],
            *self._ring_points(ring["cx"], ring["cy"], ring["radius"], progress),
        )
        self.canvas.itemconfigure(ring["ring"], fill=fill)
        self.canvas.itemconfigure(ring["value"], text=text)

        reset_fraction = data.get("reset_fraction")
        reset_text = data.get("reset_text", "")
        if reset_fraction is None:
            reset_extent = 0
        else:
            reset_fraction = max(0, min(1, float(reset_fraction)))
            reset_extent = -359.9 * reset_fraction
        self.canvas.itemconfigure(ring["timer_fill"], extent=reset_extent)
        self.canvas.itemconfigure(ring["reset"], text=trim_text(reset_text, 6))

    def _compact_status_text(self, value):
        value = (value or "").lower()
        if "login" in value:
            return "login"
        if "error" in value:
            return "err"
        if "collector" in value:
            return "..."
        return "--"

    def close(self):
        self.stop_event.set()
        if self.cfg.get("close_edge_on_exit"):
            close_collector(self.port)
        save_config(self.cfg)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    app = UsageWidget()
    app.run()


if __name__ == "__main__":
    main()
