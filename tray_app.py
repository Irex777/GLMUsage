"""GLM usage tray icon.

Polls the z.ai monitor API and shows, in the macOS menu bar:
    <token%> · <time until the token quota resets>

Menu:
    Tokens: <pct>% · <N> left
    resets <day HH:MM>
    ──
    Tools: <pct>% · <N> left
    resets <day HH:MM>
    ──
    Refresh now
    Settings…
    Quit
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone, timedelta

import requests
import rumps
from AppKit import (
    NSApplication, NSImage, NSImageView, NSWindow, NSTextField, NSButton, NSColor,
    NSFont, NSMakeRect, NSSwitch, NSTextAlignmentCenter,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered,
    NSControlStateValueOn, NSControlStateValueOff, NSBezelStyleRounded,
)
from Foundation import NSObject
import objc

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ── configuration ────────────────────────────────────────────────────────────

API_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
POLL_INTERVAL = 60  # seconds
TOOLS_LIMIT = 1000  # tool calls per 5-hour rolling window
WINDOW_DURATION = timedelta(hours=5)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, ".tray_config.json")
WINDOW_FILE = os.path.join(HERE, ".window_state.json")

LOGIN_AGENT_LABEL = "com.glm.usage"
LOGIN_AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LOGIN_AGENT_LABEL}.plist")
APP_EXECUTABLE = "/Applications/GLMUsage.app/Contents/MacOS/glmusage"

# Run as a menu-bar accessory (no Dock icon) before rumps builds the app.
NSApplication.sharedApplication().setActivationPolicy_(1)

ICON_PATH = next(
    (p for p in (
        os.path.join(HERE, "icon_256x256.png"),
        "/Applications/GLMUsage.app/Contents/Resources/GLMUsage.icns",
    ) if os.path.exists(p)),
    None,
)


def _set_app_icon():
    """Replace the default Python rocket icon used in dialogs and the dock."""
    if not ICON_PATH:
        return
    img = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
    if img is not None:
        NSApplication.sharedApplication().setApplicationIconImage_(img)


def _read_json(path):
    """Return parsed JSON from path, or None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    """Write data as JSON to path, ignoring write errors."""
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_config():
    """Load saved configuration (API key, etc.)."""
    return _read_json(CONFIG_FILE) or {}


def save_config(config):
    """Persist configuration."""
    _write_json(CONFIG_FILE, config)


def get_api_key():
    """Return the configured API key, falling back to the environment."""
    return load_config().get("api_key") or GLM_API_KEY


# ── launch-at-login (LaunchAgent) ────────────────────────────────────────────

def get_login_item_enabled():
    """Return True if the launch-at-login agent is registered."""
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        return LOGIN_AGENT_LABEL in result.stdout
    except OSError:
        return False


def set_login_item(enabled):
    """Register or remove the launch-at-login agent."""
    os.makedirs(os.path.dirname(LOGIN_AGENT_PATH), exist_ok=True)
    if enabled:
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LOGIN_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{APP_EXECUTABLE}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
        with open(LOGIN_AGENT_PATH, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", LOGIN_AGENT_PATH], capture_output=True)
    else:
        subprocess.run(["launchctl", "unload", LOGIN_AGENT_PATH], capture_output=True)
        if os.path.exists(LOGIN_AGENT_PATH):
            os.remove(LOGIN_AGENT_PATH)


# ── 5-hour rolling window tracking ───────────────────────────────────────────

def _get_window_reset_time(remaining):
    """Return when the 5-hour tools window resets.

    The window starts on the first API call. The API doesn't expose the start
    time, so we track it locally and roll it over once five hours elapse.
    """
    now = datetime.now(timezone.utc)
    state = _read_json(WINDOW_FILE)

    if not state:
        # First run: assume the window started about an hour ago.
        state = {"window_start": (now - timedelta(hours=1)).isoformat(),
                 "remaining": remaining}
        _write_json(WINDOW_FILE, state)

    reset = datetime.fromisoformat(state["window_start"]) + WINDOW_DURATION
    if reset < now:
        state = {"window_start": now.isoformat(), "remaining": remaining}
        _write_json(WINDOW_FILE, state)
        reset = now + WINDOW_DURATION
    elif abs(remaining - state.get("remaining", 0)) > 5:
        state["remaining"] = remaining
        _write_json(WINDOW_FILE, state)

    return reset


# ── z.ai API ─────────────────────────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"Bearer {get_api_key()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def fetch_usage():
    """Return a dict of metrics for the 5h window + token quota."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("API key not configured. Open Settings to set your API key.")

    r = requests.get(API_URL, headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()

    # New API returns {code, msg, data: {limits: [...]}}
    if not data.get("success"):
        raise RuntimeError(f"API error: {data.get('msg', 'Unknown error')}")

    limits = (data.get("data") or {}).get("limits") or []

    def find(t):
        return next((l for l in limits if l.get("type") == t), {}) or {}

    tl = find("TIME_LIMIT")
    tk = find("TOKENS_LIMIT")
    if not tl:
        raise RuntimeError("TIME_LIMIT window not found in response")

    now = datetime.now(timezone.utc)
    time_value = tl.get("currentValue") or 0
    time_remaining = tl.get("remaining") or 0

    # Calculate 5-hour rolling window reset time
    window_reset_dt = _get_window_reset_time(time_remaining)
    time_reset_seconds = (window_reset_dt - now).total_seconds() if window_reset_dt else None

    # The API doesn't return a total, so derive it from used + remaining.
    time_limit = time_value + time_remaining if time_remaining > 0 else 1
    time_pct = round((time_value / time_limit) * 100)

    # Token reset time from API (milliseconds -> datetime)
    token_reset_ts = tk.get("nextResetTime")
    token_reset_dt = None
    token_reset_seconds = None
    if token_reset_ts:
        token_reset_dt = datetime.fromtimestamp(token_reset_ts / 1000, tz=timezone.utc)
        token_reset_seconds = (token_reset_dt - now).total_seconds()

    return {
        "active": time_value > 0,
        "time_value": time_value,
        "time_pct": time_pct,
        "time_left": tl.get("remaining"),
        "time_reset_seconds": time_reset_seconds,
        "time_reset_dt": window_reset_dt,
        "time_models": [(d.get("modelCode"), d.get("usage", 0)) for d in (tl.get("usageDetails") or [])],
        "token_pct": tk.get("percentage") if tk else None,
        "token_left": tk.get("remaining") if tk else None,
        "token_reset_dt": token_reset_dt,
        "token_reset_seconds": token_reset_seconds,
    }


def fmt_countdown(seconds):
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "now"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    if d > 0:
        return f"{d}d{h}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _bar(ratio, width=8):
    ratio = max(0.0, min(1.0, ratio or 0))
    filled = int(round(ratio * width))
    return "▰" * filled + "▱" * (width - filled)


def _fmt_tokens(n):
    """Human-readable token count: 1234 -> '1.2K', 3_400_000 -> '3.4M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


# ── Claude Code local usage (Pro/Max) ────────────────────────────────────────
#
# Claude Pro/Max has no public usage API, so we read the token counts Claude
# Code writes to ~/.claude/projects/**/*.jsonl and sum the rolling 5h window
# (Claude's limits reset every 5 hours). This yields absolute tokens used — not
# a percentage, since Anthropic does not publish the consumer plan limits.

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_WINDOW = timedelta(hours=5)


def _read_claude_events(since):
    """Yield (timestamp, tokens) for assistant messages newer than `since`."""
    for root, _dirs, files in os.walk(CLAUDE_PROJECTS_DIR):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                if datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc) < since:
                    continue
                f = open(path)
            except OSError:
                continue
            with f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    ts_raw = rec.get("timestamp")
                    if not usage or not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                    yield ts, (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)


def fetch_claude_usage():
    """Return the current Claude Code 5h block's usage, or None if idle.

    Claude Pro/Max limits reset on a 5-hour window that begins with your first
    message. We group recent messages into 5h blocks (the same model as
    ccusage) and report the active block: {tokens, messages, window_start,
    reset_dt}. Tokens are billable input+output (cache excluded).
    """
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return None

    now = datetime.now(timezone.utc)
    # Look back two windows so a block that started up to ~10h ago is captured.
    events = sorted(_read_claude_events(now - 2 * CLAUDE_WINDOW))
    if not events:
        return None

    # Walk forward, opening a new block whenever a message lands beyond the
    # current block's 5h span. The last block is the active one.
    block_start = events[0][0]
    block_tokens = 0
    block_msgs = 0
    for ts, tok in events:
        if ts - block_start >= CLAUDE_WINDOW:
            block_start = ts
            block_tokens = 0
            block_msgs = 0
        block_tokens += tok
        block_msgs += 1

    reset_dt = block_start + CLAUDE_WINDOW
    if reset_dt < now:  # active block already elapsed -> nothing live
        return None

    return {
        "tokens": block_tokens,
        "messages": block_msgs,
        "window_start": block_start,
        "reset_dt": reset_dt,
    }


# ── custom settings window ───────────────────────────────────────────────────

def _label(text, frame, size=13, color=None, bold=False, align=None):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    f.setTextColor_(color or NSColor.labelColor())
    if align is not None:
        f.setAlignment_(align)
    return f


class SettingsWindow(NSObject):
    """A clean, native dark settings panel — no Python rocket, no NSAlert."""

    def initWithApp_(self, app):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self.app = app
        self.window = None
        return self

    def show(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return

        W, H = 420, 300
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("GLM Usage — Settings")
        win.setReleasedWhenClosed_(False)
        win.center()

        content = win.contentView()

        # Header
        content.addSubview_(_label("Settings", NSMakeRect(24, H - 56, W - 48, 28),
                                   size=20, bold=True))
        content.addSubview_(_label("Configure your GLM API access",
                                   NSMakeRect(24, H - 80, W - 48, 18),
                                   size=12, color=NSColor.secondaryLabelColor()))

        # API Key section
        content.addSubview_(_label("API KEY", NSMakeRect(24, H - 120, W - 48, 16),
                                   size=10, color=NSColor.tertiaryLabelColor(), bold=True))

        field = NSTextField.alloc().initWithFrame_(NSMakeRect(24, H - 152, W - 48, 26))
        field.setStringValue_(load_config().get("api_key", "") or "")
        field.setPlaceholderString_("Paste your key from z.ai…")
        field.setFont_(NSFont.systemFontOfSize_(12))
        content.addSubview_(field)
        self.field = field

        hint = _label("Get your key at https://z.ai/",
                      NSMakeRect(24, H - 174, W - 48, 16),
                      size=11, color=NSColor.tertiaryLabelColor())
        content.addSubview_(hint)

        # Launch at login row
        content.addSubview_(_label("STARTUP", NSMakeRect(24, H - 212, W - 48, 16),
                                   size=10, color=NSColor.tertiaryLabelColor(), bold=True))
        content.addSubview_(_label("Launch GLM Usage at login",
                                   NSMakeRect(24, H - 238, 280, 20), size=13))

        switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(W - 24 - 50, H - 240, 50, 24))
        switch.setState_(NSControlStateValueOn if get_login_item_enabled()
                         else NSControlStateValueOff)
        switch.setTarget_(self)
        switch.setAction_("toggleLogin:")
        content.addSubview_(switch)
        self.switch = switch

        # Buttons
        save = NSButton.alloc().initWithFrame_(NSMakeRect(W - 24 - 100, 20, 100, 32))
        save.setTitle_("Save")
        save.setBezelStyle_(NSBezelStyleRounded)
        save.setKeyEquivalent_("\r")
        save.setTarget_(self)
        save.setAction_("save:")
        content.addSubview_(save)

        about = NSButton.alloc().initWithFrame_(NSMakeRect(24, 20, 100, 32))
        about.setTitle_("About")
        about.setBezelStyle_(NSBezelStyleRounded)
        about.setTarget_(self)
        about.setAction_("about:")
        content.addSubview_(about)

        self.window = win
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def toggleLogin_(self, sender):
        enabled = sender.state() == NSControlStateValueOn
        set_login_item(enabled)

    def save_(self, _sender):
        new_key = self.field.stringValue().strip()
        save_config({"api_key": new_key} if new_key else {})
        self.app.refresh()
        self.window.close()

    def about_(self, _sender):
        self.app.show_about()


ABOUT_TEXT = (
    "Track your GLM Coding Plan usage from the menu bar.\n\n"
    "TOKENS\nGLM model usage (glm-4, glm-5). Measured in tokens processed.\n\n"
    "TOOLS\nGLM tool calls (search, web-reader, zread).\n"
    "1000 calls per 5-hour rolling window.\n\n"
    "CLAUDE\nIf you use Claude Code, shows tokens used in the current\n"
    "5-hour window (read from local logs). No percentage — Anthropic\n"
    "doesn't publish Pro/Max limits.\n\n"
    "Get your GLM API key at https://z.ai/\n\n"
    "Share GLMUsage.app with friends — each person uses their own key."
)


class AboutWindow(NSObject):
    """Native About panel — no NSAlert, no Python rocket."""

    def init(self):
        self = objc.super(AboutWindow, self).init()
        if self is None:
            return None
        self.window = None
        return self

    def show(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return

        W, H = 440, 460
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("About GLM Usage")
        win.setReleasedWhenClosed_(False)
        win.center()
        content = win.contentView()

        # App icon
        if ICON_PATH:
            img = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
            if img is not None:
                iv = NSImageView.alloc().initWithFrame_(
                    NSMakeRect(W / 2 - 32, H - 96, 64, 64))
                iv.setImage_(img)
                content.addSubview_(iv)

        content.addSubview_(_label("GLM Usage Tracker",
                                   NSMakeRect(24, H - 128, W - 48, 24),
                                   size=18, bold=True, align=NSTextAlignmentCenter))

        body = _label(ABOUT_TEXT, NSMakeRect(28, 64, W - 56, H - 200), size=12,
                      color=NSColor.secondaryLabelColor())
        body.cell().setWraps_(True)
        content.addSubview_(body)

        ok = NSButton.alloc().initWithFrame_(NSMakeRect(W - 24 - 100, 20, 100, 32))
        ok.setTitle_("Got it")
        ok.setBezelStyle_(NSBezelStyleRounded)
        ok.setKeyEquivalent_("\r")
        ok.setTarget_(self)
        ok.setAction_("close:")
        content.addSubview_(ok)

        self.window = win
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def close_(self, _sender):
        self.window.close()


class GLMUsageApp(rumps.App):
    def __init__(self):
        super().__init__("…")
        _set_app_icon()
        self._settings_window = SettingsWindow.alloc().initWithApp_(self)
        self._about_window = AboutWindow.alloc().init()
        self._set([rumps.MenuItem("Loading…")])

    @rumps.timer(POLL_INTERVAL)
    def _tick(self, _sender):
        self.update()

    def refresh(self, _sender=None):
        self.update()

    def quit(self, _sender=None):
        NSApplication.sharedApplication().terminate_(None)
        sys.exit(0)

    def show_about(self, _sender=None):
        """Open the custom About window."""
        self._about_window.show()

    def show_settings(self, _sender=None):
        """Open the custom settings window."""
        self._settings_window.show()

    def _set(self, items):
        # rumps' menu setter *appends* rather than replaces, so clear first
        # (otherwise entries pile up on every refresh).
        self._menu.clear()
        self.menu = list(items) + [
            None,
            rumps.MenuItem("Refresh now", callback=self.refresh),
            rumps.MenuItem("Settings…", callback=self.show_settings),
            None,
            rumps.MenuItem("Quit", callback=self.quit),
        ]

    def update(self):
        try:
            d = fetch_usage()
        except Exception as e:
            self.title = "GLM ⚠"
            self._set([rumps.MenuItem(f"Error: {e}")])
            return

        # Title: GLM token % + countdown to its reset (the headline number).
        token_pct = d["token_pct"] or 0
        token_reset_secs = d.get("token_reset_seconds")
        if token_pct > 0 and token_reset_secs and token_reset_secs > 0:
            self.title = f"{token_pct}% · {fmt_countdown(token_reset_secs)}"
        else:
            self.title = f"{token_pct}%"

        items = []
        items += self._glm_rows(d)
        claude = fetch_claude_usage()
        if claude:
            items.append(None)
            items += self._claude_rows(claude)

        self._set(items)

    @staticmethod
    def _reset_label(dt):
        return f"resets {dt.astimezone().strftime('%a %H:%M')}"

    def _glm_rows(self, d):
        """Menu rows for the GLM token + tool quotas."""
        rows = []
        if d["token_pct"] is not None:
            row = f"Tokens: {_bar(d['token_pct'] / 100)} {d['token_pct']}%"
            if isinstance(d["token_left"], (int, float)):
                row += f" · {d['token_left']} left"
            rows.append(rumps.MenuItem(row))
        else:
            rows.append(rumps.MenuItem("Tokens: —"))
        if d.get("token_reset_dt"):
            rows.append(rumps.MenuItem(self._reset_label(d["token_reset_dt"])))

        rows.append(None)
        row = f"Tools: {_bar(d['time_pct'] / 100)} {d['time_pct']}%"
        if isinstance(d["time_left"], (int, float)):
            row += f" · {d['time_left']} left"
        rows.append(rumps.MenuItem(row))
        if d["active"] and d["time_reset_dt"] is not None:
            rows.append(rumps.MenuItem(self._reset_label(d["time_reset_dt"])))
        return rows

    def _claude_rows(self, c):
        """Menu rows for local Claude Code (Pro/Max) usage in the active 5h block."""
        rows = [rumps.MenuItem(
            f"Claude: {_fmt_tokens(c['tokens'])} tokens · {c['messages']} msgs")]
        if c.get("reset_dt"):
            rows.append(rumps.MenuItem(self._reset_label(c["reset_dt"])))
        return rows


if __name__ == "__main__":
    app = GLMUsageApp()
    app.update()
    app.run()
