#!/bin/sh
# Build Workstation.app and Workstation Chat.app in the repository folder, with the app icon (assets/AppIcon.icns,
# made by scripts/make_icon.py). osacompile gives an applet the script icon from its Assets.car, which macOS prefers
# over applet.icns: the build removes that catalog and its key, puts the icon in, and signs the bundle again (ad hoc),
# because the changes break osacompile's signature.
set -eu
cd "$(dirname "$0")/.."

build() {
  app=$1
  osacompile -o "$app" "$2"
  cp assets/AppIcon.icns "$app/Contents/Resources/applet.icns"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$app/Contents/Info.plist" 2>/dev/null || true
  rm -f "$app/Contents/Resources/Assets.car"
  codesign --force --sign - "$app"
  touch "$app"  # Finder reads the icon again
  echo "Built $app"
}

build Workstation.app docs/launcher.applescript
build "Workstation Chat.app" docs/launcher-chat.applescript
