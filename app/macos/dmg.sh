#!/bin/bash
# Pack "BioQL Studio.app" into a disk image.
#
#   app/macos/dmg.sh [output-dir]
#
# Forked in spirit from Agtr's ~/Developer/agtr-build/dmg70.sh (11 lines), with its three habits NOT carried over:
# it runs without `set -e`, it prints `build rc=$?` and then copies the image **whatever that code was**, and its
# version is hard-coded in the driver while `build.sh` reads another one from the source — a recorded run printed
# `0` for its own version check and went ahead anyway. Here the build is gated on its exit status, the version has
# one source (`app/__init__.py`, read by `build.sh`), and the image is verified by mounting it read-only and
# exercising the copy inside it before the checksum is printed.
#
# The image is NOT notarized and cannot be: the signature is ad-hoc. On this Mac it opens; on any other it arrives
# quarantined and Gatekeeper refuses it until the person allows it by hand. See app/dmg.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$HERE/build}"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/app/__init__.py" | head -1)"
[ -n "$VERSION" ] || { echo "could not read VERSION from app/__init__.py"; exit 1; }

bash "$HERE/build.sh" "$OUT"                      # gated: set -e stops here if the build fails
APP="$OUT/BioQL Studio.app"
[ -d "$APP" ] || { echo "the build produced no app"; exit 1; }

STAGE="$OUT/stage"
DMG="$OUT/BioQL-Studio-$VERSION.dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/BioQL Studio.app"            # ditto, not cp: it keeps the signature intact
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/Read me first.txt" <<TXT
BioQL Studio $VERSION

Drag "BioQL Studio" onto the Applications folder beside it, then open it from Applications.

THIS COPY IS NOT NOTARIZED.
It is signed ad-hoc, which satisfies macOS only on the Mac that built it. On any other Mac the first open is
refused: open System Settings > Privacy & Security, find the message about "BioQL Studio", and choose "Open
Anyway". Notarizing instead of this would need an Apple Developer ID; nothing about the app itself changes.

What it needs to be useful:
  - macOS 13 or later, on Apple Silicon. Intel is untested.
  - Python 3.10 or later already on the Mac. No interpreter is bundled: it would have to be pinned by checksum
    first, and it is not.
  - Claude Code installed and signed in (\`claude\`). Studio has one brain and this is it; there is no second
    model and no fallback.
  - BioQL's own backend answering on 127.0.0.1:5035. Studio attaches to BioQL; it never starts, stops or
    installs it, and it installs no LaunchAgent of its own.

What it does on your Mac:
  - It listens on 127.0.0.1:7810 and nowhere else, and it mints a per-install token kept 0600 in
    ~/Library/Application Support/BioQL Studio.
  - It reaches BioQL only over HTTP, through a closed list of entries, and anything that costs compute or could
    reach real quantum hardware is refused until a person approves that one call.
  - Every call is written to a hash-chained record in that same folder.
  - Its log is ~/Library/Logs/BioQL Studio.log.

Molevera · BioQL Studio $VERSION
TXT

hdiutil create -volname "BioQL Studio $VERSION" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"

# Verify the image, not the folder it was made from: mount it read-only and exercise the copy inside it. Nothing
# is launched from the image.
MNT="$(mktemp -d)"
hdiutil attach "$DMG" -readonly -nobrowse -noautoopen -mountpoint "$MNT" -quiet
trap 'hdiutil detach "$MNT" -quiet >/dev/null 2>&1 || true; rm -rf "$MNT"' EXIT
codesign --verify --deep --strict "$MNT/BioQL Studio.app"
[ -f "$MNT/BioQL Studio.app/Contents/Resources/AppIcon.icns" ] || { echo "the image has no icon"; exit 1; }
[ -f "$MNT/Read me first.txt" ] || { echo "the image has no read-me"; exit 1; }
( cd "$ROOT" && shasum -a 256 -c "$MNT/BioQL Studio.app/Contents/Resources/bioql-studio/SOURCE-SHA256.txt" >/dev/null )
echo "the copy inside the image is signed, carries its icon, and its sources match this tree"

echo "packed: $DMG"
shasum -a 256 "$DMG"
