#!/bin/zsh
# DESC: Build the Home Screens app (the launchpad-map history in a window) into ~/Applications
set -euo pipefail

APP_DIR_REPO="${0:A:h}"
REPO="${APP_DIR_REPO:h}"
APP_NAME="Home Screens"
BUNDLE_ID="local.launchpad-map.home-screens"
EXE_NAME="HomeScreens"
APP="$HOME/Applications/$APP_NAME.app"
CONTENTS="$APP/Contents"

if [[ "${1-}" == "-h" || "${1-}" == "--help" ]]; then
  print "usage: app/bundle.sh    build $APP from this checkout and sign it"
  exit 0
fi

# A stable signing identity where there is one, as ADB Reconnect's bundle does:
# macOS keys permissions to the signature, and an ad-hoc one changes every build.
SIGN_ID="${HOME_SCREENS_SIGN_ID:-Local Dev Signing}"
if ! security find-identity -v -p codesigning | grep -qF "$SIGN_ID"; then
  print -u2 "note: signing identity '$SIGN_ID' not found — signing ad-hoc"
  SIGN_ID="-"
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
swiftc -O "$APP_DIR_REPO/HomeScreens.swift" -o "$BUILD/$EXE_NAME" -framework AppKit -framework WebKit

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
mv "$BUILD/$EXE_NAME" "$CONTENTS/MacOS/$EXE_NAME"
cp "$APP_DIR_REPO/assets/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"

# LaunchpadMapRepo tells the app which checkout's commands and pages to use;
# it is this one, wherever it was cloned.
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>$EXE_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LaunchpadMapRepo</key>
    <string>$REPO</string>
</dict>
</plist>
PLIST

codesign --force --sign "$SIGN_ID" --identifier "$BUNDLE_ID" "$APP" >/dev/null
touch "$APP"
print "$APP"
