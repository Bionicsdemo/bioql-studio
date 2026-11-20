#!/bin/bash
# Build "BioQL Studio.app" with the Command Line Tools only (no Xcode).
#
#   app/macos/build.sh [output-dir]
#
# RUN on 2026-11-19: it builds `BioQL Studio.app`, ad-hoc signed and verified. No .dmg is packed here — see
# app/dmg.md for the disk-image plan and for what blocks distribution to any other Mac (notarization).
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
# `*.md` is excluded: `app/README.md` and `app/dmg.md` are this work's own notes — they tabulate Agtr Desktop's
# defects by file and line, and name the paths of a machine that is not the reader's. Nothing in `studio/` or `app/`
# reads a .md at runtime (checked), so they are documentation of the repo, not part of the program.
for d in studio app; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'macos' --exclude '*.md' \
    "$ROOT/$d" "$APP/Contents/Resources/bioql-studio/"
done
# Proof that what shipped is what the tests ran against: repo-relative paths, so the manifest can be matched against
# the working tree. `app/macos` is pruned — rsync above excludes it, and without the prune `find` walks into
# `app/macos/build/BioQL Studio.app` and hashes the PREVIOUS build's own copies, so the manifest of a first build and
# of a rebuild differ for no change in the source. (Measured 2026-11-19: 25 of 50 lines were the last build's files.)
# `*.py` AND the window, because the window is program too: `app/web/app.js` draws every card and
# `app/web/vendor/3Dmol-min.js` is the viewer that draws the structures. A manifest that covered only Python would
# have said "what shipped is what the tests ran on" while half a megabyte of JavaScript travelled unlisted.
( cd "$ROOT" && find studio app \( -name '*.py' -o -path 'app/web/*' \) -type f \
    -not -path '*/__pycache__/*' -not -path 'app/macos/*' -not -name '*.pyc' -print0 \
    | sort -z | xargs -0 shasum -a 256 ) > "$APP/Contents/Resources/bioql-studio/SOURCE-SHA256.txt"

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
# NOT COPIED: packing the .dmg here. That is `app/macos/dmg.sh`, which calls this script and is gated on its exit
# status — Agtr's driver copies its image whatever the build returned (dmg70.sh:6-9, with no `set -e`).
