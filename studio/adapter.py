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

import http.client
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
from . import verify
from .resolution import Resolution, absent, resolved, unresolvable

SOURCE = "BioQL backend"
MAX_BODY = 64 * 1024 * 1024
RUNNING = {"accepted", "queued", "pending", "submitted", "running", "started", "in_progress", "processing"}
DONE = {"completed", "complete", "done", "success", "succeeded", "finished"}
FAILED = {"failed", "error", "errored", "cancelled", "canceled", "timeout", "timed_out"}
# `real_qpu` and `backend` are BioQL's own parameter names, read from its live registry (2026-11-19); the rest are
# spellings guarded against in advance. A name this set misses is caught by the kind: every `compute` entry asks.
HARDWARE_KEYS = {"real_qpu", "use_real_hardware", "real_hardware", "hardware", "use_hardware", "qpu", "use_qpu",
                 "run_on_hardware", "on_hardware"}
BACKEND_KEYS = {"backend", "quantum_backend", "ibm_backend", "backend_name", "device"}
HARDWARE_WORDS = re.compile(r"\b(ibm_|ibmq|fez|kingston|marrakesh|torino|heron|iqm|garnet|ionq|quantinuum|rigetti|aws_braket)", re.I)


class Refused(PermissionError):
    """Studio will not make this call. Not a fact about BioQL: a rule of Studio."""


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy: localhost only


def wants_hardware(args: dict, depth: int = 0) -> bool:
    """Arguments that ask for a real quantum device. Matched on whole key names, never substrings, and NESTED:
    reading only the top level let `{"opts": {"real_qpu": true}}` through the hardware gate."""
    if depth > 6:
        return False
    for k, v in (args or {}).items():
        key = str(k).lower()
        if key in HARDWARE_KEYS and v not in (False, None, 0, "", "false", "no", "0", "simulator", "sim"):
            return True
        if key in BACKEND_KEYS and isinstance(v, str) and HARDWARE_WORDS.search(v):
            return True
        if isinstance(v, dict) and wants_hardware(v, depth + 1):
            return True
        if isinstance(v, list):
            for item in v[:50]:
                if isinstance(item, dict) and wants_hardware(item, depth + 1):
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
                elif key in ("quantum_backend", "backend_name", "ibm_backend", "backend") and isinstance(v, str) and v:
                    # The plain key `backend`, nested in a stage, is where several BioQL pipelines record the lane
                    # they actually ran on. Reading only `quantum_backend` meant a report built from provenance
                    # could not name the device the body itself named.
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
            if e["via"] == "submit":
                # BioQL's own async door; the endpoint is this entry's, from the allowlist, never from the caller.
                url, body = self.base + al.SUBMIT_PATH, {"endpoint": e["path"], "params": body}
        except Refused as why:
            self._note(name, None, args, "REFUSED", str(why), None, None, began, by, approved_by)
            raise
        res, http = self._http(e["method"], url, body if e["method"] == "POST" else None, e["timeout"], e["absent_on_404"])
        if e["via"] == "submit" and res.resolved and isinstance(res.value, dict):
            res = self._follow(res, began + e["timeout"])
        prov = provenance(res.value) if res.resolved else {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
        # A result is not finished when it arrives, only when it has been checked. The findings travel beside the
        # science; nothing in the answer is altered.
        # Every answer, with no exempt kind: the science of a finished job arrives through a *status* route
        # (/api/pipeline/job/<id>/result), so exempting status reads would skip exactly the body that matters.
        checked = verify.check(res.value, prov) if res.resolved else []
        res = Resolution(res.state, res.source, res.detail, res._value,
                         {**res.meta, "provenance": prov, "tool": name, "seconds": round(time.time() - began, 3),
                          "verdict": verify.verdict(checked) if res.resolved else None, "findings": checked})
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
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as err:
            # IncompleteRead and the rest of HTTPException are NOT OSError, so a body cut short — plausible on
            # BioQL's streamed keepalive — escaped as a bare exception instead of becoming UNRESOLVABLE. A caller
            # would have seen a crash where the honest answer is "we could not read it".
            return unresolvable(SOURCE, f"could not reach BioQL: {getattr(err, 'reason', err)}"), None
        if len(raw) > MAX_BODY:
            return unresolvable(SOURCE, "answer larger than 64 MB", http=status), status
        try:
            # BioQL's streaming keepalive can leave a BOM and NUL bytes in the body; its own UI strips both before
            # parsing (src/frontend/src/components/chat/ChatInterface.tsx), so Studio does the same.
            out = json.loads(raw.decode("utf-8-sig").replace("\0", "").strip())
        except (UnicodeDecodeError, ValueError):
            return unresolvable(SOURCE, f"answer is not JSON ({len(raw)} bytes)", http=status), status
        if isinstance(out, dict) and out.get("error") and out.get("ok") is not True and out.get("success") is not True:
            return unresolvable(SOURCE, f"BioQL reported an error: {str(out.get('error'))[:300]}", http=status), status
        if out is None:
            return absent(SOURCE, "BioQL answered null", http=status), status
        return resolved(out, SOURCE, f"HTTP {status}", http=status), status

    def _follow(self, res: Resolution, deadline: float) -> Resolution:
        """A submitted job is followed on BioQL's own job route until it ends, and its result is then fetched from
        that job's own result route. Both paths are built from the job id — never from a URL the answer names.

        A submit is answered with a RECEIPT (`{job_id, status: "accepted", poll_url}`), which carries no science at
        all. So the receipt is never a result: a body with a job id is followed unless its state is already final,
        and a job id that cannot be followed is UNRESOLVABLE rather than passed back as if it were an answer.
        """
        job = res.value
        jid = job.get("job_id")
        if not isinstance(jid, str) or not al.ID.match(jid):
            return unresolvable(SOURCE, f"submitted, but the answer carries no job id to follow: {sorted(job)[:8]}")
        quoted = urllib.parse.quote(jid, safe="")
        url = f"{self.base}/api/pipeline/job/{quoted}"
        state = str(job.get("status") or job.get("state") or "").lower()
        cur = job
        while True:
            if state in DONE:
                return self._result_of(cur, jid, quoted)
            if state in FAILED:
                return unresolvable(SOURCE, f"job {jid} {state}: {str(cur.get('error') or '')[:300]}", job_id=jid)
            if state and state not in RUNNING:
                # A state Studio does not know is not a result: it is reported, never read as success.
                return unresolvable(SOURCE, f"job {jid} reports an unknown state {state!r}", job_id=jid)
            if time.time() >= deadline:
                return unresolvable(SOURCE, f"job {jid} still {state or 'running'} at the deadline", job_id=jid)
            time.sleep(self.poll_every)
            nxt, _ = self._http("GET", url, None, 60, absent_on_404=True)
            if not nxt.resolved:
                return Resolution(nxt.state, nxt.source, f"job {jid}: {nxt.detail}", None, {"job_id": jid})
            cur = nxt.value if isinstance(nxt.value, dict) else {}
            state = str(cur.get("status") or cur.get("state") or "").lower()

    def _result_of(self, job: dict, jid: str, quoted: str) -> Resolution:
        """A finished job's science. BioQL keeps a large result on the job's own result route and leaves `result`
        out of the status body (95 988 bytes for one ADMET run, 2026-11-19), so it is fetched from the route built
        from the job id — as BioQL's own UI does."""
        if job.get("result") not in (None, {}, []):
            return resolved(job, SOURCE, f"job {jid} completed", job_id=jid)
        if not (job.get("result_available") or job.get("result_url")):
            return unresolvable(SOURCE, f"job {jid} completed but reports no result", job_id=jid)
        got, _ = self._http("GET", f"{self.base}/api/pipeline/job/{quoted}/result", None, 300, absent_on_404=True)
        if not got.resolved:
            return Resolution(got.state, got.source, f"job {jid} result: {got.detail}", None, {"job_id": jid})
        return resolved({**job, "result": got.value}, SOURCE, f"job {jid} completed", job_id=jid)

    def _record(self, row: dict) -> dict:
        """Write one line. Signed when this Mac can sign — that is the lab notebook — and plainly chained when it
        cannot, which is reported by `notebook.verify` as UNSIGNED rather than passed off as signed. A signing
        failure never costs the line: the call still gets recorded."""
        try:
            from . import notebook
            return notebook.record(row)
        except Exception as why:                 # noqa: BLE001 - no crypto, no key, a broken key: all land here
            return ledger.append({**row, "unsigned_because": f"{type(why).__name__}: {why}"[:160]})

    def _note(self, name, e, args, state, detail, http, res, began, by, approved_by) -> None:
        if not self.log:
            return
        self._record({"tool": name, "method": e and e["method"], "path": e and e["path"], "args": args,
                       "state": state, "detail": (detail or "")[:300], "http": http,
                       "result_sha256": ledger.digest(res.value) if res is not None and res.resolved else None,
                       "provenance": res.meta.get("provenance") if res is not None else None,
                       # The verdict and its findings belong ON the line. Computing them and not writing them meant
                       # every row read back "verdict not recorded", and anything built on the ledger — the board,
                       # the notebook, a report — could not tell an unchecked result from a clean one.
                       "verdict": res.meta.get("verdict") if res is not None else None,
                       "findings": (res.meta.get("findings") or [])[:20] if res is not None else [],
                       "findings_total": len(res.meta.get("findings") or []) if res is not None else 0,
                       "job_id": (res.meta.get("job_id") or (res.value.get("job_id")
                                                             if res is not None and res.resolved
                                                             and isinstance(res.value, dict) else None))
                                 if res is not None else None,
                       "seconds": round(time.time() - began, 3), "by": by, "approved_by": approved_by or None})
