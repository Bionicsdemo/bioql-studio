"""The adapter: the only piece of Studio that talks to BioQL. A plain process with no language model in it.

It calls BioQL the way BioQL's own UI does — HTTP to the backend on 127.0.0.1:5035 — through the closed allowlist,
and turns every answer into a Resolution (RESOLVED / ABSENT / UNRESOLVABLE). It never imports BioQL, never writes in
BioQL's folders, and never chooses a route from what a model says: the route is the allowlist's.

Rules held here, in code:
- an entry that is missing, disabled or malformed is refused;
- unknown arguments are refused (no smuggled parameters);
- anything that could reach a real QPU — a "hardware" entry, an entry marked for approval, or arguments that ask for
  real hardware — is refused unless a person approved this call (`approved_by`); the model cannot supply that;
- a timeout, a refused connection, a 5xx or a body that is not JSON is UNRESOLVABLE, never "no result";
- provenance (is_real_hardware, IBM job ids, quantum backend) is copied from BioQL's answer when BioQL states it, and
  is null when it does not: never inferred;
- every call, refused or not, is one line of the hash-chained ledger.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import NAME, VERSION, bioql_url
from . import allowlist as al
from . import ledger
from .resolution import Resolution, absent, resolved, unresolvable

SOURCE = "BioQL backend"
MAX_BODY = 64 * 1024 * 1024
RUNNING = {"queued", "pending", "submitted", "running", "started", "in_progress", "processing"}
DONE = {"completed", "complete", "done", "success", "succeeded", "finished"}
FAILED = {"failed", "error", "errored", "cancelled", "canceled", "timeout", "timed_out"}
HARDWARE_KEYS = {"use_real_hardware", "real_hardware", "hardware", "use_hardware", "qpu", "use_qpu", "run_on_hardware"}
BACKEND_KEYS = {"backend", "quantum_backend", "ibm_backend", "backend_name", "device"}
HARDWARE_WORDS = re.compile(r"\b(ibm_|ibmq|fez|kingston|marrakesh|torino|heron|iqm|garnet|ionq|quantinuum|rigetti|aws_braket)", re.I)


class Refused(PermissionError):
    """Studio will not make this call. Not a fact about BioQL: a rule of Studio."""


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy: localhost only


def wants_hardware(args: dict) -> bool:
    """Arguments that ask for a real quantum device. Matched on whole key names, never substrings."""
    for k, v in (args or {}).items():
        key = str(k).lower()
        if key in HARDWARE_KEYS and v not in (False, None, 0, "", "false", "no", "0", "simulator", "sim"):
            return True
        if key in BACKEND_KEYS and isinstance(v, str) and HARDWARE_WORDS.search(v):
            return True
    return False


def provenance(body: Any, limit: int = 8) -> dict:
    """What BioQL itself said about where the work ran. Keys matched as whole names; absent keys stay null."""
    real: list = []
    jobs: list[str] = []
    backends: list[str] = []

    def walk(x: Any, depth: int) -> None:
        if depth > limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if key == "is_real_hardware" and isinstance(v, bool):
                    real.append(v)
                elif key in ("ibm_job_id", "ibm_job_ids", "qpu_job_id", "hardware_job_id") and v:
                    for j in (v if isinstance(v, list) else [v]):
                        if isinstance(j, str) and j and j not in jobs:
                            jobs.append(j)
                elif key in ("quantum_backend", "backend_name", "ibm_backend") and isinstance(v, str) and v:
                    if v not in backends:
                        backends.append(v)
                walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:500]:
                walk(v, depth + 1)

    walk(body, 0)
    said = sorted(set(real))
    return {"is_real_hardware": None if not said else (said[0] if len(said) == 1 else "mixed"),
            "ibm_job_ids": jobs, "quantum_backends": backends}


class Adapter:
    def __init__(self, base: str | None = None, entries: dict[str, dict] | None = None,
                 opener: Callable | None = None, poll_every: float = 5.0, log: bool = True) -> None:
        self.base = (base or bioql_url()).rstrip("/")
        self.entries = entries if entries is not None else al.load()
        self.open = (opener or _opener()).open
        self.poll_every = poll_every
        self.log = log

    # ------------------------------------------------------------------ what may be called
    def tools(self) -> list[dict]:
        return [e for e in self.entries.values() if e["enabled"] and not al.problems(e)]

    def _entry(self, name: str) -> dict:
        e = self.entries.get(name)
        if e is None:
            raise Refused(f"{name!r} is not in Studio's allowlist")
        if not e["enabled"]:
            raise Refused(f"{name!r} is in the allowlist but not enabled")
        bad = al.problems(e)
        if bad:
            raise Refused(f"{name!r}: " + "; ".join(bad))
        return e

    def _url(self, e: dict, args: dict) -> tuple[str, dict]:
        rest = dict(args)
        def fill(m: re.Match) -> str:
            k = m.group(1)
            v = str(rest.pop(k, ""))
            if not al.ID.match(v):
                raise Refused(f"{k!r} must look like an id (letters, digits, . _ -)")
            return urllib.parse.quote(v, safe="")
        return self.base + al.PARAM.sub(fill, e["path"]), rest

    def check(self, name: str, args: dict, approved_by: str | None = None) -> dict:
        """The entry, if this call may be made. Raises Refused otherwise."""
        e = self._entry(name)
        if not isinstance(args, dict):
            raise Refused("arguments must be an object")
        params = set(al.PARAM.findall(e["path"]))
        allowed = set(e["required"]) | set(e["optional"]) | params
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise Refused(f"{name!r} does not take {unknown} (it takes {sorted(allowed) or 'nothing'})")
        missing = [k for k in e["required"] if args.get(k) in (None, "")]
        if missing:
            raise Refused(f"{name!r} needs {missing}")
        if (e["approval"] or e["kind"] == "hardware" or wants_hardware(args)) and not (approved_by or "").strip():
            raise Refused(f"{name!r} can spend real quantum hardware or was marked for approval: a person must approve this call")
        return e

    # ------------------------------------------------------------------ calling
    def call(self, name: str, args: dict | None = None, approved_by: str | None = None, by: str = "owner") -> Resolution:
        args = dict(args or {})
        began = time.time()
        try:
            e = self.check(name, args, approved_by)
            url, body = self._url(e, args)
        except Refused as why:
            self._note(name, None, args, "REFUSED", str(why), None, None, began, by, approved_by)
            raise
        res, http = self._http(e["method"], url, body if e["method"] == "POST" else None, e["timeout"], e["absent_on_404"])
        if res.resolved and isinstance(res.value, dict):
            res = self._follow(res, began + e["timeout"])
        prov = provenance(res.value) if res.resolved else {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
        res = Resolution(res.state, res.source, res.detail, res._value, {**res.meta, "provenance": prov, "tool": name,
                                                                          "seconds": round(time.time() - began, 3)})
        self._note(name, e, args, res.state, res.detail, http, res, began, by, approved_by)
        return res

    def _http(self, method: str, url: str, body: dict | None, timeout: float, absent_on_404: bool) -> tuple[Resolution, int | None]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": f"{NAME}/{VERSION}"})
        try:
            with self.open(req, timeout=timeout) as r:
                raw = r.read(MAX_BODY + 1)
                status = r.status
        except urllib.error.HTTPError as err:
            status = err.code
            text = (err.read(2000) or b"").decode("utf-8", "replace")
            if status == 404 and absent_on_404:
                return absent(SOURCE, f"404 at {urllib.parse.urlsplit(url).path}", http=status), status
            return unresolvable(SOURCE, f"HTTP {status}: {text[:300]}", http=status), status
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return unresolvable(SOURCE, f"could not reach BioQL: {getattr(err, 'reason', err)}"), None
        if len(raw) > MAX_BODY:
            return unresolvable(SOURCE, "answer larger than 64 MB", http=status), status
        try:
            out = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return unresolvable(SOURCE, f"answer is not JSON ({len(raw)} bytes)", http=status), status
        if isinstance(out, dict) and out.get("error") and out.get("ok") is not True and out.get("success") is not True:
            return unresolvable(SOURCE, f"BioQL reported an error: {str(out.get('error'))[:300]}", http=status), status
        if out is None:
            return absent(SOURCE, "BioQL answered null", http=status), status
        return resolved(out, SOURCE, f"HTTP {status}", http=status), status

    def _follow(self, res: Resolution, deadline: float) -> Resolution:
        """A submitted job is followed on BioQL's own job route until it ends. Only that route, never a URL the
        answer names."""
        job = res.value
        state = str(job.get("status") or job.get("state") or "").lower()
        jid = job.get("job_id")
        if not (isinstance(jid, str) and al.ID.match(jid) and state in RUNNING):
            return res
        url = f"{self.base}/api/pipeline/job/{urllib.parse.quote(jid, safe='')}"
        while time.time() < deadline:
            time.sleep(self.poll_every)
            nxt, _ = self._http("GET", url, None, 60, absent_on_404=True)
            if not nxt.resolved:
                return Resolution(nxt.state, nxt.source, f"job {jid}: {nxt.detail}", None, {"job_id": jid})
            cur = nxt.value
            s = str(cur.get("status") or cur.get("state") or "").lower() if isinstance(cur, dict) else ""
            if s in DONE:
                return resolved(cur, SOURCE, f"job {jid} {s}", job_id=jid)
            if s in FAILED:
                return unresolvable(SOURCE, f"job {jid} {s}: {str(cur.get('error') or '')[:300]}", job_id=jid)
        return unresolvable(SOURCE, f"job {jid} still running at the deadline", job_id=jid)

    def _note(self, name, e, args, state, detail, http, res, began, by, approved_by) -> None:
        if not self.log:
            return
        ledger.append({"tool": name, "method": e and e["method"], "path": e and e["path"], "args": args,
                       "state": state, "detail": (detail or "")[:300], "http": http,
                       "result_sha256": ledger.digest(res.value) if res is not None and res.resolved else None,
                       "provenance": res.meta.get("provenance") if res is not None else None,
                       "seconds": round(time.time() - began, 3), "by": by, "approved_by": approved_by or None})
