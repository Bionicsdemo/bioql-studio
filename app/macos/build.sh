#!/bin/bash
# Build "BioQL Studio.app" with the Command Line Tools only (no Xcode).
#
#   app/macos/build.sh [output-dir]
#
# NOT RUN in this work: no .app and no .dmg were built. See app/dmg.md for the packaging plan, what works unsigned on
# this Mac, and what blocks distribution.
#
# Forked from Agtr's desktop/macos/build.sh, with four of its habits not carried over — each marked NOT COPIED below.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"                       # the bioql-studio repo
OUT="${1:-$HERE/build}"
APP="$OUT/BioQL Studio.app"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/app/__init__.py" | head -1)"
[ -n "$VERSION" ] || { echo "could not read VERSION from app/__init__.py"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/bioql-studio"

# Only the frameworks the shell actually uses. NOT COPIED: Agtr links LocalAuthentication, UserNotifications, Carbon,
# AVFoundation, Speech and NaturalLanguage (build.sh:23) for Touch ID, the hot key and the voice — Studio has none of
# those, and a framework that is not linked is a capability the app cannot be talked into using.
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos13.0" "$HERE"/Sources/*.swift \
  -framework AppKit -framework WebKit \
  -o "$APP/Contents/MacOS/BioQLStudio"

sed "s/__VERSION__/${VERSION}/g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"

# The service and its window travel inside the app. `studio/` is the sealed adapter; `app/` is this shell's service.
for d in studio app; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'macos' "$ROOT/$d" "$APP/Contents/Resources/bioql-studio/"
done
# proof that what shipped is what the tests ran against
( cd "$ROOT" && find studio app -name '*.py' -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 shasum -a 256 ) \
  > "$APP/Contents/Resources/bioql-studio/SOURCE-SHA256.txt"

if [ -f "$HERE/icon-1024.png" ]; then
  SET="$OUT/AppIcon.iconset"; rm -rf "$SET"; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$HERE/icon-1024.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2)); sips -z $d $d "$HERE/icon-1024.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns"
  rm -rf "$SET"
else
  echo "note: no icon-1024.png; the app gets the generic icon"
fi

xattr -cr "$APP" 2>/dev/null || true

# NOT COPIED: Agtr's `codesign … || echo "note: not signed"` (build.sh:98) turns a signing failure into a note, so a
# build that is not signed at all still prints "built:" and reads as success. Here an ad-hoc signature is required and
# the build fails without one; a real Developer ID is what notarization needs (app/dmg.md).
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "built: $APP (version $VERSION, ad-hoc signature only — not notarizable; see app/dmg.md)"

# NOT COPIED: the --standalone path, which fetches the LATEST python-build-standalone and cloudflared from GitHub at
# build time (Agtr build.sh:62-76). An unpinned "latest" means two builds of the same commit can carry different
# interpreters, and cloudflared would put a public tunnel inside an app whose whole point is 127.0.0.1.
# NOT COPIED: packing the .dmg here. See app/dmg.md; nothing is packed in this work.
