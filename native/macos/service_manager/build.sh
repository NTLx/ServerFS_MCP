#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD_ROOT="${1:-$ROOT/build}"
APP="$BUILD_ROOT/ServerFSServiceManager.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
AGENTS="$CONTENTS/Library/LaunchAgents"

rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES" "$AGENTS"
cp "$ROOT/Info.plist" "$CONTENTS/Info.plist"
cp "$ROOT/LaunchAgents/com.ntlx.serverfs.tunnel.plist" "$AGENTS/"
cp "$ROOT/LaunchAgents/com.ntlx.serverfs.bridge.plist" "$AGENTS/"

xcrun swiftc \
  -O \
  -target arm64-apple-macosx13.0 \
  -framework ServiceManagement \
  "$ROOT/Sources/ServiceManager/main.swift" \
  -o "$MACOS/serverfs-service-manager"

xcrun swiftc \
  -O \
  -target arm64-apple-macosx13.0 \
  "$ROOT/Sources/ServiceLauncher/main.swift" \
  -o "$RESOURCES/serverfs-service-launcher"

plutil -lint \
  "$CONTENTS/Info.plist" \
  "$AGENTS/com.ntlx.serverfs.tunnel.plist" \
  "$AGENTS/com.ntlx.serverfs.bridge.plist" >/dev/null
codesign --force --sign - --timestamp=none "$RESOURCES/serverfs-service-launcher"
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

printf '%s\n' "$APP"
