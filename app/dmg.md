# Packaging BioQL Studio as an app and a disk image — the plan

Derived from Agtr's own two-stage pipeline, both of which were read for this:

- `~/Library/Application Support/AGTR/agtr-os/desktop/macos/build.sh` (128 lines) — builds `Agtr Desktop.app` with
  `swiftc` from the Command Line Tools, rsyncs the Python service into `Contents/Resources/agtr-os`, makes the icon
  with `sips` + `iconutil`, and with `--standalone` also bundles a Python and `cloudflared` and packs a `.dmg`.
- `~/Developer/agtr-build/dmg70.sh` (11 lines) — the driver: set `V`, run `build.sh … --standalone`, register the app
  with LaunchServices, copy the `.dmg` to `investor/`, print its sha256.
- `~/Developer/agtr-build/dmgcheck70.py` (34 lines) — the part worth copying most: it exercises the **bundled**
  Python from inside the built app, with `HOME` pointed at an empty directory and `PATH` reduced to
  `/usr/bin:/bin:/usr/sbin:/sbin`, so the copy is tested as a new Mac would see it, with no app opened and no service
  started.

**Nothing was built in this work.** No `.app`, no `.dmg`, no `codesign` run, no `hdiutil`. `app/macos/build.sh` exists
and is the plan written as a script; it has not been executed.

## What works unsigned on this Mac

Every row below is **expected**, from reading Agtr's pipeline and its recorded logs. None of it was run here.

| Step | Expected |
|---|---|
| `swiftc` from the Command Line Tools, no Xcode project | works — Agtr ships this way (`build.sh:22-24`) |
| `sips` + `iconutil` for the icon | works, and is skipped with a note when there is no `icon-1024.png` |
| `codesign --force --deep --sign -` (ad-hoc) | works. An ad-hoc signature satisfies macOS for an app **built and run on this same Mac** |
| Opening the app from `/Applications` on this Mac | works. It was never quarantined, so Gatekeeper does not ask |
| `hdiutil create -format UDZO` | works; a `.dmg` can be made at any time |
| The service inside the bundle | works in principle — it is `python3 -m app serve`, the same thing the tests run — but the **bundled-Python** variant is not planned (below), so the app needs a Python 3.10+ already on the Mac |

## What blocks distribution, concretely

1. **Notarization needs an Apple Developer ID.** An ad-hoc signature (`--sign -`) cannot be notarized and cannot be
   stapled. On any Mac other than the one that built it, the app arrives quarantined, Gatekeeper refuses it, and the
   person has to go to System Settings → Privacy & Security → "Open Anyway" — which is exactly the note Agtr has to
   ship inside its disk image (`build.sh:104-124`). There is no way around this that does not involve a paid Apple
   Developer account ($99/year), a Developer ID Application certificate, `codesign --options runtime --timestamp`,
   `notarytool submit --wait` and `stapler staple`. **This is the one hard blocker, and it is commercial, not
   technical.**
2. **No bundled interpreter is planned.** Agtr's `--standalone` fetches the *latest* `python-build-standalone` and
   `cloudflared` from the GitHub API at build time (`build.sh:61-76`). Not copied, for two reasons: an unpinned
   "latest" means two builds of the same commit can carry different interpreters, so a bug cannot be tied to a build;
   and `cloudflared` would put a public tunnel inside an app whose entire design is 127.0.0.1 only. If a bundled
   Python is wanted later, the URL and its sha256 are pinned in the repo first.
3. **Hardened runtime and entitlements are unwritten.** They are only needed for notarization, so they wait for (1).
   Studio needs no entitlement for anything it does: no microphone, no camera, no Apple Events, no local network — it
   talks to 127.0.0.1 and reads files in its own home.
4. **Apple Silicon only.** Agtr states this (`build.sh:7, :59`); nothing here was tested on Intel.

## What `app/macos/build.sh` does differently from Agtr's, and why

| Agtr | Here |
|---|---|
| `codesign … \|\| echo "note: not signed"` (`build.sh:98`) — a signing failure becomes a note, and the build still prints `built:` | signing is required: `codesign` then `codesign --verify --deep --strict`, both unguarded, so a failure fails the build |
| `--standalone` fetches `latest` from the GitHub API (`build.sh:62-76`) | not copied; see (2) above |
| links LocalAuthentication, UserNotifications, Carbon, AVFoundation, Speech, NaturalLanguage (`build.sh:23`) | links AppKit and WebKit only — a framework that is not linked is a capability the app cannot be talked into using |
| `dmg70.sh` runs `build.sh`, prints `build rc=$?`, and then copies the `.dmg` **whatever that return code was** (`dmg70.sh:6-9`); `set -e` is not on, and `grep -c '0.9.74'` on line 4 is a version check whose result is printed and ignored — the recorded run printed `0`, meaning it did not match, and the build went ahead | any driver written later runs under `set -euo pipefail` and gates the copy on the build's exit status |
| the version is read from `agtros/__init__.py` by `sed` while `dmg70.sh` *also* hard-codes `V=0.9.93`, and a recorded `dmgcheck70.log` reports `version 0.9.74` — two versions in play, and the `grep -c '0.9.74'` on `dmg70.sh:4` printed `0`, i.e. did not match, with the build going ahead anyway. (The two logs are not shown to be from the same run; that part is inference) | one source: `VERSION` in `app/__init__.py`, and the build fails if it cannot be read |

## The order, when it is time

1. `app/macos/build.sh` → `BioQL Studio.app`, ad-hoc signed, with `SOURCE-SHA256.txt` inside it so the shipped source
   can be matched against the tree the tests ran on.
2. A check in the spirit of `dmgcheck70.py`: import `app` and `studio` **from inside the bundle**, with `HOME` set to
   an empty directory and a bare `PATH`, assert `app.PING_NAME == "bioql-studio"` and `app.PORT == 7810`, and run
   `python3 -m pytest -q tests/test_app.py` against the bundled copy. No app opened, no service started on 7810.
3. `hdiutil create -volname "BioQL Studio" -srcfolder <stage> -ov -format UDZO`, with a `Read me first.txt` that says
   plainly that this copy is not notarized and what the person will see — Agtr's note (`build.sh:104-124`) is the
   right shape for that, minus its Iris and Claude Code paragraphs.
4. `shasum -a 256` the image, and record it beside the build.
5. Only then, if it is to leave this Mac: the Developer ID, the hardened runtime, `notarytool`, `stapler`.

**The app must never ship a LaunchAgent.** (Agtr's `install/` was grepped for `LaunchAgents` and `launchctl` and has none either, so this is a rule for Studio, not a difference from Agtr.) BioQL already
owns `com.bioql.localstack`, `com.bioql.watchdog` and `com.bioql.llm-watchdog`, and Studio attaches to BioQL — it does
not own it, start it or keep it alive.
