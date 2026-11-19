# `app/` — BioQL Studio's local service and native shell

A fork of Agtr Desktop with its identity changed, so it can never attach to the running Agtr Desktop. It serves the
sealed adapter in `studio/` and nothing else: no code execution, no shell, no editor, no hub, no tunnel.

```
app/__init__.py          the identity: port 7810, ping name "bioql-studio", bundle id, log path
app/service.py           the service: ThreadingHTTPServer on 127.0.0.1:7810, the token, the routes
app/__main__.py          python3 -m app serve | token | who
app/web/index.html       the window (findings first)
app/web/app.js           ~260 lines, no framework, no CDN; reads the token from the URL fragment
app/web/app.css          dark/light aware, no web font
app/macos/Sources/*.swift  the native shell (main, AppDelegate, Service) — UNCOMPILED
app/macos/Info.plist     the bundle, with every capability Agtr asks for and Studio does not
app/macos/build.sh       the build, as a script. NOT RUN
app/dmg.md               the packaging plan, and what blocks distribution
```

## Identity: the first thing changed, and why it had to be

| | Agtr Desktop | BioQL Studio |
|---|---|---|
| port | 7790 (`service.py:5`, `:1487`; `Service.swift:5`) | **7810** |
| `/api/ping` name | `"agtr-desktop"` (`service.py:1291`) | **`"bioql-studio"`** |
| data | `~/Library/Application Support/AGTR/desktop` (`Service.swift:7-8`) | `studio.home()` = `~/Library/Application Support/BioQL Studio` |
| log | `~/Library/Logs/AgtrDesktop.log` (`Service.swift:9-10`) | `~/Library/Logs/BioQL Studio.log` |
| bundle id | `com.agtr.desktop` (`Info.plist`) | `com.molevera.bioqlstudio` |
| token header | `X-Agtr-Token` (`service.py:1228`) | `X-Studio-Token` |
| also runs | a hub on 7799, a VS Code proxy, `cloudflared` | none of it |
| LaunchAgent | not found in Agtr's `install/` either (grepped for `LaunchAgents` and `launchctl`: no hit), so this is not a difference — it is a standing rule for Studio | **none, ever** |

Launched with Agtr's identity, the fork would have found Agtr Desktop answering on 7790, called it "the service"
(`Service.swift:36`), and sent it this install's token and native key (`Service.swift:108-109`).

Ports Studio never binds: 3000 and 5035 (BioQL), 7790 / 7799 / 7797 / 7788 / 7791 (Agtr). `python3 -m app serve
--port 5035` is refused before any socket is opened (`app/__main__.py::TAKEN`).

## The routes

| | token | what |
|---|---|---|
| `GET /` `/index.html` `/app.js` `/app.css` | no | the window. Three fixed keys; no request string ever becomes a path |
| `GET /api/ping` | **no** | `{ok, name, version, port}`. The single-instance check and the shell must be able to ask who is listening before they have read anything |
| `GET /api/state` | yes | the usage/state line: calls, refusals, approvals, pendings, runs |
| `GET /api/tools` | yes | `studio.adapter.Adapter.tools()` — the allowlist, verbatim |
| `POST /api/call` | yes | `{name, args}` → `Adapter.call(name, args, approved_by=None, by="window")` |
| `POST /api/approve` | yes | `{id, code}` → one pending call, once |
| `GET /api/run?id=` | yes | the state of a call an approval started |
| `GET /api/ledger?limit=` | yes | the recent rows and `ledger.verify()` |
| `GET /api/guard` | yes | the two most recent **saved** snapshots, compared. It takes no new snapshot |

`/api/call` passes **no approval**, so a pipeline is refused here exactly as it is for Claude Code through
`studio/mcp_server.py`. The body is read for `name` and `args` and nothing else: an `approved_by`, `approve` or `by`
field in it changes nothing, and an `endpoint` or `path` inside `args` is refused by the adapter as an argument the
entry does not declare. The route is always the allowlist's.

## The approval, and exactly what it does and does not prove

Agtr puts Touch ID in front of every approval (`AppDelegate.swift:230-278`). Underneath, the proof it sends is
`native.key` — an ordinary file in the user's own home, written 0600 by `Desktop._secret` (`service.py:98`, `:124-128`).
The server compares that header (`service.py:1234`) and, when it matches, signs a proof asserting **`uv: true`** —
user-verified (`service.py:315`, `:338`, `:949`, `:969`). Any process running as that user can read the file, so
`uv: true` is a claim the server cannot support. **That is a real weakness, and Touch ID is not available to this
work, so it is not imitated.** There is no `native.key` here, no `LocalAuthentication`, and nowhere in Studio does
anything assert that a human was verified.

What is here instead:

1. `POST /api/call` on an entry that needs a person is **refused**. If — and only if — the sole reason was the missing
   approval, the service also records a **pending**: the tool name and the arguments, held server-side.
   "Sole reason" is decided by asking the adapter the same question twice, once without an approval and once with one
   (`Studio._offer`), never by reading the refusal's wording.
2. A single-use **code** is generated for that pending. It is **not** returned to the page. It goes to the service's
   own log and to `HOME/pending/<id>` (0600).
3. `POST /api/approve {id, code}` consumes the pending **under a lock, before the call runs**, and then runs the
   stored name and arguments in a background thread (an approved pipeline can run for hours; a held HTTP request
   would be dropped long before it ended).

**What that proves:** a second, separate request arrived naming this exact pending call and carrying a code that was
never sent to the window — so the call was not started by the page on its own, it cannot be replayed, and it cannot be
re-pointed at different arguments than the ones that were shown.

**What it does not prove:** that a human was present, or which human. Any process running as this user can read
`ui.token` and `HOME/pending/<id>`, so this is an explicit, logged, call-bound act by *something with this user's
files* — not a biometric fact. The ledger says so: `approved_by` is `window:<pending id>`, naming the **mechanism**,
and the row's detail reads `explicit single-use code; not a proof of human presence`. The words *verified*,
*Touch ID*, *biometric* and *uv* appear in no record Studio writes, and a test asserts that.

**Where the line is.** The window's approval does **not** reach real quantum hardware. A `hardware` entry, or
arguments naming a real device (`real_qpu`, a `backend` of `ibm_*`…), is refused and is offered **no pending at all**.
Those stay with the command line and a named person:
`python3 -m studio call <name> '{…}' --approve "Heinz"`. A code pasted into a window names nobody; IBM Quantum has
about 153 s of hardware time left, and that is not something a mechanism should be able to spend.

A pending expires in 10 minutes, at most 32 are held, and a wrong code, an unknown id, an expired offer and a replay
are each refused and each written to the ledger.

## The security properties carried over, with the Agtr line each came from

| Property | Agtr | Here |
|---|---|---|
| per-install token, 0600, created privately | `service.py:124-128` | `app/service.py::read_secret`, via `studio.write_private` (0600 from the first byte, never chmod'ed after) |
| **empty secret rejected** | *the bug*: Agtr creates `ui.token` only when it is **missing**, then `service.py:1228-1229` compares the resulting `""` against a header that is also `""` when absent — an empty or truncated file opens the whole API | `read_secret` raises `Empty` on a file shorter than 32 characters, **and** on one readable by group/other; `matches_token` additionally refuses while the stored secret is empty, so even a secret emptied at runtime does not open it |
| constant-time comparison | `secrets.compare_digest` on `str` (`service.py:1229`), which **raises** `TypeError` on a non-ASCII header — and `_guard` is called *outside* the `try` in both handlers (`service.py:1293`, `:1390`), so that raise is not caught at all and the request dies with no answer, not even a 500 | `compare_digest` on **bytes**; a non-ASCII header is a plain 403 |
| no token in a URL | Agtr also accepts `?t=<token>` (`service.py:1228`) | removed. A token in a URL lands in logs, in history and in `Referer` |
| no token in the page | Agtr substitutes its token placeholder into every `.html` it serves, to anyone with the right Host (`service.py:1236-1255`), handing the 0600 secret to a reader who never had the file | the window gets it in the **URL fragment**, which a browser never sends to a server; `app.js` reads it and clears it with `history.replaceState`. A test asserts the token appears in no response and that the HTML holds no placeholder |
| Host allowlist (no DNS rebinding) | `service.py:1226`, hosts built from the port (`:1202`) | `127.0.0.1:7810`, `localhost:7810`, built from the port actually bound — **and checked on `/api/ping` too**, which Agtr answers before any check (`service.py:1291`) |
| `Cache-Control: no-store`, `nosniff` | `service.py:1214-1215` | same, on every response |
| CSP, `X-Frame-Options: DENY`, `Referrer-Policy` | only on `.html` (`service.py:1256`), so Agtr's JSON and its errors go out framable | on **every** response, JSON and errors included. The CSP is `default-src 'none'` with `'self'` for scripts and styles — no `'unsafe-inline'` and no `frame-src`, both of which Agtr needs for VS Code and its canvases (`agtrcore/app.py:607-612`) |
| single-instance `/api/ping` check, no token | `service.py:1479-1484` | `app.service.who_answers`, and a foreign answer means **do not start** rather than *attach*. A ping name is not authentication |
| POST body capped | Agtr parses whatever `Content-Length` says (`service.py:1392-1394`) | 1 MB, drained then refused with `413` |

### Two holes closed in the sealed adapter, not in Agtr

`studio/allowlist.py::ID` is `^[A-Za-z0-9_.-]{1,128}$`, which `".."` matches, and `studio/adapter.py::_url` quotes a
path parameter with `safe=""` — and `urllib.parse.quote` does not escape a dot. So `pipeline_job(job_id="..")` builds
`/api/pipeline/job/..`, which a server that normalises paths (Flask/Werkzeug does) reads as `/api/pipeline/`.
`studio/` may not be edited in this work, so `app/service.py::dotdot` refuses any argument that fills a `{param}`
segment and is a dot-path. **The defect is in `studio/`, and it is still there for the CLI and the MCP server.**

Second: `studio/ledger.py::_last_hash` finds the previous line's hash by reading only the **final 64 KB** of
`runs.jsonl`, and `studio/adapter.py::_note` writes a call's `args` whole, with no bound. `/api/call` accepts a 1 MB
body, so one request carrying a 100 KB `smiles` would write a line longer than that tail — after which every later
`append` parses a fragment and raises, and the chain stops taking lines for ever. `app/service.py::too_big` refuses
arguments over 32 KB at the door, and `trim` bounds what a refusal records. Same caveat: the CLI and the MCP server
are still exposed.

## Running it

```sh
cd ~/Developer/bioql-studio
python3 -m app serve                  # 127.0.0.1:7810, prints the window URL with the token in the fragment
python3 -m app token                  # just the URL
python3 -m app who                    # who is listening on 7810
python3 -m pytest -q tests/test_app.py
```

Nothing here starts, stops or restarts BioQL, writes inside `~/Documents/Proyectos/Spectrix_BioQL`, `/opt/bioql`,
`~/.bioql` or `~/Library/Application Support/AGTR`, or installs a LaunchAgent.

## What is not done

- **The Swift shell is uncompiled.** `app/macos/Sources/*.swift` is correct by inspection only: no Xcode run, no
  `swiftc`, no `.app`. The bugs it fixes are listed below; the fixes themselves have not been exercised.
- **No DMG was built**, and distribution is blocked on an Apple Developer ID. `app/dmg.md`.
- **The window was served, not rendered.** Tests fetch `/`, `/app.js` and `/app.css` and assert what they contain
  and do not contain; `app.js` passes `node --check`, but no browser or WKWebView has ever executed it, so its
  layout, its dark-mode rendering and its event handlers are unverified.
- The window has no campaign view, no board and no notebook: Phases 3–5 of `docs/PLAN.md`.

### The Agtr bugs the Swift shell carries a fix for

| # | Bug | Agtr | Fix |
|---|---|---|---|
| 1 | **it reuses any server answering on its port** — a `name == "agtr-desktop"` from *anything* makes it "the service", and the shell then sends it the token and the native key | `Service.swift:28-42`, `:62-63`, `:108-109` | the shell trusts **only the child it started**. Anything already listening on 7810 is reported, and not one byte is sent to it. `Service.isMine` / `isAlive` |
| 2 | **it truncates its own log on every launch** — `createFile` on an existing path empties it, and the `seekToEndOfFile` that follows has nothing to seek past, so the log of the launch that just crashed is gone exactly when it is wanted | `Service.swift:71-73` | create only when missing; roll aside at 8 MB instead of emptying. `Service.logHandle()` |
| 3 | **it never restarts a dead service** — no `terminationHandler`, and the only reaction to a blank window is reloading the same dead address every 1.5 s, for ever | `Service.swift:87-88`, `AppDelegate.swift:158-160` | `terminationHandler` → `AppDelegate.serviceDied`, five restarts with exponential backoff, then the person is told; a reload is attempted only while the service is answering |
| 4 | **the page picks which privileged route the native side posts to** — `{type:"verify", path:"/api/…"}`, checked only with `hasPrefix("/api/")`, and the call is then marked biometrically verified | `AppDelegate.swift:189-191`, `:257-278` | `Service.post` takes a Swift `enum Route`, not a string; the page cannot name a path, and there is no `verify` message |
| 5 | **its script-message bridge trusts every frame and origin** — `message.body` is read and acted on with no check of `frameInfo`, while its own CSP allows `frame-src http://127.0.0.1:*` | `AppDelegate.swift:171-172` | `message.webView === web`, `frameInfo.isMainFrame`, and `securityOrigin` must be `http` / `127.0.0.1` / 7810 |
| 6 | **the navigation policy ignores the port** — it checks only `host != "127.0.0.1"`, so BioQL's frontend on 3000, its backend on 5035 and Agtr Desktop on 7790 all load inside the window | `AppDelegate.swift:150-156` | scheme, host **and** port must be Studio's; anything else opens in the browser |
| 7 | **two independent definitions of the home folder.** The shell hard-codes `AGTR/desktop` (`Service.swift:7-8`); the service computes its own in `agtrdesktop/__init__.py::default_home` (:23-30), which agrees *today* — but it returns `$AGTR_DESKTOP_HOME` first when that is set, and the shell passes the whole user environment to the child (`Service.swift:78-85`) without ever setting it. So an `AGTR_DESKTOP_HOME` already in the environment sends the service to one home while the shell keeps reading `ui.token` from the other, and every call is a 403 | `Service.swift:7-8`, `:78-85` vs `agtrdesktop/__init__.py:23-30` | one definition, `Service.home`, **handed to the child** as `BIOQL_STUDIO_HOME`, which `studio.home()` reads first |
| 8 | **"Try Again" after a timeout spawns a second child** while the first still holds the port — and FIX 1 then correctly refuses that orphan as "not ours", so the app can never recover | `AppDelegate.swift:65-69` with `Service.swift:88-94` | `start()` stops any child of ours before spawning. `Service.start()` |
| 9 | its only "restart" path is `service.stop()`, which clears the exit handler — so a restart would stop the service and nothing would bring it back | the shape of `Service.swift:97-100` | `Service.restart()` is separate from `stop()`: it clears the handler, terminates, and fires `onExit` exactly once |
| 10 | `prompt()` in the page returns `null` under WKWebView unless the host implements `runJavaScriptTextInputPanelWithPrompt` — a tool with a required argument would silently do nothing | the class of bug, not a line of Agtr's | the window uses inline `<input>` fields, never `window.prompt()` |

## A note on `studio/`'s own record

When this was written, `studio/adapter.py::_note` wrote one row per call with `provenance` but **not** the `verdict`
and `findings` the adapter computes in the same function — so every row read back as "verdict not recorded", and the
window compensated with a row of its own. **That was fixed in `studio/` during this same session, by whoever owns
that file** (commit `0232e11`), and the adapter now puts the verdict, the findings and the job id on its own line.

The window still writes a row of its own, and it is not redundant: it is the one that carries `window_event`
(`call` / `approval_offered` / `approval_denied` / `approval_granted`), the `approved_by` mechanism, and a REFUSED
line for a refusal the adapter never sees — `Adapter.check` deliberately writes nothing, so without it a refusal
from the window would leave no trace. Its findings are stored as codes, severities and paths only (the first 20, with
a total), for the 64 KB reason above.

Either way, a row with no `verdict` key is shown as **not recorded**, never as clean.
