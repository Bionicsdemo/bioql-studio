"""BioQL Studio's local service: one process on this Mac, on 127.0.0.1:7810 only.

A fork of Agtr Desktop's service (agtrdesktop/service.py), with its identity changed (see app/__init__.py) and the
security properties carried over deliberately rather than by copying. Each one names the Agtr line it came from, and
where Agtr's version has a hole, the hole is named too and closed here.

What it serves
    GET  /                 the window (app/web), three fixed files, no path joining at all
    GET  /api/ping         who is listening. The ONE route that needs no token (Agtr: service.py:1291)
    GET  /api/tools        the allowlist entries Studio may call (studio.adapter.Adapter.tools)
    POST /api/call         name + args -> Adapter.call(by="window", approved_by=None). A pipeline is refused here,
                           exactly as it is for Claude Code through the MCP server
    POST /api/approve      one explicit, single-use, call-bound approval for one pending call (see app/README.md for
                           what it does and does not prove — it is NOT a biometric claim)
    GET  /api/run          the state of a call that an approval started
    GET  /api/ledger       the recent ledger rows and the chain's verdict
    GET  /api/guard        the comparison of the two most recent saved guard snapshots. It never TAKES a snapshot:
                           a GET does not change state

It never binds anything but 127.0.0.1:7810 (the port is a parameter only so a test can use an ephemeral port), never
starts or stops BioQL, and reaches BioQL only through studio.adapter.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import studio
from studio import board
from studio import brain
from studio import ledger
from studio import allowlist as al
from studio import guard as sguard
from studio.adapter import Adapter, Refused, wants_hardware

from . import PING_NAME, PORT, VERSION

WEB = Path(__file__).resolve().parent / "web"
# The window is three named files. Agtr resolves a request path under a root and checks containment
# (service.py:1239-1248) — correct, but a path still reaches the filesystem. Here no request string ever becomes a
# path: a request either names one of these keys or it is a 404.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}

# Agtr's CSP (agtrcore/app.py:607-612) allows 'unsafe-inline' styles and frames from ANY localhost port, because its
# window embeds VS Code and sandboxed canvases. Studio keeps the strict half — nothing inline, nothing loaded from
# outside — and opens exactly one door: BioQL's own window on port 3000, which the Studio window wraps as its main
# panel. That is the whole shape of the product: BioQL's interface is not replaced, it is framed, and the Agtr
# machinery (the orb, the findings, the board, the notebook) is added around it.
BIOQL_UI = os.environ.get("BIOQL_UI_URL", "http://127.0.0.1:3000").rstrip("/")
# TargetRadar is its own application with its own interface, served by BioQL's own startup script on 4000. The
# section for it used to list the thirteen routes the allowlist holds, which is not what the owner opens on 3000:
# a list of endpoints is not an interface. The window frames the real one, as it frames BioQL's.
RADAR_UI = os.environ.get("BIOQL_RADAR_URL", "http://127.0.0.1:4000").rstrip("/")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       f"frame-src {BIOQL_UI} http://localhost:3000 {RADAR_UI} http://localhost:4000; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")

TOKEN_MIN = 32              # secrets.token_urlsafe(32) is 43 characters; anything shorter is a broken file
MAX_POST = 1 << 20          # Agtr reads Content-Length and parses whatever arrives (service.py:1392-1394)
MAX_ARGS = 32 * 1024        # how much of a call may go on the hash-chained record (see `too_big`)
PENDING_TTL = 600.0         # an approval offer that nobody takes expires in ten minutes
PENDING_MAX = 32
PENDING_TRIES = 3           # wrong codes before the offer is void: the page holds the token, so guessing is bounded
RUN_MAX = 64
ID = re.compile(r"^[0-9a-f]{24}$")
# A board id is `uuid4().hex[:12]`, shorter than a pending or run id. Checking it against ID refused every board
# that exists, with a message about letters and digits that was true and useless.
BOARD_ID = re.compile(r"^[0-9a-f]{8,32}$")


class Empty(RuntimeError):
    """The per-install token file is there and holds nothing usable. Agtr would carry on with "" (service.py:124-128
    creates the file only when it is MISSING, then service.py:1228-1229 compares that "" against a header that is
    also "" when absent — so an empty or truncated ui.token opens the whole API to any caller with the right Host).
    Studio refuses to serve instead."""


def dotdot(entry: dict, args: dict) -> str:
    """An empty string if no argument that becomes a PATH SEGMENT is a dot-path, else why it is refused.

    This closes a hole in the sealed adapter rather than in Agtr. `studio/allowlist.py::ID` is
    `^[A-Za-z0-9_.-]{1,128}$`, which `".."` matches, and `studio/adapter.py::_url` then quotes it with `safe=""` —
    and `urllib.parse.quote` does not escape a dot. So `pipeline_job(job_id="..")` builds
    `/api/pipeline/job/..`, which a server that normalises paths (Flask/Werkzeug does) reads as `/api/pipeline/`.
    `studio/` may not be edited here, so the window's own door refuses it; the defect is reported upstream.
    """
    for k in set(al.PARAM.findall(entry.get("path", ""))) & set(args or {}):
        v = str(args[k])
        if v in (".", "..") or "." * 2 in v:
            return f"{k!r} must look like an id, and {v!r} is a dot-path that would climb out of {entry['path']}"
    return ""


def too_big(args: dict) -> str:
    """An empty string if these arguments are small enough to be put on the record, else why they are refused.

    A second hole in the sealed adapter. `studio/ledger.py::_last_hash` finds the previous line's hash by reading
    only the FINAL 64 KB of runs.jsonl — so a single line longer than that makes the next `append` parse a fragment
    and raise, and the chain stops taking lines for ever. `studio/adapter.py::_note` writes `args` whole, with no
    bound, and `/api/call` accepts a 1 MB body: one request with a 100 KB `smiles` would brick the ledger. The window
    refuses at the door instead. `studio/` may not be edited here, so the CLI and the MCP server are still exposed.
    """
    n = len(ledger.canon(args or {}))
    if n > MAX_ARGS:
        return (f"these arguments are {n} bytes; at most {MAX_ARGS} can go on the hash-chained record, and a line "
                f"longer than 64 KB would break the chain for every line after it")
    return ""


def trim(args: dict) -> dict:
    """What goes on the record. Already bounded by `too_big` on the way in; bounded again here, because a refusal is
    recorded for arguments that were refused precisely for being too large."""
    raw = ledger.canon(args or {})
    return args if len(raw) <= MAX_ARGS else {"args_omitted": {"bytes": len(raw), "keys": sorted(map(str, args))[:40]}}


def read_secret(home: Path, name: str = "ui.token") -> str:
    """The per-install secret. Agtr: service.py:124-128 — same shape (created 0600 from the first byte by
    studio.write_private, never written open and chmod'ed after), plus the two checks Agtr does not make:

    - a file that EXISTS but holds nothing usable is an error, not an empty secret. Agtr creates the file only when it
      is missing, and then service.py:1228-1229 compares that "" against a header that is also "" when absent — so an
      empty or truncated ui.token opens its whole API to any caller with the right Host;
    - a secret readable by group or others is an error too.
    """
    p = Path(home) / name
    if not p.exists():
        studio.write_private(p, secrets.token_urlsafe(32))
    raw = p.read_text(encoding="utf-8", errors="replace").strip()
    if len(raw) < TOKEN_MIN:
        raise Empty(f"{p} holds no usable token ({len(raw)} characters). Delete it and start Studio again; a new one "
                    f"is written privately. Studio will not serve with an empty secret.")
    mode = os.stat(p).st_mode & 0o777
    if mode & 0o077:
        raise Empty(f"{p} is readable by others (mode {mode:o}). Run: chmod 600 {p}")
    return raw


@dataclass
class Pending:
    """One call that was refused for want of a person, held server-side with its arguments.

    The arguments are stored HERE, not handed back to the approver: /api/approve carries only an id and a code, so an
    approval cannot be re-pointed at a different call or different arguments than the one that was shown.
    """
    id: str
    code: str
    tool: str
    args: dict
    why: str
    created: float = field(default_factory=time.time)
    used: bool = False
    tries: int = 0              # wrong codes; the offer is void at PENDING_TRIES

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created > PENDING_TTL

    def public(self) -> dict:
        return {"id": self.id, "tool": self.tool, "args": self.args, "why": self.why, "tries_left": max(0, PENDING_TRIES - self.tries),
                "created": round(self.created, 3), "expires_in": round(PENDING_TTL - (time.time() - self.created), 1),
                "used": self.used}


@dataclass
class Run:
    id: str
    tool: str
    state: str = "RUNNING"
    detail: str = ""
    verdict: str | None = None
    findings: list = field(default_factory=list)
    result_sha256: str | None = None
    provenance: dict | None = None
    seconds: float | None = None

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class Studio:
    """The service's state. One per process; a test makes its own with a fake adapter and an ephemeral port."""

    def __init__(self, home: Path | None = None, port: int = PORT, adapter: Adapter | None = None,
                 log=None) -> None:
        self.home = Path(home) if home else studio.home()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.port = int(port)
        # Agtr builds its Host allowlist from its own port (service.py:1202). Studio's is built from the port it is
        # actually listening on, so a test on an ephemeral port is checked just as strictly as 7810 is.
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.token = self._secret("ui.token")
        self._token = self.token.encode()
        self.adapter = adapter if adapter is not None else Adapter()
        self.log = log or self._print
        self.started = time.time()
        self.pending: dict[str, Pending] = {}
        self.runs: dict[str, Run] = {}
        self.calls = 0
        self.refusals = 0
        self.approvals = 0
        self.turns: dict[str, dict] = {}            # the chat: one entry per turn, read back by polling
        self._lock = threading.Lock()
        # Agtr holds a SECOND secret, native.key (service.py:98), and any request carrying it is treated as having
        # passed Touch ID (service.py:1234, and `uv: bool(native)` in the signed proofs at :315, :338, :949, :969).
        # That file is an ordinary 0600 file: every process running as this user can read it, so `uv: true` is a claim
        # the server cannot support. Studio has no equivalent and makes no such claim. See app/README.md.

    # ------------------------------------------------------------------ the per-install token
    def _secret(self, name: str) -> str:
        return read_secret(self.home, name)

    def matches_token(self, given: str | bytes | None) -> bool:
        """Constant-time, on BYTES. Agtr compares str (service.py:1229), and secrets.compare_digest raises TypeError
        on a non-ASCII str — and `_guard` is called OUTSIDE the try in both of Agtr's handlers (service.py:1293,
        :1390), so that raise is not caught at all: the request dies with no answer, not even its 500."""
        if not self._token or len(self._token) < TOKEN_MIN:         # belt and braces: never open on an empty secret
            return False
        if given is None:
            return False
        raw = given.encode("utf-8", "replace") if isinstance(given, str) else bytes(given)
        return secrets.compare_digest(raw, self._token)

    def _print(self, msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} studio: {msg}", flush=True)

    # ------------------------------------------------------------------ what the window shows as its state line
    def state(self) -> dict:
        with self._lock:
            pend = [p.public() for p in self.pending.values() if not p.used and not p.expired()]
        return {"name": PING_NAME, "version": VERSION, "port": self.port, "home": str(self.home),
                "bioql": self.adapter.base, "bioql_ui": BIOQL_UI, "radar_ui": RADAR_UI,
                "up_seconds": round(time.time() - self.started, 1),
                "calls": self.calls, "refusals": self.refusals, "approvals": self.approvals,
                "pending": pend, "runs": [r.public() for r in list(self.runs.values())[-RUN_MAX:]]}

    # ------------------------------------------------------------------ calling BioQL
    def call(self, name: str, args: dict) -> tuple[dict, int]:
        """/api/call. `by="window"`, `approved_by=None` — always. A pipeline is refused here exactly as it is for
        Claude Code, and when a person COULD unlock it the refusal carries an offer (a pending), never the call."""
        if not isinstance(name, str) or not isinstance(args, dict):
            return {"state": "REFUSED", "detail": "a call is {name: str, args: object}"}, 400
        try:
            e = self.adapter.check(name, args)      # makes no HTTP call and writes no ledger row
            bad = dotdot(e, args) or too_big(args)
            if bad:
                raise Refused(bad)
        except Refused as why:
            out = {"state": "REFUSED", "detail": str(why)}
            offer = self._offer(name, args)         # would a person's approval change this answer?
            if offer:
                out["pending"] = offer
                out["how"] = ("This call needs a person. The code for it was written to Studio's log and to "
                              "HOME/pending/<id>; paste it into Approve. It is single-use and bound to this call.")
            else:
                # A refusal is a line of the record, as it is for Claude Code through the MCP server (where
                # studio.adapter.call writes it). The adapter's `check` deliberately writes nothing, so the window
                # writes its own — otherwise a refusal from the window would leave no trace at all.
                self._refused(name, args, str(why), event="call")
            return out, 403
        try:
            # `check` passes but `call` can still refuse: a `{job_id}` segment is filled only from an argument that
            # looks like an id, so `job_id: "../../api/scripts/execute"` is refused where the path is built, not here.
            res = self.adapter.call(name, args, approved_by=None, by="window")
        except Refused as why:
            self._refused(name, args, str(why), event="call")
            return {"state": "REFUSED", "detail": str(why)}, 403
        self.calls += 1
        self._note(name, res, by="window", event="call")
        return self._shape(res), 200

    def _offer(self, name: str, args: dict) -> dict | None:
        """A pending, when and only when the sole reason for the refusal was that nobody approved.

        Told apart by asking the adapter the same question twice — once without an approval, once with one — never by
        reading the refusal's wording. And the window's approval does NOT reach real quantum hardware: a `hardware`
        entry, or arguments naming a real device, stays with the command line and a named person
        (`python3 -m studio call … --approve "<name>"`), because a code pasted into a window names no one.
        """
        try:
            e = self.adapter.check(name, args, approved_by="window")
        except Refused:
            return None                              # refused for some other reason: there is nothing to offer
        if dotdot(e, args) or e["kind"] == "hardware" or wants_hardware(args):
            return None
        with self._lock:
            for p in list(self.pending.values()):
                if p.used or p.expired():
                    del self.pending[p.id]
            if len(self.pending) >= PENDING_MAX:
                return None
            # 128 bits of code, not 32: a guess has to be hopeless on its own, not merely rate-limited.
            p = Pending(id=secrets.token_hex(12), code=secrets.token_hex(16), tool=name, args=dict(args),
                        why=f"{e['kind']} entry {name!r} ({e['method']} {e['path']})")
            self.pending[p.id] = p
        # The code is NOT returned to the caller: it goes to this service's own log and to a 0600 file, so taking the
        # offer needs a second, deliberate act by someone reading this Mac's log. What that does and does not prove is
        # written out in app/README.md and in approve() below.
        studio.write_private(self.home / "pending" / p.id, p.code + "\n")
        self.log(f"approval needed for {name}: id {p.id} code {p.code} (expires in {int(PENDING_TTL)}s)")
        ledger.append({"tool": name, "args": trim(args), "state": "PENDING", "by": "window",
                       "window_event": "approval_offered", "pending_id": p.id, "detail": p.why[:300]})
        return p.public()

    def approve(self, pid: str, code: str) -> tuple[dict, int]:
        """One explicit human approval for ONE pending call. Single-use, consumed under the lock BEFORE the call runs.

        What this proves: that a second, separate request arrived naming this exact pending call and carrying a code
        that was never sent to the window — so the call was not started by the page on its own, and an approval cannot
        be replayed or re-pointed at other arguments.

        What it does NOT prove: that a human was present, or which human. Any process running as this user can read
        `ui.token` and the pending code, so this is an explicit, logged, call-bound act by someone with this user's
        files — not a biometric fact. Agtr's `native.key` asserts `uv: true` from exactly the same kind of file
        (service.py:98, 1234, 315); Studio does not make that claim anywhere.
        """
        if not isinstance(pid, str) or not isinstance(code, str):
            return {"state": "REFUSED", "detail": "approve takes {id, code}"}, 400
        now = time.time()
        with self._lock:
            p = self.pending.get(pid)
            if p is None:
                bad = "no such pending call"
            elif p.used:
                bad = "that approval was already used (it is single-use)"
            elif p.expired(now):
                bad = "that approval expired"
            elif p.tries >= PENDING_TRIES:
                bad = f"that approval was voided after {PENDING_TRIES} wrong codes"
            elif not secrets.compare_digest(code.encode("utf-8", "replace"), p.code.encode()):
                # A wrong code BURNS a try, under the same lock. The page already holds the token, so an offer that
                # could be guessed at for ten minutes would weaken the one thing this mechanism does claim: that the
                # call was not started by the page on its own.
                p.tries += 1
                bad = ("wrong code" if p.tries < PENDING_TRIES
                       else f"wrong code; this approval is now void after {PENDING_TRIES} tries")
            else:
                bad = ""
                p.used = True                        # consumed here, under the lock: a replay finds `used`
        if bad:
            self.refusals += 1
            ledger.append({"tool": p.tool if p else None, "state": "REFUSED", "by": "window", "verdict": None,
                           "window_event": "approval_denied", "pending_id": pid[:64], "detail": bad})
            self.log(f"approval denied ({bad}) for {pid[:12]}")
            return {"state": "REFUSED", "detail": bad}, 403
        try:
            (self.home / "pending" / p.id).unlink()
        except OSError:
            pass
        self.approvals += 1
        # `approved_by` names the MECHANISM, not a person and not a biometric: "window:<pending id>".
        who = f"window:{p.id}"
        ledger.append({"tool": p.tool, "args": trim(p.args), "state": "APPROVED", "by": "window",
                       "window_event": "approval_granted", "pending_id": p.id, "approved_by": who,
                       "detail": "explicit single-use code; not a proof of human presence"})
        run = Run(id=p.id, tool=p.tool)
        with self._lock:
            self.runs[run.id] = run
            for k in list(self.runs)[:-RUN_MAX]:
                del self.runs[k]
        # In the background: an approved pipeline can run for hours (Studio's timeouts are BioQL's, up to 12 h), and a
        # held HTTP request would be dropped by any proxy or the webview long before it ended.
        threading.Thread(target=self._run, args=(run, p, who), name=f"run-{run.id[:8]}", daemon=True).start()
        return {"state": "APPROVED", "run": run.id, "tool": p.tool,
                "note": "started; follow it on /api/run?id=…"}, 200

    def _run(self, run: Run, p: Pending, who: str) -> None:
        # `run.state` is written LAST, every way out of this function. /api/run is read without a lock, so a state
        # published before the verdict and the hash would let a reader see a finished run whose verdict is still
        # missing — and read that absence as "nothing was found", which is the failure this whole layer exists to
        # prevent. While it is "RUNNING" nothing about it is claimed at all.
        try:
            res = self.adapter.call(p.tool, p.args, approved_by=who, by="window")
        except Refused as why:                       # the allowlist changed under us, or the entry was disabled
            self.refusals += 1
            run.detail = str(why)[:300]
            run.state = "REFUSED"
            return
        except Exception as e:                       # never a plausible result: the cause, named
            run.detail = f"{type(e).__name__}: {e}"[:300]
            run.state = "UNRESOLVABLE"
            return
        self.calls += 1
        run.detail = (res.detail or "")[:300]
        run.verdict = res.meta.get("verdict")
        run.findings = res.meta.get("findings") or []
        run.provenance = res.meta.get("provenance")
        run.seconds = res.meta.get("seconds")
        run.result_sha256 = ledger.digest(res.value) if res.resolved else None
        self._note(p.tool, res, by="window", event="approved_call", approved_by=who, pending_id=p.id)
        run.state = res.state

    # ------------------------------------------------------------------ the record
    def _refused(self, name, args: dict, why: str, event: str) -> None:
        self.refusals += 1
        ledger.append({"tool": name if isinstance(name, str) else None, "args": trim(args), "state": "REFUSED",
                       "by": "window", "window_event": event, "verdict": None, "detail": why[:300]})

    def _note(self, name: str, res, by: str, event: str, **extra) -> None:
        """The verifier's verdict and findings, on the record.

        studio.adapter._note writes one row per call but carries only `provenance` from res.meta — the `verdict` and
        `findings` it also computes never reach runs.jsonl, although the project's README says they do. `studio/` is
        sealed, so Studio's window adds a second row of its own with them. A row with no `verdict` key was written by
        something that did not record one, and the window shows it as "not recorded", never as "clean".

        Only the findings' codes, severities and paths are stored: a full finding list on a large result would be a
        very long line, and ledger._last_hash reads only the final 64 KB of the file to find the previous hash, so one
        oversized line would break the chain for every line after it.
        """
        f = res.meta.get("findings") or []
        ledger.append({"tool": name, "state": res.state, "by": by, "window_event": event,
                       "verdict": res.meta.get("verdict"),
                       "findings": [{"severity": x.get("severity"), "code": x.get("code"), "path": str(x.get("path"))[:120]}
                                    for x in f[:20]],
                       "findings_total": len(f),
                       "result_sha256": ledger.digest(res.value) if res.resolved else None,
                       "provenance": res.meta.get("provenance"),
                       "seconds": res.meta.get("seconds"), **extra})

    def _shape(self, res) -> dict:
        d = res.as_dict()
        d["verdict"] = res.meta.get("verdict")
        d["findings"] = res.meta.get("findings") or []
        d["provenance"] = res.meta.get("provenance")
        return d

    def ledger_rows(self, limit: int = 50) -> dict:
        """The recent rows and the chain's verdict. A row that recorded no verdict says so; it is never read as clean."""
        f = ledger.path_file()
        rows: list[dict] = []
        if f.exists():
            for line in f.read_bytes().splitlines()[-max(1, min(limit, 200)):]:
                try:
                    r = json.loads(line)
                except ValueError:
                    rows.append({"state": "UNREADABLE", "verdict": None, "verdict_recorded": False})
                    continue
                r["verdict_recorded"] = "verdict" in r
                r.setdefault("verdict", None)
                v = r.get("value")
                if isinstance(v, (dict, list)):      # never ship a payload to the window: the ledger holds hashes
                    r["value"] = f"<{type(v).__name__}>"
                rows.append(r)
        return {"rows": rows, "chain": ledger.verify(), "file": str(f)}

    # ------------------------------------------------------------------ the chat: Claude Code, and nothing else
    def chat(self, text: str, brain_session=None) -> tuple[dict, int]:
        """Start one turn. It runs in a thread and the window polls it, because a turn can call a pipeline and a
        held request would be dropped by any proxy long before the pipeline finished.

        The brain is Claude Code. If it is not on this Mac the answer says so and names what to install — it never
        falls back to another model, because the new system has one brain and a silent substitution would put an
        unrecorded voice in a system whose claim is that everything is on a signed record."""
        text = (text or "").strip()
        if not text:
            return {"error": "say something"}, 400
        if len(text) > 32_000:
            return {"error": "that is longer than a turn takes; say it in less"}, 413
        tid = secrets.token_hex(12)
        turn = {"id": tid, "asked": text, "events": [], "state": "running", "started": time.time()}
        with self._lock:
            self.turns[tid] = turn
            for k in list(self.turns)[:-32]:
                self.turns.pop(k, None)

        def work() -> None:
            try:
                s = brain_session if brain_session is not None else brain.session("chat")
                for e in s.ask(text):
                    e = dict(e)
                    if e.get("type") == "tool_result":
                        e["card"] = self._card_of(e.get("text"))
                    with self._lock:
                        turn["events"].append(e)
                    if e.get("type") in ("result", "fail"):
                        break
            except brain.NoClaude as why:
                with self._lock:
                    turn["events"].append({"type": "fail", "text": str(why)})
            except Exception as why:                # noqa: BLE001 - a turn that dies says so; it never half-answers
                with self._lock:
                    turn["events"].append({"type": "fail", "text": f"{type(why).__name__}: {why}"[:400]})
            finally:
                with self._lock:
                    turn["state"] = "done"
                    turn["ended"] = time.time()

        threading.Thread(target=work, name=f"chat-{tid}", daemon=True).start()
        return {"id": tid}, 200

    def _card_of(self, body) -> dict | None:
        """A tool answer drawn as a card. The card module is read lazily and its absence is reported on the card,
        never hidden: a result with no card still shows its data as text."""
        try:
            payload = json.loads(body) if isinstance(body, str) else body
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("runs"), list) and payload.get("group"):
            # A fan-out: several independent calls that ran at the same time. One card per call, under a head that
            # says what the batch did — never one merged card, which would lose which answer came from which call.
            return {"kind": "group", "title": f"{len(payload['runs'])} call(s), run together",
                    "subtitle": (payload.get("note") or "") + f"  ·  {payload.get('seconds')}s",
                    "group": payload["group"], "counts": payload.get("counts") or {},
                    "cards": [self._card_of({**r, "meta": {**(r.get("meta") or {}), "tool": r.get("name")}})
                              for r in payload["runs"]],
                    "provenance": {}, "verdict": None, "findings": [], "blocks": []}
        meta = payload.get("meta") or {}
        value = payload.get("value", payload)
        try:
            from studio import cards
            return cards.card(value, tool=meta.get("tool"), meta=meta)
        except Exception:                           # noqa: BLE001 - no card module yet, or a shape it cannot read
            return {"kind": "text", "title": meta.get("tool") or "result",
                    "provenance": meta.get("provenance") or {}, "verdict": meta.get("verdict"),
                    "findings": meta.get("findings") or [],
                    "blocks": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)[:4000]}]}

    def turn_of(self, tid: str, since: int = 0) -> tuple[dict, int]:
        with self._lock:
            t = self.turns.get(tid)
            if t is None:
                return {"error": "no such turn"}, 404
            # `state` is read AFTER the slice, so a poll can never see `done` with events still to come.
            evs = t["events"][max(0, since):]
            return {"id": tid, "since": since, "events": evs, "next": since + len(evs), "state": t["state"],
                    "asked": t["asked"]}, 200

    def ui_state(self, which: str = "bioql") -> dict:
        """Whether a framed window answers, and nothing more: BioQL's on 3000, or TargetRadar's on 4000. Three
        outcomes, kept apart: it answered (`up`), it refused the connection (`up: false`, with the reason), or we
        could not tell. A window Studio cannot reach is never reported as a window that is not there."""
        base = RADAR_UI if which == "radar" else BIOQL_UI
        req = urllib.request.Request(base + "/", method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=3) as r:
                return {"url": base, "which": which, "up": 200 <= r.status < 400, "http": r.status}
        except urllib.error.HTTPError as e:
            return {"url": base, "which": which, "up": e.code < 400, "http": e.code}
        except (urllib.error.URLError, TimeoutError, OSError) as why:
            return {"url": base, "which": which, "up": False, "why": f"{getattr(why, 'reason', why)}"[:120]}

    def board_write(self, b: dict) -> tuple[dict, int]:
        """One route, four acts: new, node, link, evidence. Everything the board refuses — an unknown kind, a link
        to a node that is not there, an evidence node with no ledger line behind it — comes back as the board's own
        reason, never softened. The window cannot invent evidence: `evidence` names a ledger HASH, and the row is
        read from the record, not from the request body."""
        act = str(b.get("act") or "")
        try:
            if act == "new":
                made = board.new(str(b.get("title") or ""), str(b.get("question") or ""))
                return {"board": made["id"]}, 200
            bid = str(b.get("id") or "")
            if not BOARD_ID.match(bid):
                return {"error": "a board id is 8 to 32 hex characters"}, 400
            r = board.load(bid)
            if not r.resolved:
                return {"state": r.state, "detail": r.detail}, 404 if r.absent else 500
            bd = r.value
            if act == "node":
                node = board.add(bd, str(b.get("kind") or ""), str(b.get("label") or ""))
            elif act == "link":
                board.link(bd, str(b.get("from") or ""), str(b.get("edge") or ""), str(b.get("to") or ""))
                node = {"ok": True}
            elif act == "evidence":
                want = str(b.get("ledger_hash") or "")
                row = next((x for x in self.ledger_rows(400)["rows"] if x.get("hash") == want), None)
                if row is None:
                    return {"error": "that line is not on the hash-chained record"}, 404
                node = board.evidence_from(bd, row, about=(str(b.get("about")) if b.get("about") else None),
                                           contradicts=bool(b.get("contradicts")))
            else:
                return {"error": f"unknown act {act!r}"}, 400
            board.save(bd)
            return {"node": node, "report": board.report(bd)}, 200
        except ValueError as why:
            return {"error": str(why)}, 400

    def guard_report(self) -> dict:
        """The two most recent saved guard snapshots, compared. It takes NO snapshot: a GET changes nothing.

        The snapshot NAME never comes from the request — studio.guard.load joins a name into a path — so this reads
        the directory itself and uses the two newest files.
        """
        d = self.home / "guard"
        try:
            snaps = sorted((p for p in d.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime)
        except OSError as e:
            return {"state": "UNRESOLVABLE", "source": "studio guard", "detail": f"cannot read {d}: {e}"}
        if len(snaps) < 2:
            return {"state": "ABSENT", "source": "studio guard",
                    "detail": f"{len(snaps)} saved snapshot(s) in {d}: a comparison needs two. "
                              f"Take them with: python3 -m studio guard snapshot <name>"}
        older, newer = snaps[-2], snaps[-1]
        try:
            report = sguard.compare(json.loads(older.read_text()), json.loads(newer.read_text()))
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Unreadable is "we could not ask", never "nothing changed". The default is never clean.
            return {"state": "UNRESOLVABLE", "source": "studio guard",
                    "detail": f"{older.name} vs {newer.name}: {type(e).__name__}: {e}"}
        return {"state": "RESOLVED", "source": "studio guard",
                "detail": f"{older.name} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(older.stat().st_mtime))})"
                          f" vs {newer.name} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(newer.stat().st_mtime))})",
                "value": report}


# ---------------------------------------------------------------------------------------------- HTTP
def make_handler(s: Studio):
    class H(BaseHTTPRequestHandler):
        server_version = f"{PING_NAME}/{VERSION}"       # never "agtr-desktop/…" (Agtr: service.py:1205)
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                      # Agtr: service.py:1207-1208
            pass

        # -------------------------------------------------------------- answering
        def _send(self, status: int, data: bytes, ctype: str) -> None:
            """Agtr sets Cache-Control and nosniff on every answer (service.py:1214-1215) but adds the CSP,
            Referrer-Policy and X-Frame-Options only to .html (service.py:1256) — so its JSON and its errors go out
            framable. Here every response carries all of them."""
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            if status >= 400:
                # A refusal answers before the body is read, so this connection cannot be reused safely: the unread
                # bytes would be parsed as the next request. Closing it is also one fewer socket held by a caller
                # that has no token.
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")

        # -------------------------------------------------------------- the two gates
        def _host_ok(self) -> bool:
            """Right Host, so a name that resolves to 127.0.0.1 cannot drive this API from a web page (DNS
            rebinding). Agtr: service.py:1226. Unlike Agtr, this is checked on /api/ping too (Agtr answers ping
            before any check, at service.py:1291), because a page that can read the ping learns the port and the
            service's name for free."""
            if self.headers.get("Host", "") in s.allowed_hosts:
                return True
            self._json({"error": "forbidden"}, 403)
            return False

        def _token_ok(self) -> bool:
            """Agtr: service.py:1227-1231. Two differences, both deliberate:
            - no `?t=` query fallback (Agtr's, service.py:1228): a token in a URL lands in logs, in history and in
              the Referer of anything the page loads;
            - compared as bytes, and refused outright when the stored secret is empty or short."""
            if s.matches_token(self.headers.get("X-Studio-Token")):
                return True
            self._json({"error": "forbidden"}, 403)
            return False

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_POST:
                # Drained first (bounded), then refused: answering mid-upload resets the connection and the caller
                # sees a network error instead of the reason. Only a caller that already passed the token gets here.
                left = min(max(n, 0), 8 * MAX_POST)
                while left > 0 and self.rfile.read(min(left, 1 << 16)):
                    left -= 1 << 16
                self._json({"error": f"the body must be JSON of at most {MAX_POST} bytes"}, 413)
                return None
            raw = self.rfile.read(n) if n else b"{}"
            try:
                out = json.loads(raw or b"{}")
            except ValueError:
                self._json({"error": "bad json"}, 400)
                return None
            if not isinstance(out, dict):
                self._json({"error": "the body must be a JSON object"}, 400)
                return None
            return out

        # -------------------------------------------------------------- the window
        def _static(self, path: str) -> None:
            hit = STATIC.get(path)
            if hit is None:
                return self._json({"error": "not found"}, 404)
            try:
                data = (WEB / hit[0]).read_bytes()
            except OSError:
                return self._json({"error": f"the window's {hit[0]} is missing from this copy"}, 500)
            # The token is NOT substituted into the page. Agtr replaces __AGTR_TOKEN__ in any .html it serves
            # (service.py:1255) to whoever sent the right Host, which hands the 0600 secret to a reader who never
            # had the file. Studio's window receives it in the URL FRAGMENT, which a browser never sends to a
            # server: the native shell loads http://127.0.0.1:7810/#t=<token>, and app.js reads it and clears it.
            self._send(200, data, hit[1])

        # -------------------------------------------------------------- routes
        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return
            u = urlparse(self.path)
            p, qs = u.path, parse_qs(u.query)
            if not p.startswith("/api/"):
                return self._static(p)
            if p == "/api/ping":
                # The only route with no token: the native shell and the single-instance check must be able to ask
                # who is listening before they have read anything. It answers the name and nothing private — no
                # token, no home path, no ledger.
                return self._json({"ok": True, "name": PING_NAME, "version": VERSION, "port": s.port})
            if not self._token_ok():
                return
            try:
                if p == "/api/state":
                    return self._json(s.state())
                if p == "/api/tools":
                    return self._json({"tools": s.adapter.tools()})
                if p == "/api/ledger":
                    try:
                        limit = int((qs.get("limit") or ["50"])[0])
                    except ValueError:
                        limit = 50
                    return self._json(s.ledger_rows(limit))
                if p == "/api/guard":
                    return self._json(s.guard_report())
                if p == "/api/progress":
                    # Las etapas de los pipelines en curso, para dibujarlas como la UI de BioQL en
                    # :3000. Las escribe el adaptador desde `_follow` (otro PROCESO, de ahi el
                    # fichero con flock), y aqui solo se leen: esta ruta no sondea a BioQL ni toca
                    # una corrida. Un trabajo sin etapas localizables trae `stages_unavailable`
                    # con la causa en vez de una lista vacia que se leeria como "sin etapas".
                    from studio import stages as _stages
                    jid = (qs.get("job") or [""])[0]
                    return self._json(_stages.read(jid or None))
                if p == "/api/chat":
                    tid = (qs.get("id") or [""])[0]
                    try:
                        since = int((qs.get("since") or ["0"])[0])
                    except ValueError:
                        since = 0
                    out, code = s.turn_of(tid, since)
                    return self._json(out, code)
                if p == "/api/ui":
                    # Is BioQL's own window up? The page cannot ask: `connect-src 'self'` forbids it reaching
                    # another origin, and a page that cannot ask must not guess. Studio asks, and only reports —
                    # it never starts, stops or restarts BioQL's frontend.
                    return self._json(s.ui_state((qs.get("which") or ["bioql"])[0]))
                if p == "/api/boards":
                    return self._json({"boards": board.boards()})
                if p == "/api/board":
                    bid = (qs.get("id") or [""])[0]
                    if not BOARD_ID.match(bid or ""):
                        return self._json({"error": "a board id is 8 to 32 hex characters"}, 400)
                    r = board.load(bid)
                    if not r.resolved:
                        # ABSENT and UNRESOLVABLE are different answers and the window shows them differently: a
                        # board that cannot be read is never drawn as an empty one.
                        return self._json({"state": r.state, "detail": r.detail}, 404 if r.absent else 500)
                    b = r.value
                    return self._json({"board": b, "report": board.report(b), "markdown": board.markdown(b)})
                if p == "/api/run":
                    rid = (qs.get("id") or [""])[0]
                    run = s.runs.get(rid) if ID.match(rid or "") else None
                    if run is None:
                        return self._json({"error": "no such run"}, 404)
                    return self._json(run.public())
                return self._json({"error": "not found"}, 404)
            except Exception as e:                      # the service stays up; the caller sees the cause
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)

        def do_POST(self):  # noqa: N802
            if not self._host_ok() or not self._token_ok():
                return
            p = urlparse(self.path).path
            b = self._body()
            if b is None:
                return
            try:
                if p == "/api/call":
                    # Only `name` and `args` are read. An `approved_by`, `approve`, `by` or `endpoint` field in the
                    # body is ignored here and refused by the adapter if it names an argument the entry does not
                    # declare: the window cannot approve itself, and cannot choose a route.
                    out, code = s.call(b.get("name"), b.get("args") if isinstance(b.get("args"), dict) else {})
                    return self._json(out, code)
                if p == "/api/approve":
                    out, code = s.approve(str(b.get("id") or ""), str(b.get("code") or ""))
                    return self._json(out, code)
                if p == "/api/board":
                    out, code = s.board_write(b)
                    return self._json(out, code)
                if p == "/api/chat":
                    out, code = s.chat(str(b.get("text") or ""))
                    return self._json(out, code)
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)

    return H


# ---------------------------------------------------------------------------------------------- starting
def who_answers(port: int, timeout: float = 1.5) -> dict | None:
    """Whatever is listening on 127.0.0.1:<port>, by its own /api/ping. None if nothing answers.

    Agtr's equivalent (service.py:1479-1484) asks the same question but then its Swift shell trusts the answer: a
    `name == "agtr-desktop"` from anything at all is enough for Service.swift:36 to call it "the service", and
    Service.swift:108-109 then sends it the token and the native key. A ping name is not authentication. Studio uses
    this only to decide whether to start — never to decide whom to trust with a secret.
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            out = json.loads(r.read(8192))
        return out if isinstance(out, dict) else {"name": None}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _follow_parent(srv, every: float = 2.0) -> threading.Thread | None:
    """Stop when the shell that started this service is gone. Does nothing unless it was started by one.

    Force-quitting the app (SIGKILL) skips `applicationWillTerminate`, so `Service.stop()` never runs and this
    service is orphaned onto pid 1, still holding 7810 (measured 2026-11-19). The next launch then refuses to attach
    to it — correctly, since it is a service this app did not start and would be handed this install's token — and
    the person is left with an alert and a port they have to free by hand.

    The shell passes its own pid as BIOQL_STUDIO_PARENT. When our parent is no longer that pid we are an orphan and
    stop. A service started from a terminal, by `nohup`, or by a test sets no such variable and is never touched: it
    does not belong to a window and must outlive the shell that typed it.
    """
    raw = os.environ.get("BIOQL_STUDIO_PARENT")
    if not (raw or "").isdigit():
        return None
    parent = int(raw)

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(every)
        print(f"the app that started this service (pid {parent}) is gone; stopping and freeing the port", flush=True)
        srv.shutdown()

    t = threading.Thread(target=watch, name="studio-parent", daemon=True)
    t.start()
    return t


def run(home: Path | None = None, port: int = PORT, block: bool = True, adapter: Adapter | None = None):
    """Start the service, or explain why it is not starting. Returns (studio, server) or None."""
    seen = who_answers(port)
    if seen is not None:
        if seen.get("name") == PING_NAME:
            print(f"BioQL Studio is already running · http://127.0.0.1:{port}", flush=True)
            return None
        # Something else holds Studio's port — Agtr Desktop, BioQL, anything. Studio does NOT attach to it and does
        # not send it a byte of its own: it says what is there and stops. This is the bug this fork exists to avoid.
        print(f"port {port} is held by {seen.get('name') or 'something that is not BioQL Studio'!r}. "
              f"BioQL Studio will not attach to it. Free the port, or set a different one.", flush=True)
        return None
    s = Studio(home=home, port=port, adapter=adapter)
    srv = ThreadingHTTPServer(("127.0.0.1", s.port), make_handler(s))       # the loopback address only, never every
                                                                            # interface, and never a LaunchAgent
    srv.daemon_threads = True
    print(f"BioQL Studio {VERSION} · http://127.0.0.1:{s.port} · data in {s.home} · BioQL at {s.adapter.base}",
          flush=True)
    print(f"open the window with:\n  open \"http://127.0.0.1:{s.port}/#t=$(cat '{s.home / 'ui.token'}')\"", flush=True)
    if not block:
        threading.Thread(target=srv.serve_forever, name="studio-ui", daemon=True).start()
        return s, srv

    # Configs left by services that are gone. The app quits this one with SIGTERM, so `brain.forget()` — which runs
    # from atexit — never gets to delete the one it wrote; they accumulate one per launch (measured 2026-11-19: six).
    brain.sweep()

    def _bye(signum, _frame):
        """SIGTERM is how the native shell stops this service (`Service.swift::stop`). Python does not run atexit
        handlers on it, so without this the service dies mid-flight and leaves its MCP config behind. `shutdown()`
        must not be called from the handler's own thread — it waits for the serving loop that this very thread is
        running — so it goes to a thread of its own and `serve_forever` returns normally."""
        threading.Thread(target=srv.shutdown, name="studio-bye", daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _bye)
        except ValueError:
            pass                        # not the main thread: the caller owns the signals

    _follow_parent(srv)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        brain.forget()                  # the config of THIS service, whichever way the loop ended
    return s, srv
