#!/bin/zsh
# DESC: Render Home Screens' window states (ready, running, errors) light and dark to staging/ from stub data
set -euo pipefail

REPO="${0:A:h:h}"
if [[ "${1-}" == "-h" || "${1-}" == "--help" ]]; then
  print "usage: app/snapshot.sh [out.png]    build, then render every window state through quiet-open"
  print "writes <out minus .png>[-state]-light.png and -dark.png; default staging/home-screens.png"
  exit 0
fi
OUT="${1:-$REPO/staging/home-screens.png}"
OUT="${OUT:A}"
STEM="${OUT:r}"

if [[ -z "${PROJECT_LIFECYCLE_PACKAGE:-}" && -f "${REPO:h}/project-lifecycle/Package.swift" ]]; then
  export PROJECT_LIFECYCLE_PACKAGE="${REPO:h}/project-lifecycle"
fi
(cd "$REPO" && swift build -c release --product HomeScreens >&2)

# A throwaway bundle: quiet-open needs an .app, and the render must not touch the installed one.
APP="$REPO/staging/Snapshot.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$REPO/.build/release/HomeScreens" "$APP/Contents/MacOS/HomeScreens"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>HomeScreens</string>
<key>CFBundleIdentifier</key><string>local.launchpad-map.snapshot</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LaunchpadMapRepo</key><string>$REPO</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null

rm -f "$STEM"*-light.png "$STEM"*-dark.png
quiet-open "$APP" --snapshot "$OUT"
# The last file written is the dark error-long render.
for _ in {1..120}; do
  [[ -s "$STEM-error-long-dark.png" ]] && break
  sleep 1
done
[[ -s "$STEM-error-long-dark.png" ]] || { print -u2 "snapshot.sh: no render appeared"; exit 1; }
sleep 1
ls "$STEM"*.png
