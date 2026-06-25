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
  Shown as an absolute token count, not a percentage: Anthropic doesn't
  publish Claude Pro/Max limits, so there's nothing to compute a % against.

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
