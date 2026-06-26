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
HISTORY_FILE = os.path.join(HERE, ".glm_history.json")
HISTORY_MAX = 2880  # snapshots kept (~2 days at one per minute)
LEDGER_FILE = os.path.join(HERE, ".usage_ledger.json")

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


def record_glm_history(d):
    """Append a usage snapshot to the GLM history log.

    GLM's API exposes token usage only as a percentage (no raw token count),
    so this records the percentage over time plus tool-call counts — not a true
    token tally, which the API doesn't provide.
    """
    snap = {
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "token_pct": d.get("token_pct"),
        "tool_pct": d.get("time_pct"),
        "tool_calls": d.get("time_value"),
    }
    history = _read_json(HISTORY_FILE) or []
    # Skip if nothing changed since the last snapshot (avoid flat-line spam).
    if history:
        last = history[-1]
        if (last.get("token_pct") == snap["token_pct"]
                and last.get("tool_calls") == snap["tool_calls"]):
            return
    history.append(snap)
    _write_json(HISTORY_FILE, history[-HISTORY_MAX:])


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


def _fmt_cost(d):
    """Human-readable dollar amount: 0.42 -> '$0.42', 12.5 -> '$12.50'."""
    if d >= 100:
        return f"${d:,.0f}"
    return f"${d:.2f}"


# ── Claude Code local usage (Pro/Max) ────────────────────────────────────────
#
# Claude Pro/Max has no public usage API, so we read the token counts Claude
# Code writes to ~/.claude/projects/**/*.jsonl and sum the rolling 5h window
# (Claude's limits reset every 5 hours). This yields absolute tokens used — not
# a percentage, since Anthropic does not publish the consumer plan limits.

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_WINDOW = timedelta(hours=5)
CLAUDE_WARN_RATIO = 0.9  # title shows ⚠️ at/above this fraction of the limit

# Public API list price per 1M tokens, by model-family keyword.
# (input, output, cache_read) — used to estimate "as if you paid per token" cost.
# Anthropic cache writes bill ~1.25x input; GLM has no separate write rate.
PRICES = {
    # Anthropic
    "fable":  (10.0, 50.0, 1.0),
    "opus":   (5.0, 25.0, 0.5),
    "sonnet": (3.0, 15.0, 0.3),
    "haiku":  (1.0, 5.0, 0.1),
    # Zhipu GLM (z.ai published rates)
    "glm-5":  (1.40, 4.40, 0.26),   # glm-5.x family
    "glm-4":  (0.43, 1.74, 0.10),   # glm-4.x family
}
ANTHROPIC_DEFAULT = (5.0, 25.0, 0.5)  # unknown Anthropic model -> Opus-tier


def _is_glm(model):
    return "glm" in (model or "").lower()


def _price(model):
    """Return (input, output, cache_read) $/1M for a model id, by keyword."""
    m = (model or "").lower()
    # Longest keys first so 'glm-5' wins over a bare 'glm' substring match.
    for key in sorted(PRICES, key=len, reverse=True):
        if key in m:
            return PRICES[key]
    return ANTHROPIC_DEFAULT


def _message_cost(model, usage):
    """Dollar cost of one assistant message at public per-token API rates."""
    in_rate, out_rate, cache_rate = _price(model)
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    write_rate = cache_rate if _is_glm(model) else in_rate * 1.25
    return (
        inp * in_rate
        + out * out_rate
        + cache_read * cache_rate
        + cache_write * write_rate
    ) / 1_000_000


def get_claude_limit():
    """Return the learned/calibrated Claude 5h token limit, or None if unknown."""
    val = load_config().get("claude_limit")
    return int(val) if val else None


def set_claude_limit(tokens):
    """Persist the Claude token limit (None clears it)."""
    cfg = load_config()
    if tokens:
        cfg["claude_limit"] = int(tokens)
    else:
        cfg.pop("claude_limit", None)
    save_config(cfg)


def _read_claude_events(since):
    """Yield (timestamp, tokens, cost) for assistant messages newer than `since`.

    `tokens` is billable input+output (cache excluded, for the quota number);
    `cost` is the public-API dollar price of the message (cache included).
    """
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
                    model = msg.get("model")
                    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                    yield ts, tokens, _message_cost(model, usage), _is_glm(model)


def _get_claude_window_bounds(now, offset_hours=0):
    """Calculate the current Claude 5-hour window boundaries based on calendar time.

    The Claude API uses fixed 5-hour windows, not user-activity-based windows.
    offset_hours allows configuring the window start offset (e.g., 7.17 for 07:10 UTC).

    Returns (window_start, reset_dt) as UTC datetime objects.
    """
    offset_seconds = offset_hours * 3600
    # Unix timestamp of now, adjusted by offset
    now_ts = now.timestamp()
    adjusted = now_ts - offset_seconds
    # Which 5-hour window number is this?
    window_num = int(adjusted // (5 * 3600))
    # Calculate the start of this window
    window_start_ts = window_num * 5 * 3600 + offset_seconds
    window_start = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
    reset_dt = window_start + CLAUDE_WINDOW
    return window_start, reset_dt


def fetch_claude_usage():
    """Return the current Claude Code 5h block's usage, or None if idle.

    Claude Pro/Max limits reset on a fixed 5-hour calendar window. We count
    tokens used within the current window (determined by the configured offset)
    and report usage. Tokens are billable input+output (cache excluded).

    Returns {tokens, messages, window_start, reset_dt, limit, pct}. `limit` is
    the learned ceiling (None until known); `pct` is the fraction used or None.
    Anthropic doesn't publish the limit, so we also keep an auto "high-water
    mark": the largest block seen without being cut off is a safe lower bound.
    """
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return None

    now = datetime.now(timezone.utc)
    cfg = load_config()
    # Get configured offset (hours since midnight UTC). Default 0 (00:00 UTC windows)
    offset_hours = cfg.get("claude_reset_offset", 0)

    window_start, reset_dt = _get_claude_window_bounds(now, offset_hours)

    # Only Claude (Anthropic) messages count toward the Claude window — GLM/local
    # models logged by the same coding tool are billed separately.
    events = sorted(e for e in _read_claude_events(window_start)
                    if not e[3])
    if not events:
        return {
            "tokens": 0,
            "messages": 0,
            "cost": 0.0,
            "window_start": window_start,
            "reset_dt": reset_dt,
            "limit": None,
            "calibrated": False,
            "seen_max": cfg.get("claude_seen_max", 0),
            "pct": None,
        }

    # Sum all tokens within the current window
    block_tokens = sum(tok for ts, tok, cost, _ in events)
    block_msgs = len(events)
    block_cost = sum(cost for ts, tok, cost, _ in events)

    # Track the largest block ever seen (a safe lower bound on the real limit),
    # but only show a percentage once the user has actually calibrated — a
    # high-water mark would otherwise always read 100% and look alarming.
    if block_tokens > cfg.get("claude_seen_max", 0):
        cfg["claude_seen_max"] = block_tokens
        save_config(cfg)

    calibrated = cfg.get("claude_limit")
    limit = int(calibrated) if calibrated else None
    pct = round(block_tokens / limit * 100) if limit else None

    return {
        "tokens": block_tokens,
        "messages": block_msgs,
        "cost": block_cost,
        "window_start": window_start,
        "reset_dt": reset_dt,
        "limit": limit,
        "calibrated": bool(calibrated),
        "seen_max": cfg.get("claude_seen_max", 0),
        "pct": pct,
    }


def fetch_glm_cost():
    """Real GLM cost from local coding-tool logs, priced at z.ai per-token rates.

    Unlike the GLM quota API (percentage only), the coding tool logs exact GLM
    token counts per message. We sum them over GLM's current 5h quota window and
    price each at its model's published API rate. Returns
    {tokens, messages, cost, window} or None if there's no GLM usage logged.
    """
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return None
    since = datetime.now(timezone.utc) - CLAUDE_WINDOW
    tokens = msgs = 0
    cost = 0.0
    for _ts, tok, c, is_glm in _read_claude_events(since):
        if is_glm:
            tokens += tok
            msgs += 1
            cost += c
    if not msgs:
        return None
    return {"tokens": tokens, "messages": msgs, "cost": cost, "window_h": 5}


def update_ledger():
    """Maintain a lifetime running total across all windows, deduped by uuid.

    Logs eventually get pruned, so a re-scan can't be trusted to see every
    message. We persist cumulative totals plus the set of seen record uuids, and
    only add messages we haven't counted before — so totals keep growing
    correctly even as old transcripts disappear.

    Returns the ledger dict: {glm: {...}, claude: {...}, since} where each side
    has tokens/messages/cost; `since` is when tracking began.
    """
    led = _read_json(LEDGER_FILE) or {
        "since": int(datetime.now(timezone.utc).timestamp()),
        "seen": [],
        "glm": {"tokens": 0, "messages": 0, "cost": 0.0},
        "claude": {"tokens": 0, "messages": 0, "cost": 0.0},
    }
    seen = set(led.get("seen", []))
    added = False

    for root, _dirs, files in os.walk(CLAUDE_PROJECTS_DIR):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            try:
                f = open(os.path.join(root, name))
            except OSError:
                continue
            with f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    uid = rec.get("uuid")
                    msg = rec.get("message")
                    if not uid or uid in seen or not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    seen.add(uid)
                    added = True
                    model = msg.get("model")
                    side = led["glm"] if _is_glm(model) else led["claude"]
                    side["tokens"] += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                    side["messages"] += 1
                    side["cost"] += _message_cost(model, usage)

    if added:
        # Cap the seen-set so the file can't grow without bound; pruning old
        # uuids only risks re-counting messages that are themselves long pruned.
        led["seen"] = list(seen)[-200_000:]
        _write_json(LEDGER_FILE, led)
    return led


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

        W, H = 420, 380
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

        # Claude reset offset section
        content.addSubview_(_label("CLAUDE QUOTA RESET", NSMakeRect(24, H - 212, W - 48, 16),
                                   size=10, color=NSColor.tertiaryLabelColor(), bold=True))
        content.addSubview_(_label("When does your 5h Claude quota window reset? (UTC time)",
                                   NSMakeRect(24, H - 232, W - 48, 16),
                                   size=11, color=NSColor.secondaryLabelColor()))

        offset_field = NSTextField.alloc().initWithFrame_(NSMakeRect(24, H - 262, 80, 26))
        offset = load_config().get("claude_reset_offset")
        offset_field.setStringValue_(str(offset) if offset is not None else "")
        offset_field.setPlaceholderString_("7.17")
        offset_field.setFont_(NSFont.systemFontOfSize_(12))
        content.addSubview_(offset_field)
        self.offset_field = offset_field

        content.addSubview_(_label("hours from midnight UTC (e.g., 7.17 = 07:10 UTC)",
                                   NSMakeRect(116, H - 258, W - 140, 20),
                                   size=11, color=NSColor.tertiaryLabelColor()))

        # Launch at login row
        content.addSubview_(_label("STARTUP", NSMakeRect(24, H - 292, W - 48, 16),
                                   size=10, color=NSColor.tertiaryLabelColor(), bold=True))
        content.addSubview_(_label("Launch GLM Usage at login",
                                   NSMakeRect(24, H - 318, 280, 20), size=13))

        switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(W - 24 - 50, H - 320, 50, 24))
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
        cfg = load_config()
        new_key = self.field.stringValue().strip()
        if new_key:
            cfg["api_key"] = new_key
        elif "api_key" in cfg:
            del cfg["api_key"]

        # Save Claude reset offset if provided
        offset_str = self.offset_field.stringValue().strip()
        if offset_str:
            try:
                offset = float(offset_str)
                cfg["claude_reset_offset"] = offset
            except ValueError:
                pass  # Invalid input, don't save
        elif "claude_reset_offset" in cfg:
            del cfg["claude_reset_offset"]

        save_config(cfg)
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
    "5-hour window (read from local logs). Anthropic doesn't publish\n"
    "Pro/Max limits, so when you actually get cut off, click\n"
    "⚑ \"Hit Claude limit just now\" — the app pins your limit and\n"
    "then shows a % bar and a ⚠️ warning as you near it.\n\n"
    "Configure your Claude reset offset in Settings if the displayed\n"
    "reset time doesn't match the actual API window.\n\n"
    "COST & HISTORY\nUsage History… shows what your GLM and Claude Code\n"
    "usage would cost at each provider's public per-token API rates,\n"
    "computed from the real token counts in your local coding-tool\n"
    "logs — plus GLM's quota-percentage history from the z.ai API.\n\n"
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

        W, H = 460, 580
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


def build_history_text():
    """Compose the usage-history report shown in the History window."""
    lines = []

    # GLM — real cost from local coding-tool logs (exact token counts), plus
    # the quota-percentage history from the z.ai API.
    lines.append("GLM USAGE  (current 5h window)")
    g = fetch_glm_cost()
    if g:
        lines.append(f"  Tokens used: {_fmt_tokens(g['tokens'])}  ·  {g['messages']} messages")
        lines.append(f"  If billed at z.ai API rates: {_fmt_cost(g['cost'])}")
        lines.append("  (Real GLM token counts from local logs.)")
    else:
        lines.append("  No GLM usage logged in the last 5 hours.")

    history = _read_json(HISTORY_FILE) or []
    if history:
        tok = [h["token_pct"] for h in history if h.get("token_pct") is not None]
        if tok:
            lines.append(f"  Quota (from z.ai API): now {tok[-1]}%, peak {max(tok)}%")

    # Claude — real token counts and "as if direct API" cost.
    lines.append("")
    lines.append("CLAUDE CODE  (current 5h window)")
    c = fetch_claude_usage()
    if c:
        lines.append(f"  Tokens used: {_fmt_tokens(c['tokens'])}  ·  {c['messages']} messages")
        lines.append(f"  If billed at public API rates: {_fmt_cost(c['cost'])}")
        lines.append("  (Real token counts from local logs; cache included.)")
    else:
        lines.append("  No Claude Code activity in the last 5 hours.")

    # Lifetime totals across all windows (cumulative, deduped).
    led = update_ledger()
    glm, cla = led["glm"], led["claude"]
    if glm["messages"] or cla["messages"]:
        lines.append("")
        lines.append("ALL-TIME  (every window, cumulative)")
        if glm["messages"]:
            lines.append(f"  GLM:    {_fmt_cost(glm['cost'])}"
                         f"  ({_fmt_tokens(glm['tokens'])} tok, {glm['messages']} msgs)")
        if cla["messages"]:
            lines.append(f"  Claude: {_fmt_cost(cla['cost'])}"
                         f"  ({_fmt_tokens(cla['tokens'])} tok, {cla['messages']} msgs)")
        lines.append(f"  Total:  {_fmt_cost(glm['cost'] + cla['cost'])} of API-rate usage")

    return "\n".join(lines)


class HistoryWindow(NSObject):
    """Native window showing GLM snapshot history and Claude API-equivalent cost."""

    def init(self):
        self = objc.super(HistoryWindow, self).init()
        if self is None:
            return None
        self.window = None
        self.body = None
        return self

    def show(self):
        if self.window is None:
            W, H = 480, 420
            style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
            win.setTitle_("Usage History")
            win.setReleasedWhenClosed_(False)
            win.center()
            content = win.contentView()

            content.addSubview_(_label("Usage History",
                                       NSMakeRect(24, H - 52, W - 48, 26),
                                       size=18, bold=True))
            body = _label("", NSMakeRect(24, 64, W - 48, H - 130), size=12,
                          color=NSColor.labelColor())
            body.cell().setWraps_(True)
            body.setFont_(NSFont.userFixedPitchFontOfSize_(11))
            content.addSubview_(body)
            self.body = body

            ok = NSButton.alloc().initWithFrame_(NSMakeRect(W - 24 - 100, 20, 100, 32))
            ok.setTitle_("Close")
            ok.setBezelStyle_(NSBezelStyleRounded)
            ok.setKeyEquivalent_("\r")
            ok.setTarget_(self)
            ok.setAction_("close:")
            content.addSubview_(ok)
            self.window = win

        self.body.setStringValue_(build_history_text())
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def close_(self, _sender):
        self.window.close()


class GLMUsageApp(rumps.App):
    def __init__(self):
        super().__init__("…")
        _set_app_icon()
        self._claude = None
        self._settings_window = SettingsWindow.alloc().initWithApp_(self)
        self._about_window = AboutWindow.alloc().init()
        self._history_window = HistoryWindow.alloc().init()
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

    def show_history(self, _sender=None):
        """Open the usage-history window (GLM snapshots + Claude API cost)."""
        self._history_window.show()

    def _set(self, items):
        # rumps' menu setter *appends* rather than replaces, so clear first
        # (otherwise entries pile up on every refresh).
        self._menu.clear()
        self.menu = list(items) + [
            None,
            rumps.MenuItem("Refresh now", callback=self.refresh),
            rumps.MenuItem("Usage History…", callback=self.show_history),
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

        record_glm_history(d)
        try:
            update_ledger()
        except Exception:
            pass  # ledger is best-effort; never block the refresh

        # Title: GLM token % + countdown to its reset (the headline number).
        token_pct = d["token_pct"] or 0
        token_reset_secs = d.get("token_reset_seconds")
        if token_pct > 0 and token_reset_secs and token_reset_secs > 0:
            title = f"{token_pct}% · {fmt_countdown(token_reset_secs)}"
        else:
            title = f"{token_pct}%"

        items = list(self._glm_rows(d))

        self._claude = fetch_claude_usage()
        if self._claude:
            items.append(None)
            items += self._claude_rows(self._claude)
            # Warn in the title bar when the Claude 5h window is nearly spent.
            pct = self._claude.get("pct")
            if pct is not None and pct >= CLAUDE_WARN_RATIO * 100:
                title += " ⚠️"

        self.title = title
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

        # Real GLM cost (from local logs) right under the token reset row.
        g = fetch_glm_cost()
        if g:
            rows.append(rumps.MenuItem(f"API cost: {_fmt_cost(g['cost'])} (this window)"))

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
        used = _fmt_tokens(c["tokens"])
        limit = c.get("limit")
        if limit:
            head = f"Claude: {_bar(c['tokens'] / limit)} {c['pct']}% · {used}/{_fmt_tokens(limit)}"
        else:
            head = f"Claude: {used} tokens · {c['messages']} msgs"
        rows = [rumps.MenuItem(head)]

        # "If you paid the public API" cost for this 5h block.
        rows.append(rumps.MenuItem(f"API cost: {_fmt_cost(c['cost'])} (this window)"))

        if c.get("reset_dt"):
            rows.append(rumps.MenuItem(self._reset_label(c["reset_dt"])))

        # Calibration: when you actually get cut off, pin the limit to right now.
        rows.append(rumps.MenuItem("⚑ Hit Claude limit just now",
                                   callback=self.calibrate_claude))
        if c.get("calibrated"):
            rows.append(rumps.MenuItem("Reset learned limit",
                                       callback=self.reset_claude_limit))
        return rows

    def calibrate_claude(self, _sender=None):
        """Pin the Claude limit to the current block's token count."""
        c = getattr(self, "_claude", None)
        if not c:
            return
        set_claude_limit(c["tokens"])
        self.update()

    def reset_claude_limit(self, _sender=None):
        """Forget the calibrated limit and fall back to the auto high-water mark."""
        set_claude_limit(None)
        self.update()


if __name__ == "__main__":
    app = GLMUsageApp()
    app.update()
    app.run()
