"""The adapter: the only piece of Studio that talks to BioQL. A plain process with no language model in it.

It calls BioQL the way BioQL's own UI does — HTTP to the backend on 127.0.0.1:5035 — through the closed allowlist,
and turns every answer into a Resolution (RESOLVED / ABSENT / UNRESOLVABLE). It never imports BioQL, never writes in
BioQL's folders, and never chooses a route from what a model says: the route is the allowlist's.

Rules held here, in code:
- an entry that is missing, disabled or malformed is refused;
- unknown arguments are refused (no smuggled parameters);
- an expensive entry is NOT refused: until 2026-11-20 a "hardware" entry, one marked for approval, or arguments
  asking for real hardware needed `approved_by`, and that gate is gone. It asked one owner, with one budget, for
  permission to spend their own money, while the decision that costs anything -- a real QPU submission -- is made
  server-side in BioQL. What remains here is `kind` and `wanted_hardware` on the recorded line;
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
from . import stages
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
        # Uncapped on purpose: a cap here can miss a later `is_real_hardware: False` and return True where the
        # truth is "mixed" — a limit that turns a contradiction into a clean hardware claim.
            for v in x:
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

    # An MCP client shows these tools to the model under ITS OWN namespace: Claude Desktop and Claude Code list
    # `backend_health` as `mcp__bioql__backend_health`. That prefix is the client's, invented at registration, and
    # it never reaches the server on a normal call — the protocol strips it. But `run_many` takes tool names as
    # ARGUMENTS, so the model writes down the only name it was ever shown, prefix and all, and every entry in the
    # batch came back "not in Studio's allowlist". That read like a missing tool or a dead backend. It was neither:
    # it was Studio refusing its own tools under the name its own clients display.
    #
    # The server part of the prefix is whatever the person called it when registering, so it is matched loosely and
    # only STRIPPED — never rewritten into something else. If the bare name is not in the allowlist either, the
    # refusal names the original, because a refusal that quietly renames what was asked for is worse than the bug.
    MCP_PREFIX = re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_.-]*__(?P<tool>.+)$")

    def _resolve(self, name: str) -> str:
        """The allowlist key for what the caller asked for, taking the client's namespace off if it is there."""
        if name in self.entries:
            return name
        m = self.MCP_PREFIX.match(name or "")
        if m and m.group("tool") in self.entries:
            return m.group("tool")
        return name

    def _entry(self, name: str) -> dict:
        e = self.entries.get(self._resolve(name))
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
        url = self.base + al.PARAM.sub(fill, e["path"])
        if e["method"] == "GET" and rest and e["via"] != "submit":
            # A GET carries its arguments in the query string, and `call` sends no body on a GET -- so until
            # 2026-11-20 these were built here and then DROPPED on the floor. 117 of the 144 GET entries were
            # affected: a caller could pass q="PD-L1" to a search route, get no refusal from Studio, and read
            # BioQL's own "q parameter required" as BioQL being broken. Six were measurably unusable this way
            # (pubmed_search, pubmed_drug_search, biorxiv_search, biorxiv_detail, targetradar_compound_image,
            # unmet_need_search). Silently discarding an argument is the same defect as a plausible empty result:
            # the call looks like it asked something it never asked.
            q = {k: v if isinstance(v, str) else ("" if v is None else
                 json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v))
                 for k, v in sorted(rest.items())}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
            rest = {}
        return url, rest

    def check(self, name: str, args: dict, approved_by: str | None = None) -> dict:
        """The entry, if this call may be made. Raises Refused otherwise."""
        e = self._entry(name)
        if not isinstance(args, dict):
            raise Refused("arguments must be an object")
        params = set(al.PARAM.findall(e["path"]))
        allowed = set(e["required"]) | set(e["optional"]) | params
        unknown = sorted(set(args) - allowed)
        if unknown and e["schema_known"]:
            raise Refused(f"{name!r} does not take {unknown} (it takes {sorted(allowed) or 'nothing'})")
        if unknown:
            # BioQL never declared this entry's parameters, so Studio does not know that these are wrong. Reading
            # "no declared parameters" as "takes nothing" refused every real argument and left the tool listed but
            # unusable. The reserved names stay refused: the caller never chooses the route.
            reserved = sorted(set(args) & {"endpoint", "path", "url", "params", "approved_by", "approve", "by"})
            if reserved:
                raise Refused(f"{name!r} will not take {reserved}: the route and the approval are the allowlist's, "
                              f"never the caller's")
        missing = [k for k in e["required"] if args.get(k) in (None, "")]
        if missing:
            raise Refused(f"{name!r} needs {missing}")
        # NO HAY PUERTA DE APROBACION. Studio ejecuta, como hace la ventana de BioQL en :3000.
        #
        # La habia, y la razon por la que se quito importa mas que la razon por la que estaba. Estaba porque BioQL
        # no dice de antemano si una corrida va a tocar QPU real, asi que Studio no podia saberlo antes de llamar
        # y preguntaba. Pero preguntar no era un control: el dueño de esta maquina, de su presupuesto de IBM y de
        # su asignacion de Pasqal es una sola persona trabajando sola. Lo unico que producia la pregunta era que
        # su propio Claude no pudiera usar su propio sistema -- `pipeline_mrna_therapeutic` y `replicon_pipeline`
        # se negaban en su cara, en su maquina, con su presupuesto.
        #
        # Y el 2026-11-20 dejo de ser siquiera necesaria: hasta esa manana la puerta de Studio era lo unico entre
        # una peticion y el QPU real, porque `pasqal_submit_neutral_atom` no comprobaba nada mas que presupuesto y
        # `route_workload` dejaba que el cuerpo de la peticion se autoaprobara. Las dos estan cerradas ahora, en el
        # servidor de BioQL, que es donde tiene que vivir la decision de gastar una asignacion escasa:
        # BIOQL_ALLOW_PASQAL_QPU, BIOQL_REAL_QPU_PRIMARY, el tope por llamada y el ledger de trabajos reales.
        # Una segunda puerta en el cliente, por encima de esas, solo estorbaba a su dueño.
        #
        # Lo que NO se quito, y es lo que de verdad protege: cada llamada sigue siendo una linea del registro
        # encadenado y firmado, con su tool, su kind, sus argumentos, su veredicto y si toco hardware real. El
        # control pasa de "pedir permiso antes" a "no poder negar despues", que es el control que esta app siempre
        # tuvo de verdad.
        return e

    # ------------------------------------------------------------------ calling
    def call(self, name: str, args: dict | None = None, approved_by: str | None = None, by: str = "owner",
             group: str | None = None, note: dict | None = None) -> Resolution:
        """`group` names a fan-out this call belongs to (see `studio/fanout.py`): several independent calls run at
        the same time. It goes on the line and changes nothing else — the gate, the verifier and the record are
        the same whether a call runs alone or beside five others.

        `note` is extra facts the CALLER knows and this module does not — which window event started the call, which
        approval unlocked it — merged onto the one row. It exists so a caller never has to write a SECOND row for
        the same call: Studio's window used to, back when this module recorded no verdict, and the duplicate
        survived the fix. Every window call was on the record twice, the copy carried findings with no `detail`,
        and anything counting the record counted each call twice.
        """
        args = dict(args or {})
        # One name on the record, whatever the caller typed. A client's namespace prefix is the client's label for
        # the same entry, so `mcp__bioql__admet_standalone` and `admet_standalone` must not become two tools in the
        # notebook or in anything that counts it.
        name = self._resolve(name)
        began = time.time()
        try:
            e = self.check(name, args, approved_by)
            url, body = self._url(e, args)
            if e["via"] == "submit":
                # BioQL's own async door; the endpoint is this entry's, from the allowlist, never from the caller.
                url, body = self.base + al.SUBMIT_PATH, {"endpoint": e["path"], "params": body}
        except Refused as why:
            self._note(name, None, args, "REFUSED", str(why), None, None, began, by, approved_by, group, note)
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
        self._note(name, e, args, res.state, res.detail, http, res, began, by, approved_by, group, note)
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
        pid: str | None = None
        try:
            while True:
                # Las etapas vivas, antes de cada decision: si la corrida acaba en esta vuelta, el
                # ultimo parte ya se publico y la ventana no se queda con la penultima etapa.
                pid = self._stages(jid, cur, pid)
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
        finally:
            # El parte es un adorno de una corrida EN CURSO. Terminada, la ciencia esta en el
            # resultado y en el cuaderno; dejarlo aqui pintaria una barra a medias para siempre.
            stages.publish(jid, None)

    def _stages(self, jid: str, job: dict, pid: str | None) -> str | None:
        """Publicar las etapas vivas de este trabajo, y devolver el `pipeline_id` ya emparejado.

        El trabajo y el pipeline tienen ids distintos y nada los conecta, asi que el id se
        empareja una vez (`stages.pipeline_of`) y luego se reusa. Ver `studio/stages.py`.

        NADA de lo que pase aqui puede costar la llamada: el progreso es un adorno y la ciencia
        no depende de el. Por eso todo fallo acaba en un parte sin etapas CON su causa, nunca en
        una excepcion que suba ni en una etapa inventada.
        """
        try:
            if pid is None:
                act, _ = self._http("GET", f"{self.base}/api/pipeline/active", None, 20, absent_on_404=True)
                if not act.resolved:
                    stages.publish(jid, stages.unavailable(f"/api/pipeline/active: {act.detail}"))
                    return None
                pid, why = stages.pipeline_of(act.value, job.get("target"), job.get("created_at"))
                if pid is None:
                    stages.publish(jid, stages.unavailable(why))
                    return None
            quoted = urllib.parse.quote(pid, safe="")
            got, _ = self._http("GET", f"{self.base}/api/pipeline/progress/{quoted}", None, 20, absent_on_404=True)
            if not got.resolved:
                # Un pipeline que se cae del registro al terminar es lo normal: se dice, no se finge.
                stages.publish(jid, stages.unavailable(f"progress/{pid}: {got.detail}"))
                return pid
            stages.publish(jid, stages.snapshot(got.value, pid, "target + ventana de arranque"))
            return pid
        except Exception as why:                 # noqa: BLE001 - un adorno no tumba una corrida
            stages.publish(jid, stages.unavailable(f"{type(why).__name__}: {why}"[:160]))
            return pid

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

    def record(self, row: dict) -> dict | None:
        """Write one line that is not a call: a fan-out's summary, for instance. It goes through the same signed
        notebook, with the same fallback, and is skipped entirely when this adapter does not log (the tests')."""
        return self._record(row) if self.log else None

    def _record(self, row: dict) -> dict:
        """Write one line. Signed when this Mac can sign — that is the lab notebook — and plainly chained when it
        cannot, which is reported by `notebook.verify` as UNSIGNED rather than passed off as signed. A signing
        failure never costs the line: the call still gets recorded."""
        try:
            from . import notebook
            return notebook.record(row)
        except Exception as why:                 # noqa: BLE001 - no crypto, no key, a broken key: all land here
            return ledger.append({**row, "unsigned_because": f"{type(why).__name__}: {why}"[:160]})

    def _note(self, name, e, args, state, detail, http, res, began, by, approved_by, group=None,
              note: dict | None = None) -> None:
        if not self.log:
            return
        self._record({"tool": name, "method": e and e["method"], "path": e and e["path"], "args": args,
                       # `kind` y `wanted_hardware` no estaban en la linea, y hasta hoy casi no hacian falta: una
                       # llamada de hardware no llegaba aqui sin que una persona la hubiera aprobado, asi que la
                       # aprobacion era la marca. Quitada la puerta, el registro es el unico control que queda, y
                       # un registro que no distingue un simulador de un chip real no sirve para la pregunta que
                       # se le va a hacer: que se gasto. `wanted_hardware` es lo que PEDIAN los argumentos; lo que
                       # de verdad corrio lo dice `provenance`, y los dos juntos es como se ve una discrepancia.
                       "kind": e and e["kind"], "wanted_hardware": wants_hardware(args) or None,
                       "state": state, "detail": (detail or "")[:300], "http": http,
                       "result_sha256": ledger.digest(res.value) if res is not None and res.resolved else None,
                       "provenance": res.meta.get("provenance") if res is not None else None,
                       # The verdict and its findings belong ON the line. Computing them and not writing them meant
                       # every row read back "verdict not recorded", and anything built on the ledger — the board,
                       # the notebook, a report — could not tell an unchecked result from a clean one.
                       "verdict": res.meta.get("verdict") if res is not None else None,
                       # Each finding keeps its `detail` — a code with no explanation is a page that says
                       # "quantum_label_vs_execution" and nothing a person can act on. It is capped at 300
                       # characters apiece because `ledger._last_hash` reads only the final 64 KB of the file to
                       # find the previous hash, and one oversized line would break the chain for every line after.
                       "findings": [{**x, "detail": str(x.get("detail") or "")[:300]}
                                    for x in (res.meta.get("findings") or [])[:20]] if res is not None else [],
                       "findings_total": len(res.meta.get("findings") or []) if res is not None else 0,
                       "job_id": (res.meta.get("job_id") or (res.value.get("job_id")
                                                             if res is not None and res.resolved
                                                             and isinstance(res.value, dict) else None))
                                 if res is not None else None,
                       "seconds": round(time.time() - began, 3), "by": by, "approved_by": approved_by or None,
                       "group": group, **(note or {})})
