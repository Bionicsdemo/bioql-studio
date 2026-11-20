"""BioQL Studio's local service: one process on this Mac, on 127.0.0.1:7810 only.

A fork of Agtr Desktop's service (agtrdesktop/service.py), with its identity changed (see app/__init__.py) and the
security properties carried over deliberately rather than by copying. Each one names the Agtr line it came from, and
where Agtr's version has a hole, the hole is named too and closed here.

What it serves
    GET  /                 the window (app/web), three fixed files, no path joining at all
    GET  /api/ping         who is listening. The ONE route that needs no token (Agtr: service.py:1291)
    GET  /api/tools        the allowlist entries Studio may call (studio.adapter.Adapter.tools)
    POST /api/call         name + args -> Adapter.call(by="window"). Lo que puede tardar (compute, hardware o
                           la puerta asincrona de BioQL) arranca un Run en su propio hilo y devuelve 202 con su id,
                           exactly as it is for Claude Code through the MCP server
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
from studio import advanced
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
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          # 3Dmol.js 2.5.5, the same build BioQL's own window uses, copied into `app/web/vendor` with its licence
          # and its sha256 (see vendor/PROCEDENCIA.txt). It is served from here, not from a CDN: the CSP says
          # `script-src 'self'` and an app whose whole design is 127.0.0.1 does not reach the internet to draw a
          # molecule. The map stays a closed list — a path is never built out of the request.
          "/vendor/3Dmol-min.js": ("vendor/3Dmol-min.js", "text/javascript; charset=utf-8"),
          # Mol* 5.10.1, el mismo build que declara el package.json de BioQL: superficies, seleccion, panel de
          # secuencia y medidas. Se carga SOLO cuando una tarjeta lo pide — son 4,7 MB y la ventana abre sin el.
          "/vendor/molstar.js": ("vendor/molstar.js", "text/javascript; charset=utf-8"),
          "/vendor/molstar.css": ("vendor/molstar.css", "text/css; charset=utf-8")}

# Agtr's CSP (agtrcore/app.py:607-612) allows 'unsafe-inline' styles and frames from ANY localhost port, because its
# window embeds VS Code and sandboxed canvases. Studio keeps the strict half — nothing inline, nothing loaded from
# outside — and, since 2026-11-20, opens NO door at all: there is no `frame-src`, because there is nothing to frame.
#
# It used to name BioQL's own window on 3000 and TargetRadar on 4000, and the Studio window wrapped BioQL's pages
# as its main panel. The owner asked for an app that does not reach :3000, and removing the directive is what makes
# that true rather than a promise: a page here cannot put :3000 in a frame even if a future line of JavaScript asks
# for one. The window is its own interface now, built on BioQL's own history routes over HTTP, which is the only
# way it ever touches BioQL.
# `'unsafe-eval'`: Mol* lo necesita. Medido en WebKit con esta misma configuracion ANTES de pedirlo — la
# violacion era `script-src <- eval @ /vendor/molstar.js` y el visor no llegaba a crear un canvas. Lo que abre es
# real: dentro de esta ventana, que lleva el token de la instalacion, una cadena puede volverse codigo. Por eso no
# se puso solo; el dueno lo autorizo el 2026-11-19. Lo que NO se abre sigue cerrado: nada se carga de fuera
# (`'self'`), no hay `'unsafe-inline'`, y `connect-src` sigue siendo solo este origen, asi que ni Mol* ni nada que
# corra aqui puede mandar un byte a ningun sitio.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self'; img-src 'self' data: blob:; "
       # `data:` en connect-src: Mol* lee asi sus propios recursos empotrados. Un `data:` se lleva su contenido
       # dentro, asi que no alcanza ningun host — esto no abre la red, sigue cerrada a este origen.
       "connect-src 'self' data:; worker-src 'self' blob:; "
       # No `frame-src`: `default-src 'none'` is what applies, so every frame is refused. This is the line that
       # says the app has no access to :3000, and it is enforced by the browser engine, not by a convention.
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
class Run:
    id: str
    tool: str
    # `args` y `started` entran aqui porque ahora una corrida se lanza sin que nadie la apruebe: la ventana ya no
    # tiene una pantalla de aprobacion donde leer QUE se va a correr, asi que la corrida tiene que decirlo ella.
    args: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)
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
        self.runs: dict[str, Run] = {}
        self.calls = 0
        self.refusals = 0
        self.approvals = 0
        self.turns: dict[str, dict] = {}            # the chat: one entry per turn, read back by polling
        self._lock = threading.Lock()
        # The guard's baseline for THIS run of the app. Its whole purpose is "did BioQL change while Studio was
        # open?", and that question needs a mark at the moment Studio opened. Without one the page compared the two
        # newest files in the folder, which on this Mac were both from the day before: it reported, in red and
        # permanently, a change that had happened 23 hours earlier and that somebody had already lived through.
        self._guard_lock = threading.Lock()
        self.guard_base: dict | None = None         # {"name", "t"} once taken
        self.guard_base_error: str | None = None
        # True from the moment the app decides to take a baseline until that baseline exists. Without this flag the
        # window could ask during those ~10 seconds, find no baseline, and fall back to "the two newest files in the
        # folder" — which on this Mac is yesterday's pair. Measured on 2026-11-20: the app took its baseline at
        # 11:11:09 and the Guard tab, drawn while it ran, showed yesterday's comparison in red for the rest of the
        # session, because this page does not redraw itself. "It is being taken" is an answer; the old pair is not.
        self.guard_base_pending = False
        # Updates. UNKNOWN until something has actually been asked: "not checked" and "nothing new" are different
        # answers and the window shows them differently.
        self.update = {"state": "UNKNOWN", "current": VERSION, "latest": None, "checked": None,
                       "staged": None, "detail": "the channel has not been asked yet"}
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
        return {"name": PING_NAME, "version": VERSION, "port": self.port, "home": str(self.home),
                "bioql": self.adapter.base,
                "up_seconds": round(time.time() - self.started, 1),
                "calls": self.calls, "refusals": self.refusals, "approvals": self.approvals,
                # Whether Settings is in the menu. It HIDES a page and locks nothing: this very route answers the
                # same with it off, which is why it is reported here rather than withheld. See studio/advanced.py.
                "advanced": advanced.is_on(self.home),
                "update": {"state": self.update.get("state"), "latest": self.update.get("latest"),
                           "detail": self.update.get("detail")},
                "runs": [r.public() for r in list(self.runs.values())[-RUN_MAX:]]}

    def busy(self) -> dict:
        """What would be lost by quitting right now. Read-only, and the ONE question the native shell asks before
        it closes the app.

        The policy lives here rather than in the shell: what counts as work in flight is Studio's business, it
        changes when Studio changes, and here it is under test. The shell gets counts and names, and decides only
        whether to put a sheet on the screen.

        A run is BioQL's, not Studio's. Quitting does not stop a pipeline — BioQL keeps running it — but Studio
        stops following it, so the result is never fetched, the verifier never reads it and no line for it reaches
        the record. That is what the person is being told before they lose it.
        """
        with self._lock:
            running = [{"id": r.id, "tool": r.tool} for r in self.runs.values() if r.state == "RUNNING"]
            chats = sum(1 for t in self.turns.values() if t.get("state") == "running")
        return {"running": running, "chats": chats, "busy": bool(running or chats)}

    # ------------------------------------------------------------------ calling BioQL
    def call(self, name: str, args: dict) -> tuple[dict, int]:
        """/api/call. Ejecuta, como la ventana de BioQL en :3000.

        Ya no hay puerta de aprobacion -- la razon esta escrita en `Adapter.check`. Lo que queda de aquella
        maquinaria es lo util: una llamada que puede tardar se va a su propio hilo con un Run, y la ventana sigue
        su estado en vez de quedarse colgada hasta el timeout de BioQL.
        """
        if not isinstance(name, str) or not isinstance(args, dict):
            return {"state": "REFUSED", "detail": "a call is {name: str, args: object}"}, 400
        try:
            e = self.adapter.check(name, args)      # makes no HTTP call and writes no ledger row
            bad = dotdot(e, args) or too_big(args)
            if bad:
                raise Refused(bad)
        except Refused as why:
            # Una negativa es una linea del registro, igual que lo es para Claude Code a traves del servidor MCP.
            # `check` no escribe nada a proposito, asi que la ventana escribe la suya: si no, una negativa desde
            # la ventana no dejaria ningun rastro.
            self._refused(name, args, str(why), event="call")
            return {"state": "REFUSED", "detail": str(why)}, 403
        # Lo que puede tardar NO se contesta en la misma peticion. Una corrida de peptido de esta maquina tardo
        # 70 minutos; con la puerta de aprobacion, eso iba por `_run` en su propio hilo. Al quitar la puerta ese
        # camino quedo huerfano y una llamada cara habria bloqueado la ventana hasta el timeout de BioQL, que son
        # 12 horas. Asi que lo decide el allowlist, no el llamante: `compute`, `hardware` o la puerta asincrona de
        # BioQL (`via: submit`) arrancan un Run y devuelven su id, como hace :3000.
        if e["kind"] in ("compute", "hardware") or e["via"] == "submit":
            # 24 hex, que es lo que exige `ID` en la ruta /api/run. Con 16 la corrida existia y no se podia
            # consultar: arrancaba, corria y la ventana recibia un 404 por su propio id.
            run = Run(id=secrets.token_hex(12), tool=name, args=dict(args))
            with self._lock:
                self.runs[run.id] = run
            threading.Thread(target=self._run, args=(run, name, dict(args)),
                             name=f"studio-run-{run.id}", daemon=True).start()
            return {"state": "RUNNING", "run": run.public(),
                    "detail": f"{name} corriendo en segundo plano; sigue su estado en /api/run"}, 202

        try:
            # `check` passes but `call` can still refuse: a `{job_id}` segment is filled only from an argument that
            # looks like an id, so `job_id: "../../api/scripts/execute"` is refused where the path is built, not here.
            res = self.adapter.call(name, args, approved_by=None, by="window", note={"window_event": "call"})
        except Refused as why:
            self._refused(name, args, str(why), event="call")
            return {"state": "REFUSED", "detail": str(why)}, 403
        self.calls += 1
        out = self._shape(res)
        if res.resolved:
            # The card is built HERE, beside the answer, so a result opened on a work page is the same card the
            # chat draws for the same result — one card machinery (`studio/cards.py`), not two. The window used to
            # get the payload and nothing else, which is why its pages could only ever show JSON.
            out["card"] = self._card_of({"value": res.value, "meta": {**res.meta, "tool": name}})
        return out, 200

    def _run(self, run: Run, name: str, args: dict) -> None:
        """Una llamada cara, en su propio hilo, con su registro de Run.

        Esto lo llamaba `approve`, y al quitar la puerta se quedo huerfano -- lo que habria dejado las corridas
        largas bloqueando la peticion de la ventana hasta 12 horas. Ahora lo llama `call()` directamente para
        todo lo que puede tardar, que es como la ventana de BioQL en :3000 se ha comportado siempre: lanzas, y
        la interfaz sigue el trabajo.
        """
        # `run.state` is written LAST, every way out of this function. /api/run is read without a lock, so a state
        # published before the verdict and the hash would let a reader see a finished run whose verdict is still
        # missing — and read that absence as "nothing was found", which is the failure this whole layer exists to
        # prevent. While it is "RUNNING" nothing about it is claimed at all.
        try:
            res = self.adapter.call(name, args, approved_by=None, by="window",
                                    note={"window_event": "background_call", "run_id": run.id})
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
        run.state = res.state

    # ------------------------------------------------------------------ the record
    def _refused(self, name, args: dict, why: str, event: str) -> None:
        self.refusals += 1
        ledger.append({"tool": name if isinstance(name, str) else None, "args": trim(args), "state": "REFUSED",
                       "by": "window", "window_event": event, "verdict": None, "detail": why[:300]})

    # There is no `_note` here any more, and there must not be one again.
    #
    # It wrote a SECOND ledger row for every window call. It was right to exist once: `studio.adapter._note`
    # recorded only `provenance`, so the verdict and the findings never reached the record and this window added
    # them. The adapter was fixed to record both — and this row survived the fix, so from then on every call from
    # the window was on the signed record twice: once complete, once with findings stripped of their `detail`.
    #
    # What that cost, measured on this Mac on 2026-11-20: Findings showed 92 rows where 46 calls had been made,
    # six "contradicted" for three distinct results, and half of them printed a bare code — `subspace_fraction_
    # missing` — with nothing a person could act on, because the duplicate had thrown the explanation away.
    #
    # The facts this row carried that the adapter's does not (which window event, which approval) now travel as
    # `note=` on the call itself and land on the one row.

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
            # `cards_of`, not `card`: BioQL's own window splits a result into a card per section and so does this.
            # With fewer than two sections it falls back to the single card, so a small answer is not wrapped in a
            # group of one.
            return cards.cards_of(value, tool=meta.get("tool"), meta=meta)
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

    # ------------------------------------------------------------------ updates
    def update_check(self, take: bool = True) -> dict:
        """Ask the channel, and — when `take` — fetch, verify and stage what it publishes.

        This is the ONLY place in Studio that reaches past 127.0.0.1. Every refusal along the way is kept as the
        answer: an update that cannot be verified is never installed, and never quietly treated as "nothing new".
        """
        from studio import release
        if os.environ.get("BIOQL_STUDIO_NO_UPDATES"):
            self.update = {**self.update, "state": "OFF", "checked": time.time(),
                           "detail": "BIOQL_STUDIO_NO_UPDATES is set: the channel is not asked at all"}
            return self.update
        res = release.latest(VERSION)
        now = time.time()
        if res.absent:
            self.update = {**self.update, "state": "CURRENT", "checked": now, "latest": None, "detail": res.detail}
            return self.update
        if not res.resolved:
            self.update = {**self.update, "state": "UNRESOLVABLE", "checked": now, "detail": res.detail}
            self.log(f"update check: {res.detail}")
            return self.update
        man = res.value
        self.update = {**self.update, "state": "AVAILABLE", "checked": now, "latest": man.get("version"),
                       "detail": res.detail, "notes": man.get("notes") or "", "sha256": man.get("sha256")}
        if not take:
            return self.update
        got = release.download(man, self.home / "updates")
        if not got.resolved:
            self.update = {**self.update, "state": "UNRESOLVABLE", "detail": got.detail}
            self.log(f"update download: {got.detail}")
            return self.update
        put = release.stage(Path(got.value), man["version"])
        try:
            Path(got.value).unlink(missing_ok=True)     # the image has been checked and copied out; keep neither
        except OSError:
            pass
        if not put.resolved:
            self.update = {**self.update, "state": "UNRESOLVABLE", "detail": put.detail}
            self.log(f"update stage: {put.detail}")
            return self.update
        self.update = {**self.update, "state": "READY", "staged": put.value, "detail": put.detail}
        self.log(f"update {man['version']} staged at {put.value}; it replaces the app when this one quits")
        return self.update

    def update_swap(self) -> None:
        """Arm the replacement, at the very end. Nothing is swapped while the app runs: this service re-reads its
        own web files from disk on every request, so replacing the bundle under it would serve the new page against
        the old service.

        The process it waits for is the PARENT — the .app — not this Python child, which dies first.
        """
        staged = (self.update or {}).get("staged")
        if not staged or not Path(staged).is_dir():
            return
        from studio import release
        try:
            parent = int(os.environ.get("BIOQL_STUDIO_PARENT") or 0) or os.getppid()
        except ValueError:
            parent = os.getppid()
        out = release.swap(Path(staged), pid=parent)
        self.log(f"update swap: {out.state} · {out.detail}")

    GUARD_BASE = "app-start"

    def guard_snapshot(self, name: str | None = None) -> dict:
        """Take ONE snapshot and save it. `name` is never the caller's: it is this method's own, so no request
        string ever reaches a path (`studio.guard.save` joins a name into one).

        It takes about ten seconds over the ~47 000 files BioQL is made of, measured on this Mac, so it runs under
        a lock: two at once would double that for both and prove nothing extra.
        """
        picked = self.GUARD_BASE if name == self.GUARD_BASE else time.strftime("reading-%Y%m%d-%H%M%S")
        if not self._guard_lock.acquire(blocking=False):
            return {"state": "UNRESOLVABLE", "source": "studio guard",
                    "detail": "a snapshot is already being taken; it reads about 47 000 files and takes ~10s"}
        try:
            began = time.time()
            snap = sguard.snapshot()
            path = sguard.save(picked, snap)
            took = round(time.time() - began, 1)
            self.log(f"guard snapshot {picked}: {sum(len(r['files']) for r in snap['roots'].values())} files in {took}s")
            return {"state": "RESOLVED", "source": "studio guard", "name": picked, "seconds": took,
                    "files": sum(len(r["files"]) for r in snap["roots"].values()), "path": str(path)}
        except Exception as e:                       # noqa: BLE001 - the cause, never a plausible empty snapshot
            return {"state": "UNRESOLVABLE", "source": "studio guard", "detail": f"{type(e).__name__}: {e}"[:200]}
        finally:
            self._guard_lock.release()

    def guard_baseline(self) -> None:
        """The mark this run of the app leaves when it opens. Runs in a thread: ten seconds is ten seconds, and the
        window must not wait on it to appear."""
        self.guard_base_pending = True
        out = self.guard_snapshot(self.GUARD_BASE)
        if out["state"] == "RESOLVED":
            try:
                self.guard_base = {"name": self.GUARD_BASE + ".json",
                                   "t": (self.home / "guard" / (self.GUARD_BASE + ".json")).stat().st_mtime}
            except OSError as e:
                self.guard_base_error = f"saved, then could not be stat'ed: {e}"
        else:
            self.guard_base_error = out.get("detail")
        self.guard_base_pending = False

    def secrets_report(self) -> dict:
        """Where Studio's own secrets are and how they are protected — never a byte of what is in them.

        This is the whole of the API Keys page, and the page says why: BioQL's own key routes are refused by name
        in the allowlist's DENY pattern (auth, admin, login, secrets, keys, token), so Studio holds no key of
        BioQL's and cannot read one. What it does hold is its own two: the window token and the notebook's signing
        key. A file that cannot be stat'ed is UNRESOLVABLE with its cause, never reported as a file that is not
        there — the difference decides whether somebody should go looking for it.
        """
        want = [("ui.token", "the per-install token this window carries. Anything holding it can call Studio."),
                ("notebook.key", "the Ed25519 private key that signs the record. A record signed by two keys is "
                                 "not a record."),
                ("notebook.pub", "its public half, which is what verifies the chain. Not a secret.")]
        out = []
        for name, what in want:
            f = self.home / name
            row = {"name": name, "what": what}
            try:
                st = f.stat()
            except FileNotFoundError:
                row.update({"state": "ABSENT", "detail": "not written yet", "mode": None})
            except OSError as why:                 # a denied stat is not an absent file
                row.update({"state": "UNRESOLVABLE", "detail": f"{type(why).__name__}: {why}"[:120], "mode": None})
            else:
                row.update({"state": "RESOLVED", "bytes": st.st_size, "mtime": st.st_mtime,
                            "mode": f"{st.st_mode & 0o777:04o}"})
            out.append(row)
        return {"home": str(self.home), "files": out}

    def manifest_report(self) -> dict:
        """What the LIVE BioQL process is running, from BioQL's own `runtime_manifest`, plus the one comparison it
        cannot make about itself.

        BioQL keeps two copies of most of its backend — the repo and `/opt/bioql` — and its own notes call an edit
        to the wrong one the trap that silently does nothing. `/api/system/manifest` measures that on the running
        process: every loaded module, where it was imported FROM, its hash, and whether its twin differs.

        Studio adds nothing to the science and changes none of BioQL's fields. It adds two things BioQL's answer
        cannot contain:

        **The direction, read off the data instead of the prose.** BioQL stamps every diverged module with one
        fixed sentence — "editar la copia del repo no cambia nada en esta ruta" — without regard for which copy is
        running. When `origin` is "promoted" the repo copy IS the one running and that sentence is backwards. So
        `runs_from` and `stale_twin` are derived here from `origin` and `executed_from`, and BioQL's sentence is
        passed through untouched beside them, flagged when the two disagree. Studio does not edit BioQL and does
        not repeat a claim BioQL's own fields contradict.

        **Whether the process actually loaded what is on disk.** `runtime_manifest.sha256_of` stats the path when
        asked, so a module edited after the backend started reports its NEW hash while still executing the OLD
        code. One comparison settles it — the process's start time against the file's mtime — and that is a local
        fact about a pid, which is why it lives in `studio.proc` with the three states kept apart. A pid that is
        gone, or a `ps` that could not be asked, leaves `loaded` as None: neither becomes a yes or a no.
        """
        from studio import proc as _proc

        res = self.adapter.call("api_system_manifest", {}, approved_by=None, by="window",
                                note={"window_event": "manifest"})
        out = self._shape(res)
        if not res.resolved:
            return out
        m = dict(res.value or {})
        pid = m.get("pid")
        started = _proc.started_at(int(pid)) if isinstance(pid, int) else _proc.unresolvable(
            "ps", "BioQL's manifest named no pid, so nothing can be asked about the process")
        rows = []
        for r in list(m.get("diverged") or []) + list(m.get("shadow_only") or []):
            path, origin = str(r.get("executed_from") or ""), str(r.get("origin") or "")
            row = dict(r)
            # Which tree is live and which is the stale copy, from `origin` — not from BioQL's sentence.
            row["runs_from"] = {"promoted": "repo", "shadow": "/opt/bioql"}.get(origin, "elsewhere")
            row["stale_twin"] = r.get("twin") if r.get("diverged") else None
            said = str(r.get("warning") or "")
            # BioQL's sentence claims editing the repo copy changes nothing. That holds only where the SHADOW copy
            # is the one running. Saying so is not correcting BioQL: it is refusing to repeat it where its own
            # `executed_from` says otherwise.
            row["warning_disagrees"] = bool(said and origin == "promoted"
                                            and "editar la copia del repo no cambia nada" in said)
            age = _proc.age_of(path) if path else _proc.absent("stat", "no path given")
            row["file"] = {"state": age.state, "detail": age.detail}
            row["loaded"] = None
            if started.resolved and age.resolved:
                row["file"]["at"] = age.value
                row["loaded"] = bool(age.value <= started.value)
                row["loaded_says"] = (
                    "written before this process started, so this is what it loaded" if row["loaded"] else
                    f"written {int(age.value - started.value)}s AFTER this process started: it is on disk and "
                    "this process has not read it")
            else:
                row["loaded_says"] = "cannot be told from here: " + "; ".join(
                    x.detail for x in (started, age) if not x.resolved)
            rows.append(row)
        out["value"] = {**m, "rows": rows,
                        "process": {"pid": pid, "state": started.state, "detail": started.detail,
                                    **({"started_at": started.value} if started.resolved else {})},
                        "hash_is_of": "the file on disk at the moment this was asked, not the bytes the process "
                                      "imported — which is why each row says whether the process loaded it"}
        return out

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
        # The pair is chosen to answer the question the guard exists for: has BioQL changed SINCE STUDIO OPENED?
        # The baseline is the mark this run of the app made when it started; the other side is the most recent
        # snapshot taken after it. Falling back to "the two newest files in the folder" is what made this page
        # report, for a whole day, a change from the day before.
        base = self.guard_base
        if base is None and self.guard_base_pending and not self.guard_base_error:
            return {"state": "ABSENT", "source": "studio guard", "pending": True,
                    "detail": "Studio is fingerprinting BioQL to mark where it stood when this window opened "
                              "(about 47 000 files, ten seconds). This fills by itself when it is done."}
        if base is not None:
            later = [p for p in snaps if p.name != base["name"] and p.stat().st_mtime > base["t"]]
            if not later:
                return {"state": "ABSENT", "source": "studio guard",
                        "detail": f"Studio took its baseline when it opened, at "
                                  f"{time.strftime('%H:%M', time.localtime(base['t']))}. Nothing has been compared "
                                  f"against it yet — take a reading and this fills.",
                        "baseline": base}
            older, newer = self.home / "guard" / base["name"], later[-1]
        elif self.guard_base_error:
            return {"state": "UNRESOLVABLE", "source": "studio guard",
                    "detail": f"Studio could not take its baseline when it opened, so there is nothing to compare "
                              f"against: {self.guard_base_error}"}
        elif len(snaps) < 2:
            return {"state": "ABSENT", "source": "studio guard",
                    "detail": f"{len(snaps)} saved snapshot(s) in {d}: a comparison needs two. "
                              f"Take them with: python3 -m studio guard snapshot <name>"}
        else:
            # No baseline (the command line, or a baseline still being taken): the two newest, said as what they are.
            older, newer = snaps[-2], snaps[-1]
        try:
            report = sguard.compare(json.loads(older.read_text()), json.loads(newer.read_text()))
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Unreadable is "we could not ask", never "nothing changed". The default is never clean.
            return {"state": "UNRESOLVABLE", "source": "studio guard",
                    "detail": f"{older.name} vs {newer.name}: {type(e).__name__}: {e}"}
        when = (f"{older.name} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(older.stat().st_mtime))})"
                f" vs {newer.name} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(newer.stat().st_mtime))})")
        if not report.get("readable", True):
            # One of the roots could not be read when its snapshot was taken — a permission macOS refused, most
            # likely. That is not a verdict about BioQL, and RESOLVED here would hand the window a report whose
            # `clean: false` reads as "BioQL changed". The third state does not degrade into the second.
            return {"state": "UNRESOLVABLE", "source": "studio guard",
                    "detail": f"{when}: part of BioQL could not be read — " + "; ".join(report["unreadable"][:3]),
                    "meta": {"report": report}}
        # WHEN the two snapshots were taken travels with the report. Without it the window drew a comparison of
        # two day-old files as though it were a reading of this minute, and "BioQL changed" could mean a change
        # from yesterday that somebody has already dealt with.
        taken = {"older": {"name": older.name, "t": older.stat().st_mtime},
                 "newer": {"name": newer.name, "t": newer.stat().st_mtime}}
        return {"state": "RESOLVED", "source": "studio guard", "detail": when, "value": report, "taken": taken}


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
                if p == "/api/busy":
                    # Lo que se perderia al cerrar. Solo lectura, y la unica pregunta que la app hace antes de
                    # cerrarse: la politica de que cuenta como trabajo en vuelo vive en Python, no en el Swift.
                    return self._json(s.busy())
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
                if p == "/api/connect":
                    # READ ONLY, and it stays that way. Registering BioQL edits the MCP configuration of the
                    # person's Claude clients — their whole setup — so it is a command they run with their own
                    # hands, not a button an app presses. This page says where it stands and what to type.
                    from studio import connect as _connect
                    return self._json({"clients": _connect.status(), "entry": _connect.entry(),
                                       "command": "python3 -m studio connect"})
                if p == "/api/brain":
                    # Where Claude Code is and what version. It does NOT probe: asking whether the account answers
                    # spends a turn of the person's own usage, so that is a button (POST), never a page load.
                    return self._json(brain.status())
                if p == "/api/update":
                    return self._json(s.update)
                if p == "/api/secrets":
                    # Where Studio's own secrets live and how they are protected. No contents, ever — not even
                    # truncated: a prefix of a token is still part of a token.
                    return self._json(s.secrets_report())
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
                if p == "/api/board":
                    out, code = s.board_write(b)
                    return self._json(out, code)
                if p == "/api/update/check":
                    # A POST: it reaches the internet and writes a staged bundle to disk. Opening a page must not.
                    return self._json(s.update_check(take=bool(b.get("take", True))))
                if p == "/api/advanced":
                    # Puts Settings in the menu or takes it out. Deliberately NOT guarded: there is nothing to
                    # guard it with. Studio has no users and no roles -- one token per install, readable by
                    # anyone with a session on this Mac -- so a check here would protect nothing and would make
                    # somebody believe it did. It hides a page; that claim is the whole claim.
                    return self._json(advanced.set_on(bool(b.get("on")), s.home))
                if p == "/api/manifest":
                    # A POST because it is a CALL to BioQL, and every call to BioQL is one line on the signed
                    # record. A page that asked this on being opened would write a line each time somebody
                    # clicked through the menu, and a record full of rows nobody asked for is a record nobody
                    # reads. So: a button, like the guard's reading.
                    return self._json(s.manifest_report())
                if p == "/api/guard/snapshot":
                    # A snapshot WRITES (one file in Studio's own folder), so it is a POST and never a GET: opening
                    # the Guard page must not change what the Guard page is about to report. The name is the
                    # service's own; nothing from this body reaches a path.
                    return self._json(s.guard_snapshot())
                if p == "/api/render3d":
                    # PyMOL, sobre una estructura que esta ventana ya tiene. No es una llamada a BioQL: no gasta
                    # su motor, no toca el 5035 y no entra en el registro firmado, que es de llamadas a BioQL.
                    # El guion de PyMOL esta fijo en `studio/render3d.py`; de aqui solo sale un nombre de estilo
                    # de una lista cerrada y un tamano, nunca una orden.
                    from studio import render3d
                    r = render3d.render(str(b.get("data") or ""), str(b.get("format") or ""),
                                        style=str(b.get("style") or "cartoon"),
                                        width=int(b.get("width") or 1000), height=int(b.get("height") or 760))
                    if r.resolved:
                        return self._json({"state": "RESOLVED", "source": "pymol", "detail": r.detail,
                                           "value": r.value})
                    return self._json({"state": r.state, "source": "pymol", "detail": r.detail}, 200)
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

    # The guard's baseline for this run, in a thread: ~10s over ~47 000 files, and the window must not wait on it.
    # Without it the Guard page has no mark of its own to compare against and falls back to whatever two files are
    # newest in the folder — which is how it spent a day reporting a change from the day before.
    s.guard_base_pending = True     # set HERE, before the thread exists, so there is no gap where the window
    threading.Thread(target=s.guard_baseline, name="studio-guard-baseline", daemon=True).start()

    # The update check, also in a thread. It is the one thing here that leaves this Mac, so it is said plainly in
    # the log and it can be turned off with BIOQL_STUDIO_NO_UPDATES without touching anything else.
    threading.Thread(target=s.update_check, name="studio-update", daemon=True).start()

    _follow_parent(srv)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        brain.forget()                  # the config of THIS service, whichever way the loop ended
        # LAST, after the loop has stopped: if a verified update is staged, arm the process that puts it in place
        # once the app itself has gone. Nothing is replaced while anything is still running.
        s.update_swap()
    return s, srv
