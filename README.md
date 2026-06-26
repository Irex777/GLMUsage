# GLM Usage

A macOS menu-bar app that tracks your [GLM Coding Plan](https://z.ai/) usage in real time.

The title bar shows your **token usage %** and a countdown to the next reset.
Open the menu for a breakdown of both quotas.

## What it tracks

- **Tokens** — GLM model usage (glm-4, glm-5, …), measured in tokens processed.
  Resets on your subscription's schedule (reported by the API).
- **Tools** — built-in GLM tool calls (`search`, `web-reader`, `zread`).
  1000 calls per rolling 5-hour window.
- **Claude** *(optional)* — if you use Claude Code, shows tokens used in the
  current 5-hour window, read from local logs in `~/.claude/projects`.

  Anthropic doesn't publish Claude Pro/Max limits, so there's no % to show by
  default. To get one, **calibrate**: the next time Claude actually cuts you
  off, click **⚑ Hit Claude limit just now**. The app pins your limit to that
  block's token count and from then on shows a **% bar** plus a **⚠️** in the
  menu-bar title as you approach it. "Reset learned limit" clears it.

## Cost & history

**Usage History…** (in the menu) shows real per-token cost for both providers,
computed from the exact token counts in your local coding-tool logs
(`~/.claude/projects`):

- **GLM API-equivalent cost** — what your current 5-hour block of GLM usage
  *would* cost at z.ai's published rates (e.g. GLM 5.2 at $1.40/$4.40 per 1M
  in/out). Real token counts, priced per GLM model.
- **Claude API-equivalent cost** — same, at public Anthropic rates
  (input/output/cache priced per model).
- **GLM quota %** — the percentage history from the z.ai quota API (the API
  exposes the quota only as a percentage, so this complements the token-based
  cost above).

It also keeps an **all-time total** across every window — a cumulative ledger of
GLM and Claude token cost that keeps growing as you use them. It dedupes by log
record ID, so it stays correct even after old transcripts are pruned (it counts
each message once, then remembers it).

All cost figures are billed-per-token estimates for comparison — they show the
value of your flat-rate subscriptions, not an actual charge.

## Install

```bash
git clone https://github.com/Irex777/GLMUsage.git
cd GLMUsage
python3 -m venv venv
venv/bin/pip install -r requirements.txt
./build_app.sh            # builds /Applications/GLMUsage.app
open /Applications/GLMUsage.app
```

The app runs in the menu bar only — no Dock icon.

## Configure

Click the menu-bar icon → **Settings…** and paste your API key
(get one at <https://z.ai/>). The key is stored locally in
`.tray_config.json` and never leaves your machine.

You can also toggle **Launch at login** from the same window.

## Develop

Run directly without building the bundle:

```bash
venv/bin/python tray_app.py
```

## Share

`GLMUsage.app` is shareable — each person sets their own API key in Settings.

## License

MIT
