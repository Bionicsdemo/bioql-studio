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

**Built, packed and running, 2026-11-19.** `app/macos/build.sh` builds `BioQL Studio.app`, ad-hoc signed, with
BioQL's own mark as its icon; it is installed in `/Applications` and starts its own service on 127.0.0.1:7810.
`app/macos/dmg.sh` packs `BioQL-Studio-0.1.0.dmg` (1.8 MB) and verifies the copy **inside the mounted image**. The
steps below are measurements, not expectations.

The icon is not invented: `app/macos/icon.html` redraws BioQL's own mark — three orbits around a benzene hexagon —
from `Spectrix_BioQL/docs/BIOQL Documentos por revisa/molevera_bioql_logo.png`, in the exact colours sampled out of
that file (#2DD4BF, #26B5A3, #22D3EE, #F2F7FC on #060A0F), on the 824-of-1024 macOS icon grid. It is rendered by
headless Chrome, so the PNG can be rebuilt from the HTML; `iconutil` then makes all ten sizes from 16 to 1024, and
macOS resolves it for the bundle (checked by dumping `NSWorkspace.icon(forFile:)`).

## What works unsigned on this Mac — measured

| Step | Expected |
|---|---|
| `swiftc` from the Command Line Tools, no Xcode project | **works.** swiftc 6.4.2, `-swift-version 5`, `arm64-apple-macos13.0`: no errors and no warnings |
| `sips` + `iconutil` for the icon | **skipped**, as designed: there is no `icon-1024.png`, so the build printed its note and the app has the generic icon |
| `codesign --force --deep --sign -` (ad-hoc) | **works**, and `--verify --deep --strict` passes — before the first run and again after the app had run |
| Opening the app from `/Applications` on this Mac | **works.** `ditto` to `/Applications`, `open -a`, and the service answered `/api/ping` in 3 s. Never quarantined, so Gatekeeper did not ask |
| `hdiutil create -format UDZO` | still only expected: **nothing was packed in this work** |
| The service inside the bundle | **works.** It runs as a child of the app (service ppid = app pid), with `cwd` inside the bundle, on `/opt/homebrew/opt/python@3.14/bin/python3.14` (3.14.7) — the first of `Service.python()`'s candidates that exists here. No Python is bundled, so the app still needs a 3.10+ on the Mac |

### What the build and the first run found

- **The source manifest hashed the previous build.** `find studio app -name '*.py'` walked into `app/macos/build/BioQL Studio.app` and hashed the last build's own copies: 25 of 50 lines. So the manifest of a first build and of a rebuild differed with no change in the source — the opposite of what it is for. `app/macos` is pruned now, and the manifest is 25 lines that `shasum -c` matches against the working tree.
- **The brain was dead inside the app.** `studio/brain.py::binary()` found `claude` with `shutil.which`, and the installer puts it in `~/.local/bin` — on PATH in a terminal, on no PATH an app opened from the Finder has (LaunchServices gives `/usr/bin:/bin:/usr/sbin:/sbin` and does not read the shell's profile). The window, `/api/ping` and the engine's health would all have read fine while the chat raised `NoClaude`. Fixed in two places: `WHERE_CLAUDE` looks where Claude Code installs itself, and the shell puts `~/.local/bin` on the child's PATH. Verified with `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin`.
- **One real turn, inside the app:** window → service → `claude` → Studio's MCP server → BioQL → the verifier → the ledger. `api_bioql_health` came back RESOLVED, HTTP 200, verdict clean, and the answer quoted the state rather than asserting it.
- **Every quit left its MCP config behind.** `brain.forget()` runs from `atexit`, and Python does not run `atexit` on the SIGTERM the shell sends: six files had piled up, one per service that had ever run. They hold no secret while `bioql` is the only server, but `mcp_config(extra=…)` writes the owner's external sources into the same file — the leak that function exists to prevent. Fixed: the service traps SIGTERM and SIGINT, deletes its own config on the way out, and sweeps the configs of processes that are gone at start. Measured after the fix: six orphans swept at launch, one file while running, none after quitting.
- **A force-quit left the service holding the port.** SIGKILL on the app skips `applicationWillTerminate`, so `Service.stop()` never runs: the child was reparented onto pid 1, still listening on 7810, and the next launch refused to adopt it — correctly, since it is a service the app did not start and would be handed this install's token — leaving the person an alert and a port to free by hand. The shell now passes its own pid as `BIOQL_STUDIO_PARENT` and the service stops when its parent changes. A service started from a terminal sets no such variable and is never touched. Measured after the fix: SIGKILL on the app, and 7810 free with no orphan.
- **The window really loads:** the app's own WebKit networking process holds connections to 7810 beside the app's, and keeps polling. (A browser tab on the same URL would also poll, so the client processes were told apart rather than the ledger trusted.)

- **Quitting with a call in flight hung the app.** `ThreadPoolExecutor`'s threads are not daemons and Python joins every one of them at interpreter shutdown, so a single `fanout` or `campaign` call in flight kept the service alive long after `run()` had returned — measured: a 30-second task, 30 seconds. The shell quits with `terminate()` and then `waitUntilExit()` **on its main thread**, so that is the app beachballed for the length of the call, and BioQL's calls are allowed twelve hours. `python3 -m app serve` now ends the process itself once its own cleanup is done. Measured: 25 s before, 1 s after.
- **The guard read a root it could not open as "nothing changed".** `os.walk` swallows every error unless it is given `onerror`, so a root macOS refuses — `~/Documents` sits behind a TCC permission — walked as if it held no files, and two such snapshots compared with nothing added, removed or changed: **clean, because it could not look.** That is the third state degrading into the second. A snapshot now records what it could not list, `compare` keeps that apart from added/removed/changed and refuses to call the report clean, and `Changed` says "could not be read" rather than "BioQL changed" — a refused permission must not send the owner hunting for an edit that never happened.
- **The Documents permission, narrowed.** `/api/guard` takes no snapshot at all; it compares the two newest saved ones. So the app itself never needs the permission to serve that route — only an explicit `python3 -m studio guard snapshot` or `tools/e2e_live.py` does, and that is still untested from inside the app. An ad-hoc signature changes identity on every rebuild, so a prompt, once raised, can come back.

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

## The order — steps 1 to 4 are done

1. **Done.** `app/macos/build.sh` → `BioQL Studio.app`, ad-hoc signed, with `SOURCE-SHA256.txt` inside it so the
   shipped source can be matched against the tree the tests ran on (`shasum -a 256 -c`, from the repo root).
2. **Done.** The check in the spirit of `dmgcheck70.py`: `app` and `studio` imported **from inside the bundle**, with
   `HOME` on an empty directory and `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, on the interpreter the app itself picks —
   identity, home, token mode, the window's three files, and that no BioQL source travels inside. The whole suite
   was also run against the shipped bytes: 552 pass. No app opened, no service started on 7810.
3. **Done.** `app/macos/dmg.sh`: a stage folder with the app, a link to `/Applications` and a `Read me first.txt`
   that states plainly that the copy is not notarized, what the person will see, and what the app needs to be
   useful (Python ≥ 3.10, Claude Code signed in, BioQL on :5035, Apple Silicon, macOS 13+). Then
   `hdiutil create -format UDZO`.
4. **Done.** The image is mounted read-only and the copy **inside it** is verified — `codesign --verify --deep
   --strict`, its icon, its read-me, and `shasum -c` of its source manifest against this tree — before the image's
   own sha256 is printed. Nothing is ever launched from the image.
5. **Not done, and not mine to do:** the Developer ID, the hardened runtime, `notarytool`, `stapler`.

**The app must never ship a LaunchAgent.** (Agtr's `install/` was grepped for `LaunchAgents` and `launchctl` and has none either, so this is a rule for Studio, not a difference from Agtr.) BioQL already
owns `com.bioql.localstack`, `com.bioql.watchdog` and `com.bioql.llm-watchdog`, and Studio attaches to BioQL — it does
not own it, start it or keep it alive.
