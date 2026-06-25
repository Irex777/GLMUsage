#!/bin/bash
# Build GLMUsage.app — a self-contained macOS menu-bar app bundle.
#
# Usage: ./build_app.sh [DEST]
#   DEST defaults to /Applications. The app launches tray_app.py from THIS
#   project directory using the local virtualenv, so keep the project in place.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/GLMUsage.app"
PYTHON="$PROJECT_DIR/venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "error: virtualenv not found at $PYTHON" >&2
  echo "create it first:  python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "Building $APP …"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Icon
if [ -f "$PROJECT_DIR/GLMUsage.icns" ]; then
  cp "$PROJECT_DIR/GLMUsage.icns" "$APP/Contents/Resources/GLMUsage.icns"
fi

# Info.plist — LSUIElement hides the Dock icon (menu-bar only)
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>glmusage</string>
    <key>CFBundleIdentifier</key><string>com.glm.usage</string>
    <key>CFBundleName</key><string>GLMUsage</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleIconFile</key><string>GLMUsage</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Launcher
cat > "$APP/Contents/MacOS/glmusage" <<EOF
#!/bin/bash
cd "$PROJECT_DIR"
exec "$PYTHON" tray_app.py
EOF
chmod +x "$APP/Contents/MacOS/glmusage"

echo "Done. Launch it with:  open \"$APP\""
