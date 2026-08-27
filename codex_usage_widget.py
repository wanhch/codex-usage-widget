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

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None
    ImageDraw = None
    ImageTk = None


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
    "data_source": "codex_cli",
    "codex_cli_path": None,
    "kimi_cli_path": None,
    "show_kimi_usage": True,
    "remote_debugging_port": 39225,
    "refresh_page_each_poll": False,
    "reload_wait_seconds": 4,
    "browser_mode": "visible",
    "minimize_edge_after_data": True,
    "close_edge_on_exit": True,
    "system_poll_seconds": 1,
    "gauge_animation_ms": 100,
    "show_five_hour_usage": False,
    "widget_width": 334,
    "widget_height": 48,
    "widget_x": None,
    "widget_y": None,
    "allow_taskbar_overlap": True,
}

LEGACY_WIDGET_SIZES = {(183, 50), (228, 50), (236, 50), (281, 50), (286, 78), (286, 50), (334, 50)}
WIDGET_BG = "#14181b"
UI_TEXT = "#f2f4f5"
UI_MUTED = "#b8c0c6"
UI_TRACK = "#343a40"
UI_NEEDLE = "#e8edf0"
STATUS_WAIT = "#d8ae5e"
STATUS_OK = "#62c99a"
STATUS_ERROR = "#ef6a70"
GPT_FIVE = "#c6ccd3"
GPT_WEEK = "#6f7883"
GPT_FIVE_TIMER = "#3f4347"
GPT_WEEK_TIMER = "#262b30"
KIMI_FIVE = "#4da8ff"
KIMI_WEEK = "#2f6bff"
KIMI_FIVE_TIMER = "#2f5d88"
KIMI_WEEK_TIMER = "#214088"
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def _hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[index:index + 2], 16) for index in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(value)))) for value in rgb))


def _blend_color(left, right, ratio):
    left_rgb = _hex_to_rgb(left)
    right_rgb = _hex_to_rgb(right)
    return _rgb_to_hex(
        left_rgb[index] + (right_rgb[index] - left_rgb[index]) * ratio
        for index in range(3)
    )


def detect_taskbar_color():
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return WIDGET_BG
        rect = (ctypes.c_long * 4)()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return WIDGET_BG
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return WIDGET_BG
        dc = user32.GetDC(None)
        if not dc:
            return WIDGET_BG
        samples = []
        try:
            for xi in range(2, 39):
                x = left + (right - left - 1) * xi // 40
                for yi in (2, 5, 8):
                    y = top + (bottom - top - 1) * yi // 10
                    value = gdi32.GetPixel(dc, x, y)
                    if value == -1:
                        continue
                    samples.append((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
        finally:
            user32.ReleaseDC(None, dc)
        if not samples:
            return WIDGET_BG
        channels = list(zip(*samples))
        median = tuple(sorted(channel)[len(channel) // 2] for channel in channels)
        return _rgb_to_hex(median)
    except Exception:
        return WIDGET_BG


def apply_taskbar_palette(background):
    global WIDGET_BG, UI_TEXT, UI_MUTED, UI_TRACK, UI_NEEDLE
    global GPT_FIVE, GPT_WEEK, KIMI_FIVE, KIMI_WEEK
    global GPT_FIVE_TIMER, GPT_WEEK_TIMER, KIMI_FIVE_TIMER, KIMI_WEEK_TIMER
    WIDGET_BG = background
    red, green, blue = _hex_to_rgb(background)
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    light_background = luminance > 0.58
    foreground = "#17191b" if light_background else "#f2f4f5"
    UI_TEXT = foreground
    UI_MUTED = _blend_color(background, foreground, 0.70)
    UI_TRACK = _blend_color(background, foreground, 0.16)
    UI_NEEDLE = _blend_color(background, foreground, 0.90)
    GPT_FIVE = "#5d6670" if light_background else "#c6ccd3"
    GPT_WEEK = "#31383f" if light_background else "#6f7883"
    KIMI_FIVE = "#1582e3" if light_background else "#4da8ff"
    KIMI_WEEK = "#2456d6" if light_background else "#2f6bff"
    GPT_FIVE_TIMER = _blend_color(background, GPT_FIVE, 0.24)
    GPT_WEEK_TIMER = _blend_color(background, GPT_WEEK, 0.20)
    KIMI_FIVE_TIMER = _blend_color(background, KIMI_FIVE, 0.48)
    KIMI_WEEK_TIMER = _blend_color(background, KIMI_WEEK, 0.48)


EXTRACT_JS = r"""
(() => {
  const bodyText = document.body ? document.body.innerText : "";
  const lines = bodyText.split(/\n+/).map(s => s.trim()).filter(Boolean);
  const hit = /(codex|usage|limit|session|5\s*[- ]?\s*h(?:our)?s?|weekly|week|remaining|reset|用量|限额|限制|小时|小時|一周|每周|每週|周|週)/i;
  const chunks = [];
  const seenChunks = new Set();
  for (let i = 0; i < lines.length; i++) {
    if (!hit.test(lines[i])) continue;
    const chunk = lines.slice(Math.max(0, i - 3), Math.min(lines.length, i + 7));
    const key = chunk.join("\n").slice(0, 1800);
    if (seenChunks.has(key)) continue;
    seenChunks.add(key);
    chunks.push(...chunk, "---");
  }
  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    text: bodyText.slice(0, 120000),
    focusedText: chunks.slice(0, 520).join("\n"),
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


def find_codex_cli(configured_path=None):
    candidates = []
    if configured_path:
        candidates.append(pathlib.Path(os.path.expandvars(str(configured_path))).expanduser())

    found = shutil.which("codex.exe")
    if found:
        candidates.append(pathlib.Path(found))

    appdata = pathlib.Path(os.environ.get("APPDATA", ""))
    localappdata = pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
    package_roots = [
        appdata / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai",
        localappdata / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai",
    ]
    for package_root in package_roots:
        if not package_root.exists():
            continue
        candidates.extend(package_root.glob("codex-win32-*/vendor/*/bin/codex.exe"))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            pass
    raise RuntimeError(
        "Codex CLI was not found. Install/update Codex CLI or set codex_cli_path in config.json."
    )


def query_codex_rate_limits(cfg, timeout=15):
    codex = find_codex_cli(cfg.get("codex_cli_path"))
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    process = subprocess.Popen(
        [codex, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )
    responses = queue.Queue()

    def read_stdout():
        try:
            for line in process.stdout:
                responses.put(line)
        except Exception:
            pass

    threading.Thread(target=read_stdout, name="codex-app-server-reader", daemon=True).start()
    messages = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-usage-widget",
                    "title": "Codex Usage Widget",
                    "version": "1.0",
                }
            },
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read"},
    ]
    try:
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if process.poll() is not None and responses.empty():
                raise RuntimeError(f"Codex app-server exited with code {process.returncode}.")
            try:
                line = responses.get(timeout=min(0.5, max(0.05, deadline - time.time())))
            except queue.Empty:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != 2:
                continue
            if response.get("error"):
                raise RuntimeError(f"Codex rate-limit query failed: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex app-server returned no rate-limit data.")
            return result
        raise TimeoutError("Codex app-server rate-limit query timed out.")
    finally:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def usage_from_rate_limit_window(window, title):
    result = {
        "title": title,
        "value": "Waiting",
        "detail": "No matching rate-limit window",
        "percent": None,
        "reset_text": "",
        "reset_fraction": None,
        "raw": "",
    }
    if not isinstance(window, dict) or window.get("usedPercent") is None:
        return result

    used_percent = max(0.0, min(100.0, float(window["usedPercent"])))
    percent = 100.0 - used_percent
    duration = window.get("windowDurationMins")
    resets_at = window.get("resetsAt")
    result["value"] = f"{format_number(percent)}% remaining"
    result["percent"] = percent
    result["detail"] = f"Codex CLI {format_number(percent)}% remaining"
    result["raw"] = json.dumps(window, ensure_ascii=False)
    if resets_at is not None:
        minutes = max(0, int(round((float(resets_at) - time.time()) / 60)))
        result["reset_text"] = format_reset_minutes(minutes)
        if duration:
            result["reset_fraction"] = max(0.0, min(1.0, minutes / float(duration)))
    return result


def parse_codex_rate_limits(response):
    buckets = response.get("rateLimitsByLimitId") or {}
    snapshot = buckets.get("codex") or response.get("rateLimits") or {}
    windows = [snapshot.get("primary"), snapshot.get("secondary")]
    five_window = None
    week_window = None
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        if duration is not None and duration <= 24 * 60:
            five_window = window
        elif duration is not None:
            week_window = window
        elif index == 0:
            five_window = window
        else:
            week_window = window
    return (
        usage_from_rate_limit_window(five_window, "5h"),
        usage_from_rate_limit_window(week_window, "Week"),
    )


def collect_codex_cli_usage(cfg):
    response = query_codex_rate_limits(cfg)
    five, week = parse_codex_rate_limits(response)
    try:
        CAPTURE_PATH.write_text(
            json.dumps({"source": "codex_cli", "response": response}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return {
        "ok": True,
        "login": False,
        "source": "codex_cli",
        "five": five,
        "week": week,
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


def open_usage_page():
    try:
        os.startfile(TARGET_URL)
    except Exception as exc:
        log(f"open usage page failed: {exc}")


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


def find_kimi_cli(configured_path=None):
    candidates = []
    if configured_path:
        candidates.append(pathlib.Path(os.path.expandvars(str(configured_path))).expanduser())
    found = shutil.which("kimi.exe")
    if found:
        candidates.append(pathlib.Path(found))
    candidates.append(pathlib.Path.home() / ".kimi-code" / "bin" / "kimi.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            pass
    raise RuntimeError(
        "Kimi CLI was not found. Install/update Kimi Code CLI or set kimi_cli_path in config.json."
    )


class KimiUsageClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.port = None

    def _server_token(self):
        token_path = pathlib.Path.home() / ".kimi-code" / "server.token"
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("Kimi local server token is empty.")
        return token

    def _request_usage(self, timeout=4):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/oauth/usage",
            headers={
                "Authorization": f"Bearer {self._server_token()}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(payload.get("msg") or "Kimi usage query failed.")
        data = payload["data"]
        if data.get("kind") != "ok":
            raise RuntimeError(data.get("message") or "Kimi login or subscription is unavailable.")
        return data

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        kimi = find_kimi_cli(self.cfg.get("kimi_cli_path"))
        self.port = find_free_port(58649)
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self.process = subprocess.Popen(
            [kimi, "web", "--no-open", "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        deadline = time.time() + 25
        last_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Kimi local server exited with code {self.process.returncode}.")
            try:
                self._request_usage(timeout=2)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.4)
        self.close()
        raise RuntimeError(f"Kimi local usage service did not become ready: {last_error}")

    def query(self):
        self.start()
        try:
            return self._request_usage()
        except Exception:
            self.close()
            self.start()
            return self._request_usage()

    def close(self):
        process = self.process
        self.process = None
        self.port = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def usage_from_kimi_window(window, title):
    result = {
        "title": title,
        "value": "Waiting",
        "detail": "No matching Kimi rate-limit window",
        "percent": None,
        "reset_text": "",
        "reset_fraction": None,
        "raw": "",
    }
    if not isinstance(window, dict):
        return result
    used = float(window.get("used") or 0)
    limit = float(window.get("limit") or 0)
    if limit <= 0:
        return result
    percent = max(0.0, min(100.0, (limit - used) / limit * 100.0))
    result["value"] = f"{format_number(percent)}% remaining"
    result["percent"] = percent
    result["detail"] = f"Kimi {format_number(percent)}% remaining"
    result["raw"] = json.dumps(window, ensure_ascii=False)
    window_info = window.get("window") or {}
    duration = float(window_info.get("duration") or 0)
    unit = (window_info.get("unit") or "").lower()
    total_minutes = duration * (60 if unit.startswith("hour") else 7 * 24 * 60 if unit.startswith("week") else 0)
    reset_at = window.get("reset_at")
    if reset_at:
        try:
            reset_timestamp = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
            minutes = max(0, int(round((reset_timestamp - time.time()) / 60)))
            result["reset_text"] = format_reset_minutes(minutes)
            if total_minutes:
                result["reset_fraction"] = max(0.0, min(1.0, minutes / total_minutes))
        except (TypeError, ValueError):
            pass
    return result


def parse_kimi_usage(data):
    windows = [data.get("summary")] + list(data.get("limits") or [])
    five_window = None
    week_window = None
    for window in windows:
        info = window.get("window") if isinstance(window, dict) else None
        if not isinstance(info, dict):
            continue
        unit = (info.get("unit") or "").lower()
        duration = float(info.get("duration") or 0)
        if unit.startswith("hour") and duration == 5:
            five_window = window
        elif unit.startswith("week"):
            week_window = window
    return usage_from_kimi_window(five_window, "K5"), usage_from_kimi_window(week_window, "K7")


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
        remaining = max(0.0, limit - used)
        pct = max(0.0, min(100.0, remaining / limit * 100.0))
        result["value"] = f"{format_number(remaining)} / {format_number(limit)} remaining"
        result["percent"] = pct
        result["detail"] = find_detail_line(block_lines)
        enrich_usage_result(result, title)
        return result

    percent_match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", compact)
    if percent_match:
        shown_pct = max(0.0, min(100.0, float(percent_match.group(1))))
        is_remaining = re.search(
            r"\b(?:remaining|left|available)\b|剩余|可用",
            compact,
            re.IGNORECASE,
        )
        pct = shown_pct if is_remaining else 100.0 - shown_pct
        result["value"] = f"{format_number(pct)}% remaining"
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
    focused_text = (capture.get("focusedText") or "").strip()
    full_text = capture.get("text") or ""
    five_patterns = [
        r"\b5\s*[- ]?\s*h(?:our)?s?\b",
        r"\bfive\s*[- ]?\s*hour",
        r"\bsession(?:\s+usage)?(?:\s+limit)?\b",
        r"5\s*(?:小时|小時)",
    ]
    week_patterns = [
        r"\bweekly\b",
        r"\bweek\b",
        r"(?:一周|每周|每週|本周|周|週)",
    ]
    def parse_text(text):
        lines = normalize_lines(text)
        return (
            extract_usage(build_block(lines, five_patterns, week_patterns), "5h"),
            extract_usage(build_block(lines, week_patterns, five_patterns), "Week"),
        )

    five, week = parse_text(focused_text if len(focused_text) >= 60 else full_text)
    if full_text and (five.get("percent") is None or week.get("percent") is None):
        full_five, full_week = parse_text(full_text)
        if five.get("percent") is None and full_five.get("percent") is not None:
            five = full_five
        if week.get("percent") is None and full_week.get("percent") is not None:
            week = full_week
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


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
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


class PDH_FMT_COUNTERVALUE_UNION(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("wideStringValue", ctypes.c_wchar_p),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("CStatus", ctypes.c_ulong),
        ("value", PDH_FMT_COUNTERVALUE_UNION),
    ]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", ctypes.c_wchar_p),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


def filetime_to_int(filetime):
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


class SystemSampler:
    def __init__(self):
        self.last_idle = None
        self.last_kernel = None
        self.last_user = None
        self.pdh = None
        self.gpu_query = ctypes.c_void_p()
        self.gpu_counter = ctypes.c_void_p()
        self._init_gpu_counter()

    def sample(self):
        return {
            "cpu": self.cpu_percent(),
            "memory": self.memory_percent(),
            "gpu": self.gpu_percent(),
        }

    def _init_gpu_counter(self):
        try:
            self.pdh = ctypes.WinDLL("pdh")
            self.pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.pdh.PdhOpenQueryW.restype = ctypes.c_long
            self.pdh.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self.pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
            self.pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.pdh.PdhCollectQueryData.restype = ctypes.c_long
            self.pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.c_void_p,
            ]
            self.pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
            self.pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            self.pdh.PdhCloseQuery.restype = ctypes.c_long
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.gpu_query)) != 0:
                self.pdh = None
                return
            status = self.pdh.PdhAddEnglishCounterW(
                self.gpu_query,
                r"\GPU Engine(*)\Utilization Percentage",
                0,
                ctypes.byref(self.gpu_counter),
            )
            if status != 0:
                self.close()
                return
            self.pdh.PdhCollectQueryData(self.gpu_query)
        except Exception:
            self.close()

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

    def gpu_percent(self):
        if self.pdh is None or not self.gpu_counter.value:
            return None
        try:
            if self.pdh.PdhCollectQueryData(self.gpu_query) != 0:
                return None
            buffer_size = ctypes.c_ulong(0)
            item_count = ctypes.c_ulong(0)
            status = self.pdh.PdhGetFormattedCounterArrayW(
                self.gpu_counter,
                0x00000200,
                ctypes.byref(buffer_size),
                ctypes.byref(item_count),
                None,
            )
            if status & 0xFFFFFFFF != 0x800007D2 or buffer_size.value == 0:
                return None
            buffer = ctypes.create_string_buffer(buffer_size.value)
            status = self.pdh.PdhGetFormattedCounterArrayW(
                self.gpu_counter,
                0x00000200,
                ctypes.byref(buffer_size),
                ctypes.byref(item_count),
                buffer,
            )
            if status != 0:
                return None
            items = ctypes.cast(buffer, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
            engine_totals = {}
            for index in range(item_count.value):
                item = items[index]
                if item.FmtValue.CStatus not in (0, 1):
                    continue
                name = item.szName or ""
                match = re.search(
                    r"luid_(.+?)_phys_(\d+)_eng_(\d+)_engtype_(.+)$",
                    name,
                    re.IGNORECASE,
                )
                key = match.groups() if match else (name,)
                value = max(0.0, float(item.FmtValue.doubleValue))
                engine_totals[key] = engine_totals.get(key, 0.0) + value
            if not engine_totals:
                return 0.0
            return max(0.0, min(100.0, max(engine_totals.values())))
        except Exception:
            return None

    def close(self):
        if self.pdh is not None and self.gpu_query.value:
            try:
                self.pdh.PdhCloseQuery(self.gpu_query)
            except Exception:
                pass
        self.gpu_query = ctypes.c_void_p()
        self.gpu_counter = ctypes.c_void_p()
        self.pdh = None


class UsageWidget:
    def __init__(self):
        apply_taskbar_palette(detect_taskbar_color())
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
        self.browser_started = False
        self.drag = None
        self.last = None
        self.system_sampler = SystemSampler()
        self.kimi_client = KimiUsageClient(self.cfg)
        self.use_bitmap_dials = Image is not None and ImageTk is not None
        self.dial_scale = 4
        self.dial_photo = None
        self.dial_image_id = None
        self.dial_state = {
            "five": {"label": "G5", "percent": None, "text": "--", "reset_text": "", "reset_fraction": None},
            "week": {"label": "G7", "percent": None, "text": "--", "reset_text": "", "reset_fraction": None},
            "kimi_five": {"label": "K5", "percent": None, "text": "--", "reset_text": "", "reset_fraction": None},
            "kimi_week": {"label": "K7", "percent": None, "text": "--", "reset_text": "", "reset_fraction": None},
        }
        self.cpu_gauge = self._metric_state()
        self.mem_gauge = self._metric_state()
        self.gpu_gauge = self._metric_state()
        self.show_five_hour_usage = bool(self.cfg.get("show_five_hour_usage", False))
        self.show_kimi_usage = bool(self.cfg.get("show_kimi_usage", True))
        self.gpu_x = 123
        self.five_x = 182
        self.week_x = 235 if self.show_five_hour_usage else 182
        self.kimi_five_x = self.week_x + 53
        self.kimi_week_x = self.kimi_five_x + 53
        if self.show_kimi_usage:
            layout_width = self.kimi_week_x + 46
        else:
            layout_width = self.week_x + 46

        self.root = tk.Tk()
        self.root.title("Coding Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=WIDGET_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.w = int(self.cfg.get("widget_width") or layout_width)
        self.h = int(self.cfg.get("widget_height") or DEFAULT_CONFIG["widget_height"])
        if (self.w, self.h) in LEGACY_WIDGET_SIZES or (not self.show_five_hour_usage and (self.w, self.h) == (236, 50)):
            self.w = layout_width
            self.h = DEFAULT_CONFIG["widget_height"]
            self.cfg["widget_width"] = self.w
            self.cfg["widget_height"] = self.h
            save_config(self.cfg)
        elif self.w < 160 or self.h < 46 or self.w > 420 or self.h > 150:
            self.w = layout_width
            self.h = DEFAULT_CONFIG["widget_height"]
            self.cfg["widget_width"] = self.w
            self.cfg["widget_height"] = self.h
            save_config(self.cfg)
        self.canvas = tk.Canvas(
            self.root,
            width=self.w,
            height=self.h,
            highlightthickness=0,
            bg=WIDGET_BG,
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
        self.root.after(400, self._animate_metric_gauges)
        self.root.after(800, self._keep_window_visible)

    def _place_window(self):
        x = self.cfg.get("widget_x")
        y = self.cfg.get("widget_y")
        if x is None or y is None:
            left, top, right, bottom = self._visible_bounds_for(0, 0)
            x = max(left, right - self.w - 8)
            y = max(top, bottom - self.h - 2)
        x, y = self._clamp_window_position(x, y)
        if self.cfg.get("widget_x") != x or self.cfg.get("widget_y") != y:
            self.cfg["widget_x"] = x
            self.cfg["widget_y"] = y
            save_config(self.cfg)
        self.root.geometry(f"{self.w}x{self.h}+{int(x)}+{int(y)}")
        self._force_topmost()

    def _visible_bounds_for(self, x, y):
        try:
            user32 = ctypes.windll.user32
            user32.MonitorFromRect.restype = ctypes.c_void_p
            rect = RECT(int(x), int(y), int(x) + self.w, int(y) + self.h)
            monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)
            if monitor:
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    bounds = info.rcMonitor if self.cfg.get("allow_taskbar_overlap", True) else info.rcWork
                    return bounds.left, bounds.top, bounds.right, bounds.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_window_position(self, x, y):
        x = int(x)
        y = int(y)
        left, top, right, bottom = self._visible_bounds_for(x, y)
        max_x = max(left, right - self.w)
        max_y = max(top, bottom - self.h)
        return min(max(x, left), max_x), min(max(y, top), max_y)

    def _force_topmost(self):
        try:
            self.root.attributes("-topmost", True)
            hwnd = int(self.root.winfo_id())
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.c_bool
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            self.root.lift()
        except Exception:
            pass

    def _keep_window_visible(self):
        if self.stop_event.is_set():
            return
        self._force_topmost()
        x, y = self._clamp_window_position(self.root.winfo_x(), self.root.winfo_y())
        if x != self.root.winfo_x() or y != self.root.winfo_y():
            self.root.geometry(f"+{x}+{y}")
            self.cfg["widget_x"] = x
            self.cfg["widget_y"] = y
            save_config(self.cfg)
            self._force_topmost()
        self.root.after(800, self._keep_window_visible)

    def _build_canvas(self):
        c = self.canvas
        self.bg = c.create_rectangle(0, 0, self.w, self.h, fill=WIDGET_BG, outline="")
        self.status_dot = c.create_oval(5, 5, 10, 10, fill=STATUS_WAIT, outline="")
        self.close_btn = c.create_text(self.w - 8, 8, text="x", anchor="center", fill=UI_MUTED, font=("Segoe UI Semibold", 8), tags=("close",))
        if self.use_bitmap_dials:
            self.dial_image_id = c.create_image(0, 0, anchor="nw")
            self.bitmap_text = {
                "cpu_label": c.create_text(33, 41, text="CPU", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 7)),
                "mem_label": c.create_text(78, 41, text="MEM", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 7)),
                "gpu_label": c.create_text(self.gpu_x, 41, text="GPU", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 7)),
                "week_label": c.create_text(self.week_x, 15, text="G7", anchor="center", fill=GPT_FIVE, font=("Segoe UI Semibold", 7)),
                "week_value": c.create_text(self.week_x, 24, text="--", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 9)),
                "week_reset": c.create_text(self.week_x, 33, text="", anchor="center", fill=UI_MUTED, font=("Segoe UI Semibold", 6)),
            }
            if self.show_five_hour_usage:
                self.bitmap_text.update({
                    "five_label": c.create_text(self.five_x, 15, text="G5", anchor="center", fill=GPT_FIVE, font=("Segoe UI Semibold", 7)),
                    "five_value": c.create_text(self.five_x, 24, text="--", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 9)),
                    "five_reset": c.create_text(self.five_x, 33, text="", anchor="center", fill=UI_MUTED, font=("Segoe UI Semibold", 6)),
                })
            if self.show_kimi_usage:
                self.bitmap_text.update({
                    "kimi_five_label": c.create_text(self.kimi_five_x, 15, text="K5", anchor="center", fill=KIMI_FIVE, font=("Segoe UI Semibold", 7)),
                    "kimi_five_value": c.create_text(self.kimi_five_x, 24, text="--", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 9)),
                    "kimi_five_reset": c.create_text(self.kimi_five_x, 33, text="", anchor="center", fill=UI_MUTED, font=("Segoe UI Semibold", 6)),
                    "kimi_week_label": c.create_text(self.kimi_week_x, 15, text="K7", anchor="center", fill=KIMI_WEEK, font=("Segoe UI Semibold", 7)),
                    "kimi_week_value": c.create_text(self.kimi_week_x, 24, text="--", anchor="center", fill=UI_TEXT, font=("Segoe UI Semibold", 9)),
                    "kimi_week_reset": c.create_text(self.kimi_week_x, 33, text="", anchor="center", fill=UI_MUTED, font=("Segoe UI Semibold", 6)),
                })
            self._render_bitmap_dials()
            self._sync_bitmap_text()
            for item in self.bitmap_text.values():
                c.tag_raise(item)
            c.tag_raise(self.status_dot)
            c.tag_raise(self.close_btn)
            return

        self.cpu_gauge = self._create_metric_gauge(
            cx=33,
            cy=28,
            radius=17,
            label="CPU",
            color="#72b6c9",
        )
        self.mem_gauge = self._create_metric_gauge(
            cx=78,
            cy=28,
            radius=17,
            label="MEM",
            color="#a99bd4",
        )
        self.gpu_gauge = self._create_metric_gauge(
            cx=self.gpu_x,
            cy=28,
            radius=17,
            label="GPU",
            color="#68aee8",
        )
        self.five_ring = None
        if self.show_five_hour_usage:
            self.five_ring = self._create_ring(
                cx=self.five_x,
                cy=24,
                radius=18,
                label="G5",
                color=GPT_FIVE,
                light_color=WIDGET_BG,
                timer_color=GPT_FIVE_TIMER,
            )
        self.week_ring = self._create_ring(
            cx=self.week_x,
            cy=24,
            radius=18,
            label="G7",
            color=GPT_WEEK,
            light_color=WIDGET_BG,
            timer_color=GPT_WEEK_TIMER,
        )
        self.kimi_five_ring = None
        self.kimi_week_ring = None
        if self.show_kimi_usage:
            self.kimi_five_ring = self._create_ring(
                cx=self.kimi_five_x,
                cy=24,
                radius=18,
                label="K5",
                color=KIMI_FIVE,
                light_color=WIDGET_BG,
                timer_color=KIMI_FIVE_TIMER,
            )
            self.kimi_week_ring = self._create_ring(
                cx=self.kimi_week_x,
                cy=24,
                radius=18,
                label="K7",
                color=KIMI_WEEK,
                light_color=WIDGET_BG,
                timer_color=KIMI_WEEK_TIMER,
            )

    def _metric_state(self):
        return {
            "target": None,
            "display": 0.0,
        }

    def _create_metric_gauge(self, cx, cy, radius, label, color):
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        bg = self.canvas.create_arc(
            *box,
            start=205,
            extent=-230,
            style="arc",
            outline=UI_TRACK,
            width=3,
        )
        segments = []
        segment_count = 28
        for index in range(segment_count):
            segment = self.canvas.create_line(
                *self._gauge_segment_points(cx, cy, radius, index / segment_count, (index + 0.72) / segment_count),
                fill=self._metric_gradient_color((index + 1) / segment_count),
                width=3,
                capstyle=tk.ROUND,
                state="hidden",
            )
            segments.append(segment)
        needle = self.canvas.create_line(
            cx,
            cy,
            cx,
            cy - radius + 7,
            fill=UI_NEEDLE,
            width=2,
            capstyle=tk.ROUND,
        )
        hub = self.canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=UI_NEEDLE, outline="")
        label_id = self.canvas.create_text(
            cx,
            cy + radius - 4,
            text=label,
            anchor="center",
            fill=UI_MUTED,
            font=("Segoe UI Semibold", 7),
        )
        return {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "color": color,
            "bg": bg,
            "segments": segments,
            "segment_count": segment_count,
            "needle": needle,
            "hub": hub,
            "label": label_id,
            "target": None,
            "display": 0.0,
            "visible_segments": -1,
        }

    def _create_ring(self, cx, cy, radius, label, color, light_color, timer_color):
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        bg = self.canvas.create_oval(*box, outline=UI_TRACK, width=4)
        inner_radius = radius - 6
        inner_box = (
            cx - inner_radius,
            cy - inner_radius,
            cx + inner_radius,
            cy + inner_radius,
        )
        timer_bg = self.canvas.create_oval(*inner_box, fill=light_color, outline="")
        timer_fill = self.canvas.create_polygon(
            *self._timer_slice_coords(cx, cy, inner_radius, 0),
            fill=timer_color,
            outline="",
        )
        ring = self.canvas.create_line(
            *self._ring_points(cx, cy, radius, 0),
            fill=color,
            width=4,
            capstyle=tk.ROUND,
            smooth=True,
            splinesteps=24,
        )
        label_id = self.canvas.create_text(
            cx,
            cy - 11,
            text=label,
            anchor="center",
            fill=UI_MUTED,
            font=("Segoe UI Semibold", 7),
        )
        value_id = self.canvas.create_text(
            cx,
            cy,
            text="--",
            anchor="center",
            fill=UI_TEXT,
            font=("Segoe UI Semibold", 9),
        )
        reset_id = self.canvas.create_text(
            cx,
            cy + 12,
            text="",
            anchor="center",
            fill=UI_MUTED,
            font=("Segoe UI Semibold", 6),
        )
        return {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "timer_radius": inner_radius,
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

    def _gauge_segment_points(self, cx, cy, radius, start_ratio, end_ratio):
        coords = []
        steps = 4
        for i in range(steps + 1):
            ratio = start_ratio + (end_ratio - start_ratio) * i / steps
            angle = math.radians(205 - 230 * ratio)
            coords.extend([
                cx + radius * math.cos(angle),
                cy - radius * math.sin(angle),
            ])
        return coords

    def _metric_gradient_color(self, ratio):
        ratio = max(0.0, min(1.0, float(ratio)))
        stops = [
            (0.00, (104, 174, 196)),
            (0.55, (104, 174, 196)),
            (0.78, (222, 179, 98)),
            (0.92, (239, 112, 100)),
            (1.00, (190, 58, 76)),
        ]
        for index in range(len(stops) - 1):
            left_pos, left_color = stops[index]
            right_pos, right_color = stops[index + 1]
            if ratio <= right_pos:
                local = (ratio - left_pos) / (right_pos - left_pos)
                rgb = [
                    int(round(left_color[channel] + (right_color[channel] - left_color[channel]) * local))
                    for channel in range(3)
                ]
                return "#{:02x}{:02x}{:02x}".format(*rgb)
        return "#{:02x}{:02x}{:02x}".format(*stops[-1][1])

    def _render_bitmap_dials(self):
        if not self.use_bitmap_dials:
            return
        scale = self.dial_scale
        image = Image.new("RGBA", (self.w * scale, self.h * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        self._draw_bitmap_gauge(draw, 33, 28, 17, self.cpu_gauge.get("display", 0.0), scale)
        self._draw_bitmap_gauge(draw, 78, 28, 17, self.mem_gauge.get("display", 0.0), scale)
        self._draw_bitmap_gauge(draw, self.gpu_x, 28, 17, self.gpu_gauge.get("display", 0.0), scale)
        if self.show_five_hour_usage:
            self._draw_bitmap_ring(draw, self.five_x, 24, 18, self.dial_state["five"], GPT_FIVE, GPT_FIVE_TIMER, scale)
        self._draw_bitmap_ring(draw, self.week_x, 24, 18, self.dial_state["week"], GPT_WEEK, GPT_WEEK_TIMER, scale)
        if self.show_kimi_usage:
            self._draw_bitmap_ring(draw, self.kimi_five_x, 24, 18, self.dial_state["kimi_five"], KIMI_FIVE, KIMI_FIVE_TIMER, scale)
            self._draw_bitmap_ring(draw, self.kimi_week_x, 24, 18, self.dial_state["kimi_week"], KIMI_WEEK, KIMI_WEEK_TIMER, scale)

        image = image.resize((self.w, self.h), Image.Resampling.LANCZOS)
        self.dial_photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfigure(self.dial_image_id, image=self.dial_photo)
        self._sync_bitmap_text()

    def _sync_bitmap_text(self):
        if not self.use_bitmap_dials or not hasattr(self, "bitmap_text"):
            return
        five = self.dial_state["five"]
        week = self.dial_state["week"]
        if self.show_five_hour_usage:
            self.canvas.itemconfigure(self.bitmap_text["five_label"], text=five.get("label", "G5"))
            self.canvas.itemconfigure(self.bitmap_text["five_value"], text=five.get("text", "--"))
            self.canvas.itemconfigure(self.bitmap_text["five_reset"], text=trim_text(five.get("reset_text", ""), 6))
        self.canvas.itemconfigure(self.bitmap_text["week_label"], text=week.get("label", "G7"))
        self.canvas.itemconfigure(self.bitmap_text["week_value"], text=week.get("text", "--"))
        self.canvas.itemconfigure(self.bitmap_text["week_reset"], text=trim_text(week.get("reset_text", ""), 6))
        if self.show_kimi_usage:
            kimi_five = self.dial_state["kimi_five"]
            kimi_week = self.dial_state["kimi_week"]
            for key, state in (("kimi_five", kimi_five), ("kimi_week", kimi_week)):
                self.canvas.itemconfigure(self.bitmap_text[f"{key}_label"], text=state.get("label", "K5" if key == "kimi_five" else "K7"))
                self.canvas.itemconfigure(self.bitmap_text[f"{key}_value"], text=state.get("text", "--"))
                self.canvas.itemconfigure(self.bitmap_text[f"{key}_reset"], text=trim_text(state.get("reset_text", ""), 6))

    def _draw_bitmap_gauge(self, draw, cx, cy, radius, percent, scale):
        cx *= scale
        cy *= scale
        radius *= scale
        percent = max(0.0, min(100.0, float(percent or 0.0)))
        width = 3 * scale
        bg_points = self._bitmap_gauge_arc_points(cx, cy, radius, 0.0, 1.0)
        self._draw_round_line(draw, bg_points, UI_TRACK, width)

        segment_count = 48
        visible_segments = int(math.ceil(segment_count * percent / 100))
        for index in range(visible_segments):
            start_ratio = index / segment_count
            end_ratio = min(1.0, (index + 0.72) / segment_count)
            points = self._bitmap_gauge_arc_points(cx, cy, radius, start_ratio, end_ratio)
            self._draw_round_line(draw, points, self._metric_gradient_color((index + 1) / segment_count), width)

        angle = math.radians(205 - 230 * percent / 100)
        length = radius - 8 * scale
        x2 = cx + length * math.cos(angle)
        y2 = cy - length * math.sin(angle)
        draw.line([cx, cy, x2, y2], fill=UI_NEEDLE, width=2 * scale)
        hub = 2.5 * scale
        draw.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=UI_NEEDLE)

    def _draw_bitmap_ring(self, draw, cx, cy, radius, state, color, timer_color, scale):
        cx *= scale
        cy *= scale
        radius *= scale
        inner_radius = radius - 6 * scale
        outer_box = [cx - radius, cy - radius, cx + radius, cy + radius]
        inner_box = [cx - inner_radius, cy - inner_radius, cx + inner_radius, cy + inner_radius]
        draw.ellipse(inner_box, fill=WIDGET_BG)

        reset_fraction = state.get("reset_fraction")
        if reset_fraction is not None:
            reset_fraction = max(0.0, min(1.0, float(reset_fraction)))
            if reset_fraction >= 0.999:
                draw.ellipse(inner_box, fill=timer_color)
            elif reset_fraction > 0:
                draw.polygon(self._timer_slice_points(cx, cy, inner_radius, reset_fraction), fill=timer_color)

        draw.arc(outer_box, start=0, end=360, fill=UI_TRACK, width=4 * scale)
        percent = state.get("percent")
        if percent is not None:
            percent = max(0.0, min(100.0, float(percent)))
            self._draw_round_arc(draw, cx, cy, radius, 0, percent, color, 4 * scale)

    def _draw_round_arc(self, draw, cx, cy, radius, start_percent, end_percent, fill, width):
        points = self._arc_points(cx, cy, radius, start_percent, end_percent)
        self._draw_round_line(draw, points, fill, width)

    def _draw_round_line(self, draw, points, fill, width):
        if len(points) >= 2:
            draw.line(points, fill=fill, width=width, joint="curve")
            cap_radius = width / 2
            for x, y in (points[0], points[-1]):
                draw.ellipse([x - cap_radius, y - cap_radius, x + cap_radius, y + cap_radius], fill=fill)

    def _arc_points(self, cx, cy, radius, start_percent, end_percent):
        start_percent = max(0.0, min(100.0, float(start_percent)))
        end_percent = max(0.0, min(100.0, float(end_percent)))
        if end_percent <= start_percent:
            angle = math.radians(90 - 360 * start_percent / 100)
            return [(cx + radius * math.cos(angle), cy - radius * math.sin(angle))]
        steps = max(6, int(120 * (end_percent - start_percent) / 100))
        points = []
        for i in range(steps + 1):
            percent = start_percent + (end_percent - start_percent) * i / steps
            angle = math.radians(90 - 360 * percent / 100)
            points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
        return points

    def _timer_slice_coords(self, cx, cy, radius, fraction):
        coords = []
        for x, y in self._timer_slice_points(cx, cy, radius, fraction):
            coords.extend([x, y])
        return coords

    def _timer_slice_points(self, cx, cy, radius, fraction):
        fraction = max(0.0, min(1.0, float(fraction or 0.0)))
        if fraction <= 0:
            return [(cx, cy), (cx, cy), (cx, cy)]
        steps = max(4, int(120 * fraction))
        points = [(cx, cy)]
        for i in range(steps + 1):
            progress = fraction * i / steps
            angle = math.radians(90 - 360 * progress)
            points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
        return points

    def _bitmap_gauge_arc_points(self, cx, cy, radius, start_ratio, end_ratio):
        start_ratio = max(0.0, min(1.0, float(start_ratio)))
        end_ratio = max(0.0, min(1.0, float(end_ratio)))
        if end_ratio <= start_ratio:
            angle = math.radians(205 - 230 * start_ratio)
            return [(cx + radius * math.cos(angle), cy - radius * math.sin(angle))]
        steps = max(5, int(96 * (end_ratio - start_ratio)))
        points = []
        for i in range(steps + 1):
            ratio = start_ratio + (end_ratio - start_ratio) * i / steps
            angle = math.radians(205 - 230 * ratio)
            points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
        return points

    def _update_system_metrics(self):
        if self.stop_event.is_set():
            return
        sample = self.system_sampler.sample()
        self._set_metric_target(self.cpu_gauge, sample.get("cpu"))
        self._set_metric_target(self.mem_gauge, sample.get("memory"))
        self._set_metric_target(self.gpu_gauge, sample.get("gpu"))
        interval = max(1, int(self.cfg.get("system_poll_seconds") or 1))
        self.root.after(interval * 1000, self._update_system_metrics)

    def _set_metric_target(self, gauge, percent):
        if percent is None:
            gauge["target"] = None
        else:
            gauge["target"] = max(0.0, min(100.0, float(percent)))

    def _animate_metric_gauges(self):
        if self.stop_event.is_set():
            return
        self._animate_metric_gauge(self.cpu_gauge)
        self._animate_metric_gauge(self.mem_gauge)
        self._animate_metric_gauge(self.gpu_gauge)
        if self.use_bitmap_dials:
            self._render_bitmap_dials()
        delay = max(50, int(self.cfg.get("gauge_animation_ms") or 100))
        self.root.after(delay, self._animate_metric_gauges)

    def _animate_metric_gauge(self, gauge):
        target = gauge.get("target")
        if target is None:
            target = 0.0

        display = float(gauge.get("display") or 0.0)
        display += (target - display) * 0.25
        if abs(target - display) < 0.15:
            display = target
        gauge["display"] = display
        if self.use_bitmap_dials:
            return
        visible_segments = int(math.ceil(gauge["segment_count"] * display / 100))
        if visible_segments != gauge.get("visible_segments"):
            for index, segment in enumerate(gauge["segments"]):
                state = "normal" if index < visible_segments else "hidden"
                self.canvas.itemconfigure(segment, state=state)
            gauge["visible_segments"] = visible_segments
        x2, y2 = self._gauge_needle_point(gauge, display)
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
        if (self.cfg.get("data_source") or "codex_cli").lower() == "web":
            self.menu.add_command(label="Open collector page", command=lambda: show_collector(self.port))
        else:
            self.menu.add_command(label="Open usage page", command=open_usage_page)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.close)

    def _bind_events(self):
        self.canvas.tag_bind("close", "<Button-1>", lambda event: self.close())
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag_window)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Button-3>", self._show_menu)
        if (self.cfg.get("data_source") or "codex_cli").lower() == "web":
            self.canvas.bind("<Double-Button-1>", lambda event: show_collector(self.port))
        else:
            self.canvas.bind("<Double-Button-1>", lambda event: open_usage_page())

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
        nx, ny = self._clamp_window_position(nx, ny)
        self.root.geometry(f"+{nx}+{ny}")

    def _end_drag(self, event):
        if not self.drag:
            return
        self.drag = None
        x, y = self._clamp_window_position(self.root.winfo_x(), self.root.winfo_y())
        self.root.geometry(f"+{x}+{y}")
        self.cfg["widget_x"] = x
        self.cfg["widget_y"] = y
        save_config(self.cfg)

    def _start_worker(self):
        thread = threading.Thread(target=self._worker, name="collector", daemon=True)
        thread.start()

    def _add_kimi_usage(self, payload):
        if not self.show_kimi_usage:
            return payload
        try:
            kimi_five, kimi_week = parse_kimi_usage(self.kimi_client.query())
            payload["kimi_five"] = kimi_five
            payload["kimi_week"] = kimi_week
        except Exception as exc:
            log(f"Kimi usage query failed: {exc}")
            payload["kimi_error"] = str(exc)
            payload["kimi_five"] = {"value": "Error", "detail": str(exc), "percent": None}
            payload["kimi_week"] = {"value": "Error", "detail": str(exc), "percent": None}
        return payload

    def _worker(self):
        first_poll = True
        while not self.stop_event.is_set():
            try:
                source = (self.cfg.get("data_source") or "codex_cli").lower()
                if source not in ("codex_cli", "web"):
                    raise RuntimeError("data_source must be 'codex_cli' or 'web'.")
                if source == "codex_cli":
                    payload = collect_codex_cli_usage(self.cfg)
                    self.events.put(self._add_kimi_usage(payload))
                    first_poll = False
                    wait_for = max(5, int(self.cfg.get("poll_seconds") or 30))
                    deadline = time.time() + wait_for
                    while time.time() < deadline and not self.stop_event.is_set():
                        if self.refresh_event.wait(0.2):
                            self.refresh_event.clear()
                            break
                    continue

                self.port = ensure_browser(self.cfg)
                self.browser_started = True
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
                self.events.put(self._add_kimi_usage(payload))
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
        self.canvas.itemconfigure(self.status_dot, fill=STATUS_WAIT)

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
            self.canvas.itemconfigure(self.status_dot, fill=STATUS_ERROR)
            self._set_period("five", {"value": "Error", "detail": trim_text(item.get("error", ""), 58), "percent": None})
            self._set_period("week", {"value": "Error", "detail": trim_text(item.get("error", ""), 58), "percent": None})
            return
        if item.get("login"):
            self.canvas.itemconfigure(self.status_dot, fill=STATUS_WAIT)
            self._set_period("five", {"value": "Login needed", "detail": "Finish ChatGPT login in the Edge window", "percent": None})
            self._set_period("week", item["week"])
        else:
            self.canvas.itemconfigure(self.status_dot, fill=STATUS_WAIT if item.get("kimi_error") else STATUS_OK)
            self._set_period("five", item["five"])
            self._set_period("week", item["week"])
        if self.show_kimi_usage:
            self._set_period("kimi_five", item.get("kimi_five") or {"value": "Waiting", "percent": None})
            self._set_period("kimi_week", item.get("kimi_week") or {"value": "Waiting", "percent": None})

    def _set_period(self, key, data):
        if key == "five":
            ring, color = (None if self.use_bitmap_dials else self.five_ring), GPT_FIVE
        elif key == "week":
            ring, color = (None if self.use_bitmap_dials else self.week_ring), GPT_WEEK
        elif key == "kimi_five":
            ring, color = (None if self.use_bitmap_dials else self.kimi_five_ring), KIMI_FIVE
        else:
            ring, color = (None if self.use_bitmap_dials else self.kimi_week_ring), KIMI_WEEK
        pct = data.get("percent")
        if pct is None:
            text = self._compact_status_text(data.get("value", ""))
            progress = 0
            fill = UI_TRACK
        else:
            pct = max(0, min(100, float(pct)))
            text = f"{pct:.0f}%"
            progress = pct
            fill = color
        reset_fraction = data.get("reset_fraction")
        reset_text = data.get("reset_text", "")
        if self.use_bitmap_dials:
            self.dial_state[key]["percent"] = pct if pct is not None else None
            self.dial_state[key]["text"] = text
            self.dial_state[key]["reset_text"] = reset_text
            self.dial_state[key]["reset_fraction"] = reset_fraction
            self._render_bitmap_dials()
            return
        if ring is None:
            return
        self.canvas.coords(
            ring["ring"],
            *self._ring_points(ring["cx"], ring["cy"], ring["radius"], progress),
        )
        self.canvas.itemconfigure(ring["ring"], fill=fill)
        self.canvas.itemconfigure(ring["value"], text=text)

        if reset_fraction is None:
            reset_fraction = 0
        else:
            reset_fraction = max(0, min(1, float(reset_fraction)))
        self.canvas.coords(
            ring["timer_fill"],
            *self._timer_slice_coords(ring["cx"], ring["cy"], ring["timer_radius"], reset_fraction),
        )
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
        self.system_sampler.close()
        self.kimi_client.close()
        if self.browser_started and self.cfg.get("close_edge_on_exit"):
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
    if "--json" in sys.argv:
        result = collect_codex_cli_usage(cfg)
        if cfg.get("show_kimi_usage", True):
            kimi_client = KimiUsageClient(cfg)
            try:
                kimi_five, kimi_week = parse_kimi_usage(kimi_client.query())
                result["kimi_five"] = kimi_five
                result["kimi_week"] = kimi_week
            finally:
                kimi_client.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    app = UsageWidget()
    app.run()


if __name__ == "__main__":
    main()
