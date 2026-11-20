"""The app shell's own service, proved on an ephemeral port against a FAKE BioQL.

Nothing here reaches 127.0.0.1:5035, 7810, 7790 or any other real port: the adapter is built with the `fake_bioql`
fixture's base URL and an opener that RECORDS every URL it is asked for, so a test can assert that no request ever
left the allowlist. Both directions are proved: the positives, and — mostly — the negatives.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import studio
from studio import allowlist as al
from studio import ledger
from studio.adapter import Adapter

from app import PING_NAME, PORT
from app import service
from app.service import (CSP, MAX_POST, STATIC, Empty, Studio, make_handler, read_secret, who_answers)


# ------------------------------------------------------------------ harness
class Recorder:
    """A real opener that remembers every URL. `Adapter` uses `opener.open`, so this stands in for one."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self._o = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return self._o.open(req, timeout=timeout)


def entries(**extra) -> dict:
    base = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    # a pipeline: `compute`, so the allowlist forces approval on it (studio/allowlist.py::_norm)
    base["pipe"] = al._norm("pipe", {"method": "POST", "path": "/api/quantum/peptide_design", "kind": "compute",
                                     "via": "submit", "optional": ["which"], "timeout": 30,
                                     "why": "a pipeline: refused without a person"})
    # and one that may reach a real QPU: never offered an approval from the window at all
    base["hw"] = al._norm("hw", {"method": "POST", "path": "/api/mixed", "kind": "hardware", "timeout": 30,
                                 "why": "may reach real quantum hardware"})
    for k, v in extra.items():
        base[k] = al._norm(k, v)
    return base


class Svc:
    """The service, running in this process on an ephemeral port, shut down at the end of the test."""

    def __init__(self, home, base, ents=None):
        self.rec = Recorder()
        ad = Adapter(base, ents if ents is not None else entries(), opener=self.rec, poll_every=0.05)
        self.s = Studio(home=home, port=0, adapter=ad, log=lambda m: self.logs.append(m))
        self.logs: list[str] = []
        self.s.log = lambda m: self.logs.append(m)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.s))
        self.port = self.srv.server_address[1]
        # the Host allowlist is built from the port actually bound, so an ephemeral port is checked just as strictly
        self.s.port = self.port
        self.s.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, path, body=None, token="auto", host=None, method=None, raw=None):
        url = self.base + path
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        if token == "auto":
            token = self.s.token
        if token is not None:
            r.add_header("X-Studio-Token", token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if host is not None:
            r.add_header("Host", host)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def j(self, *a, **k):
        code, raw, hdr = self.req(*a, **k)
        return code, json.loads(raw or b"{}"), hdr

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def svc(studio_home, fake_bioql):
    s = Svc(studio_home, fake_bioql)
    yield s
    s.stop()


# ------------------------------------------------------------------ identity: never Agtr Desktop's
def test_identity_is_its_own():
    assert PING_NAME == "bioql-studio" and PORT == 7810
    for bad in (3000, 5035, 7790, 7799, 7797, 7788, 7791):
        assert PORT != bad


def test_ping_names_studio_and_needs_no_token(svc):
    code, body, _ = svc.j("/api/ping", token=None)
    assert code == 200 and body["name"] == PING_NAME and body["port"] == svc.port
    assert "token" not in json.dumps(body).lower()


def test_ping_still_checks_the_host(svc):
    """Agtr answers /api/ping before any check (service.py:1291). Here the Host gate comes first."""
    code, body, _ = svc.j("/api/ping", token=None, host="evil.example")
    assert code == 403 and body == {"error": "forbidden"}


def test_the_single_instance_check_does_not_mistake_agtr_for_itself(studio_home):
    """Agtr's shell trusts whatever answers its port (Service.swift:36, 108-109) and hands it the token. Studio
    treats a foreign answer as a reason NOT to start."""
    class Agtr(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = json.dumps({"ok": True, "name": "agtr-desktop", "version": "0.9.93"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Agtr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        seen = who_answers(port)
        assert seen is not None and seen["name"] == "agtr-desktop"
        assert seen["name"] != PING_NAME                     # so app.service.run() reports it and stops
        from app.service import run
        assert run(home=studio_home, port=port) is None      # it did not attach, and started nothing
    finally:
        srv.shutdown()
        srv.server_close()
    assert who_answers(port, timeout=0.4) is None             # and nothing of Studio's is left listening


# ------------------------------------------------------------------ the token
def test_token_is_required_everywhere_but_ping(svc):
    for path in ("/api/state", "/api/tools", "/api/ledger", "/api/guard", "/api/run?id=" + "0" * 24):
        code, body, _ = svc.j(path, token=None)
        assert code == 403 and body == {"error": "forbidden"}, path
    for path in ("/api/call", "/api/approve"):
        code, body, _ = svc.j(path, {"name": "backend_health", "args": {}}, token=None)
        assert code == 403, path
    code, _, _ = svc.j("/api/tools", token="not-the-token-but-long-enough-to-pass-the-length-check")
    assert code == 403
    assert svc.j("/api/tools")[0] == 200                     # and the right one works


def test_an_empty_or_short_token_file_is_an_error_not_an_empty_secret(tmp_path):
    """The bug in the original: Agtr creates ui.token only when it is MISSING (service.py:124-128) and then compares
    the resulting "" against an absent header, which is also "" (service.py:1228-1229)."""
    h = tmp_path / "home"
    h.mkdir(mode=0o700)
    (h / "ui.token").write_text("")
    with pytest.raises(Empty):
        read_secret(h)
    (h / "ui.token").write_text("short\n")
    with pytest.raises(Empty):
        read_secret(h)
    (h / "ui.token").unlink()
    tok = read_secret(h)                                      # created privately, 0600 from the first byte
    assert len(tok) >= 32 and (os.stat(h / "ui.token").st_mode & 0o077) == 0
    os.chmod(h / "ui.token", 0o644)
    with pytest.raises(Empty):
        read_secret(h)


def test_an_empty_secret_in_memory_still_refuses(svc):
    """Belt and braces: even if the stored secret is emptied at runtime, an empty header does not match it."""
    svc.s._token = b""
    assert svc.s.matches_token("") is False and svc.s.matches_token(None) is False
    code, body, _ = svc.j("/api/tools", token="")
    assert code == 403 and body == {"error": "forbidden"}


def test_a_non_ascii_token_header_is_a_refusal_not_a_crash(svc):
    """Agtr compares str (service.py:1229), and secrets.compare_digest raises on a non-ASCII str: that becomes a 500
    from its catch-all. Here the comparison is on bytes."""
    code, body, _ = svc.j("/api/tools", token="tökén-" + "x" * 40)
    assert code == 403 and body == {"error": "forbidden"}


def test_the_query_token_fallback_is_gone(svc):
    """Agtr also accepts ?t=<token> (service.py:1228); a token in a URL lands in logs and in Referer headers."""
    code, body, _ = svc.j(f"/api/tools?t={svc.s.token}", token=None)
    assert code == 403 and body == {"error": "forbidden"}


def test_the_token_is_never_in_a_response(svc):
    """Agtr substitutes it into every .html it serves (service.py:1255). Studio's window reads it from the URL
    fragment instead, which no browser sends to a server."""
    for path, tok in (("/", None), ("/index.html", None), ("/app.js", None), ("/app.css", None), ("/api/ping", None),
                      ("/api/state", "auto"), ("/api/tools", "auto")):
        code, raw, _ = svc.req(path, token=tok)
        assert svc.s.token.encode() not in raw, path
    html = (studio.Path(__file__).resolve().parents[1] / "app" / "web" / "index.html").read_bytes()
    assert b"TOKEN" not in html             # no placeholder for a server to substitute a secret into


# ------------------------------------------------------------------ the Host gate and the headers
def test_a_foreign_host_header_is_refused_on_every_route(svc):
    for path, body in (("/", None), ("/app.js", None), ("/api/ping", None), ("/api/state", None),
                       ("/api/tools", None), ("/api/ledger", None), ("/api/guard", None),
                       ("/api/call", {"name": "backend_health", "args": {}}),
                       ("/api/approve", {"id": "x", "code": "y"})):
        code, out, _ = svc.j(path, body, host="bioql-studio.attacker.example")
        assert code == 403 and out == {"error": "forbidden"}, path
    assert svc.j("/api/state", host=f"localhost:{svc.port}")[0] == 200     # the other allowed spelling works


def test_every_response_carries_the_headers_not_only_the_html(svc):
    """Agtr adds the CSP, Referrer-Policy and X-Frame-Options only to .html (service.py:1256)."""
    for path, tok in (("/", None), ("/app.js", None), ("/api/ping", None), ("/api/state", "auto")):
        _, _, h = svc.req(path, token=tok)
        assert h["Cache-Control"] == "no-store" and h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer"
        assert h["Content-Security-Policy"] == CSP, path
    _, _, h = svc.req("/api/state", token=None)                            # a 403 too
    assert h["Content-Security-Policy"] == CSP and h["X-Frame-Options"] == "DENY"
    assert "'unsafe-inline'" not in CSP and "default-src 'none'" in CSP
    assert "bioql-studio" in (svc.req("/api/ping", token=None)[2].get("Server") or "")
    assert "agtr" not in (svc.req("/api/ping", token=None)[2].get("Server") or "").lower()


def test_a_body_larger_than_the_cap_is_refused(svc):
    code, body, _ = svc.j("/api/call", raw=b"{}" + b" " * (MAX_POST + 10))
    assert code == 413 and "at most" in body["error"]
    assert svc.j("/api/call", raw=b"not json")[0] == 400
    assert svc.j("/api/call", raw=b"[1,2,3]")[0] == 400


# ------------------------------------------------------------------ a pipeline is refused, with no approval
def test_a_pipeline_is_refused_from_the_window(svc, fake_log):
    code, body, _ = svc.j("/api/call", {"name": "pipe", "args": {}})
    assert code == 403 and body["state"] == "REFUSED" and "approve" in body["detail"]
    assert fake_log.submits == []                             # nothing was submitted to BioQL
    assert not any("/api/quantum" in u or "/api/pipeline/submit" in u for u in svc.rec.urls)


def test_the_body_cannot_approve_itself(svc, fake_log):
    """/api/call reads `name` and `args` and nothing else: an approval in the body changes nothing."""
    for extra in ({"approved_by": "Heinz"}, {"approve": True}, {"by": "owner"},
                  {"args": {"approved_by": "Heinz"}}, {"args": {"endpoint": "/api/exec"}}):
        payload = {"name": "pipe", "args": {}, **extra}
        code, body, _ = svc.j("/api/call", payload)
        assert code == 403 and body["state"] == "REFUSED", extra
    assert fake_log.submits == []


def test_real_hardware_is_never_offered_an_approval_from_the_window(svc):
    """A `hardware` entry, or an argument naming a real device, gets no pending at all: that stays with the command
    line and a named person."""
    code, body, _ = svc.j("/api/call", {"name": "hw", "args": {}})
    assert code == 403 and "pending" not in body
    ents = entries(sim={"method": "POST", "path": "/api/mixed", "kind": "local", "optional": ["backend"],
                        "timeout": 30, "why": "cheap, unless it is pointed at a real device"})
    s2 = Svc(svc.s.home, svc.s.adapter.base, ents)
    try:
        code, body, _ = s2.j("/api/call", {"name": "sim", "args": {"backend": "ibm_fez"}})
        assert code == 403 and "pending" not in body        # wants_hardware: refused, and not offered
        assert s2.j("/api/call", {"name": "sim", "args": {"backend": "aer_simulator"}})[1]["state"] == "RESOLVED"
    finally:
        s2.stop()


# ------------------------------------------------------------------ a cheap local entry works
def test_a_cheap_local_entry_works_against_the_fake(svc, fake_log):
    code, body, _ = svc.j("/api/call", {"name": "smiles_to_3d", "args": {"smiles": "CC(=O)Nc1ccc(O)cc1"}})
    assert code == 200 and body["state"] == "RESOLVED"
    assert body["value"]["mol"].startswith("SDF for CC(=O)")
    assert body["verdict"] == "clean" and body["findings"] == []
    assert body["provenance"] == {"is_real_hardware": None, "ibm_job_ids": [], "quantum_backends": []}
    assert svc.rec.urls == [svc.s.adapter.base + "/api/smiles-to-3d"]


def test_the_engine_health_the_window_shows(svc):
    code, body, _ = svc.j("/api/call", {"name": "backend_health", "args": {}})
    assert code == 200 and body["state"] == "RESOLVED" and body["value"]["status"] == "healthy"


def test_tools_are_exactly_the_allowlist(svc):
    code, body, _ = svc.j("/api/tools")
    assert code == 200
    assert {t["name"] for t in body["tools"]} == set(entries())
    assert all(t["approval"] for t in body["tools"] if t["kind"] in ("compute", "hardware"))


# ------------------------------------------------------------------ nothing reaches a path outside the allowlist
def test_an_unknown_or_crafted_tool_name_reaches_nothing(svc):
    for name in ("", "nope", "/api/scripts/execute", "../tools_registry", "backend_health ", "BACKEND_HEALTH",
                 "__init__", "tools", None, 7):
        code, body, _ = svc.j("/api/call", {"name": name, "args": {}})
        assert code in (400, 403) and body["state"] == "REFUSED", name
    assert svc.rec.urls == []


def test_an_argument_cannot_smuggle_a_route(svc):
    for args in ({"endpoint": "/api/scripts/execute"}, {"path": "/api/auth/login"}, {"url": "http://evil"},
                 {"smiles": "CCO", "endpoint": "/api/exec"}):
        code, body, _ = svc.j("/api/call", {"name": "smiles_to_3d", "args": args})
        assert code == 403 and "does not take" in body["detail"], args
    assert svc.rec.urls == []


def test_a_path_parameter_cannot_climb_out(svc):
    for jid in ("../../api/scripts/execute", "..", "a/b", "j1?x=1", "j1#f", "j1/../j2", ""):
        code, body, _ = svc.j("/api/call", {"name": "pipeline_job", "args": {"job_id": jid}})
        assert code == 403 and body["state"] == "REFUSED", jid
    assert svc.rec.urls == []
    code, body, _ = svc.j("/api/call", {"name": "pipeline_job", "args": {"job_id": "j1"}})
    assert code == 200 and svc.rec.urls == [svc.s.adapter.base + "/api/pipeline/job/j1"]


def test_a_denied_route_stays_denied_even_when_the_allowlist_file_names_it(studio_home, fake_bioql):
    """studio/allowlist.py::DENY. The window is no way around it either."""
    ents = entries()
    ents["sneaky"] = al._norm("sneaky", {"method": "POST", "path": "/api/scripts/execute", "kind": "local",
                                         "timeout": 10, "why": "the owner's own file asked for it"})
    s2 = Svc(studio_home, fake_bioql, ents)
    try:
        assert "sneaky" not in {t["name"] for t in s2.j("/api/tools")[1]["tools"]}
        code, body, _ = s2.j("/api/call", {"name": "sneaky", "args": {}})
        assert code == 403 and "never callable" in body["detail"]
        assert s2.rec.urls == []
    finally:
        s2.stop()


def test_the_static_server_has_no_path_to_climb(svc):
    """No request string becomes a filesystem path: a request names one of the keys or it is a 404."""
    for path in ("/../app/service.py", "/..%2f..%2fapp/service.py", "/%2e%2e/service.py", "/web/app.js",
                 "/app.js/", "/ui.token", "/runs.jsonl", "/../../ui.token", "/favicon.ico", "/nope.html",
                 # el directorio del visor no se puede recorrer: solo el fichero que esta en la lista
                 "/vendor/", "/vendor/SHA256.txt", "/vendor/../app.js", "/vendor/3Dmol-min.js.LICENSE.txt",
                 "/vendor/PROCEDENCIA.txt", "/vendor/molstar.js.map"):
        code, raw, _ = svc.req(path, token=None)
        assert code == 404, (path, code)
        assert b"def " not in raw and b"import " not in raw
    for path in ("/", "/index.html", "/app.js", "/app.css", "/vendor/3Dmol-min.js",
                 "/vendor/molstar.js", "/vendor/molstar.css"):
        code, raw, _ = svc.req(path, token=None)
        assert code == 200 and raw, path
    assert set(STATIC) == {"/", "/index.html", "/app.js", "/app.css", "/vendor/3Dmol-min.js",
                           "/vendor/molstar.js", "/vendor/molstar.css"}


def test_the_vendored_viewer_is_the_build_it_says_it_is(svc):
    """3Dmol viaja dentro de la app con su sha256 anotado. Un visor que cambia sin que nadie lo anote es una
    dependencia sin version, y es lo que separa "el mismo build que :3000" de una suposicion."""
    import hashlib
    web = Path(__file__).resolve().parents[1] / "app" / "web" / "vendor"
    got = hashlib.sha256((web / "3Dmol-min.js").read_bytes()).hexdigest()
    anotado = (web / "SHA256.txt").read_text().split()[0]
    assert got == anotado, "el visor del disco no es el que anota SHA256.txt"
    assert "3dmol v2.5.5" in (web / "3Dmol-min.js.LICENSE.txt").read_text(errors="replace").lower()
    assert "2.5.5" in (web / "PROCEDENCIA.txt").read_text()
    assert "$3Dmol" in (web / "3Dmol-min.js").read_text(errors="replace")
    code, raw, hdr = svc.req("/vendor/3Dmol-min.js", token=None)
    assert code == 200 and hashlib.sha256(raw).hexdigest() == got
    assert "javascript" in hdr.get("Content-Type", "")


def test_no_unknown_api_route_answers(svc):
    for path in ("/api/", "/api/exec", "/api/secret", "/api/stop", "/api/events", "/api/nope"):
        code, _, _ = svc.j(path)
        assert code == 404, path
    for path in ("/api/exec", "/api/stop", "/api/nope"):
        code, _, _ = svc.j(path, {"x": 1})
        assert code == 404, path


# ------------------------------------------------------------------ the approval, done honestly
def _pending(svc, name="pipe", args=None):
    code, body, _ = svc.j("/api/call", {"name": name, "args": args or {}})
    assert code == 403 and body["state"] == "REFUSED"
    return body.get("pending")


def test_an_approval_is_offered_but_its_code_is_never_sent_to_the_page(svc):
    p = _pending(svc)
    assert p and p["tool"] == "pipe" and not p["used"]
    assert "code" not in p and "code" not in json.dumps(p)
    code_file = svc.s.home / "pending" / p["id"]
    assert code_file.exists() and (os.stat(code_file).st_mode & 0o077) == 0
    assert any(p["id"] in line and code_file.read_text().strip() in line for line in svc.logs)


def test_a_wrong_code_an_unknown_id_and_a_replay_are_all_refused(svc, fake_log):
    p = _pending(svc)
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    for pid, code_ in ((p["id"], "deadbeef"), (p["id"], ""), ("0" * 24, real), ("../x", real)):
        st, body, _ = svc.j("/api/approve", {"id": pid, "code": code_})
        assert st == 403 and body["state"] == "REFUSED", (pid, code_)
    assert fake_log.submits == []
    st, body, _ = svc.j("/api/approve", {"id": p["id"], "code": real})     # the right one, once
    assert st == 200 and body["state"] == "APPROVED"
    st, body, _ = svc.j("/api/approve", {"id": p["id"], "code": real})     # and only once
    assert st == 403 and "single-use" in body["detail"]


def test_an_expired_approval_is_refused(svc, fake_log):
    p = _pending(svc)
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    svc.s.pending[p["id"]].created -= 10_000
    st, body, _ = svc.j("/api/approve", {"id": p["id"], "code": real})
    assert st == 403 and "expired" in body["detail"]
    assert fake_log.submits == []


def test_an_approval_runs_the_stored_call_and_nothing_else(svc, fake_log):
    p = _pending(svc, args={"which": "j1"})
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    st, body, _ = svc.j("/api/approve", {"id": p["id"], "code": real})
    assert st == 200 and body["run"] == p["id"] and body["tool"] == "pipe"
    run = _wait(svc, p["id"])
    assert run["state"] == "RESOLVED" and run["result_sha256"]
    # the arguments BioQL saw are the ones that were shown, and the endpoint is the allowlist's, not the caller's
    assert fake_log.submits == [{"endpoint": "/api/quantum/peptide_design", "params": {"which": "j1"}}]
    assert svc.s.adapter.base + "/api/pipeline/submit" in svc.rec.urls


def _wait(svc, rid, limit=10.0):
    end = time.time() + limit
    while time.time() < end:
        code, body, _ = svc.j("/api/run?id=" + rid)
        assert code == 200
        if body["state"] != "RUNNING":
            return body
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_an_unknown_run_id_is_a_404_and_never_a_path(svc):
    for rid in ("", "../../ui.token", "0" * 24, "nope", "0" * 64):
        code, _, _ = svc.j("/api/run?id=" + rid)
        assert code == 404, rid


# ------------------------------------------------------------------ the record
def test_one_row_per_call_carries_the_verdict_the_findings_and_their_detail(svc):
    """ONE line per call, complete. Not two, and not one of them stripped.

    The history: `studio.adapter._note` computed the verdict and did not write it, so the window added a second
    row of its own to carry it. The adapter was fixed; the second row survived the fix. From then on every window
    call was on the signed record TWICE — once complete, once with every finding's `detail` thrown away — so
    Findings showed 92 rows for 46 calls, counted one contradiction as two, and printed bare codes like
    `subspace_fraction_missing` with nothing a person could act on.

    What the window knows and the adapter does not (which event, which approval) now rides on `note=` and lands on
    the single row.
    """
    ents = entries(twin={"method": "POST", "path": "/api/mixed", "kind": "local", "timeout": 30,
                         "why": "provenance that contradicts itself"})
    s2 = Svc(svc.s.home, svc.s.adapter.base, ents)
    try:
        code, body, _ = s2.j("/api/call", {"name": "twin", "args": {}})
        assert code == 200 and body["verdict"] in ("concerns", "contradicted") and body["findings"]
        code, out, _ = s2.j("/api/ledger?limit=100")
        assert code == 200 and out["chain"]["ok"]
        mine = [r for r in out["rows"] if r.get("tool") == "twin"]
        assert len(mine) == 1, f"one call wrote {len(mine)} rows"
        row = mine[0]
        assert row["window_event"] == "call" and row["by"] == "window"
        assert row["verdict"] == body["verdict"] and row["verdict_recorded"] is True
        assert [f["code"] for f in row["findings"]] == [f["code"] for f in body["findings"]][:20]
        # the explanation travels WITH the code, bounded so one line cannot break the hash chain
        assert all(f.get("detail") for f in row["findings"]), "a finding with no detail is a code nobody can act on"
        assert all(len(f["detail"]) <= 300 for f in row["findings"])
        assert row["method"] == "POST" and row["path"] == "/api/mixed"      # the adapter's own facts, same line
    finally:
        s2.stop()


def test_the_refusal_and_both_sides_of_the_approval_are_on_the_record(svc):
    _pending(svc)
    p = _pending(svc)
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    svc.j("/api/approve", {"id": p["id"], "code": "wrong"})
    svc.j("/api/approve", {"id": p["id"], "code": real})
    _wait(svc, p["id"])
    rows = svc.j("/api/ledger?limit=200")[1]["rows"]
    events = [r.get("window_event") for r in rows if r.get("window_event")]
    assert "approval_offered" in events and "approval_denied" in events and "approval_granted" in events
    granted = [r for r in rows if r.get("window_event") == "approval_granted"][-1]
    # the mechanism, never a person and never a biometric claim
    assert granted["approved_by"] == f"window:{p['id']}"
    for word in ("verified", "touchid", "touch id", "biometric", "uv"):
        assert word not in json.dumps(granted).lower().replace("value", "")
    assert svc.j("/api/ledger")[1]["chain"]["ok"]


def test_no_ledger_row_carries_a_payload_or_the_token(svc):
    svc.j("/api/call", {"name": "smiles_to_3d", "args": {"smiles": "CCO"}})
    raw = ledger.path_file().read_bytes()
    assert b"SDF for CCO" not in raw and svc.s.token.encode() not in raw
    for line in raw.splitlines():
        assert len(line) < 60_000, "a line this long would break ledger._last_hash's 64 KB tail read"


# ------------------------------------------------------------------ the guard
def test_the_guard_route_takes_no_snapshot_and_never_defaults_to_clean(svc, monkeypatch):
    monkeypatch.setattr("studio.guard.snapshot", lambda *a, **k: pytest.fail("a GET must not take a snapshot"))
    code, body, _ = svc.j("/api/guard")
    assert code == 200 and body["state"] == "ABSENT" and "needs two" in body["detail"]
    assert "value" not in body

    d = svc.s.home / "guard"
    d.mkdir(parents=True, exist_ok=True)
    one = {"t": 1.0, "roots": {"r": {"path": "/x", "exists": True, "files": {"a.py": [1, 1, 1, "a" * 64]}}}}
    two = {"t": 2.0, "roots": {"r": {"path": "/x", "exists": True, "files": {"a.py": [2, 2, 1, "b" * 64]}}}}
    (d / "before.json").write_text(json.dumps(one))
    time.sleep(0.02)
    (d / "after.json").write_text(json.dumps(two))
    code, body, _ = svc.j("/api/guard")
    assert code == 200 and body["state"] == "RESOLVED"
    assert body["value"]["clean"] is False and body["value"]["roots"]["r"]["changed"] == ["a.py"]

    (d / "after.json").write_text("{ not json")
    code, body, _ = svc.j("/api/guard")
    assert code == 200 and body["state"] == "UNRESOLVABLE"       # could not ask: never "nothing changed"
    assert "value" not in body


def test_the_guard_route_takes_no_name_from_the_request(svc):
    """studio.guard.load joins a name into a path, so the route reads the directory itself."""
    src = (studio.Path(__file__).resolve().parents[1] / "app" / "service.py").read_text()
    body = src.split("def guard_report")[1].split("\n    def ")[0]
    assert "guard.load(" not in body and "sguard.load(" not in body
    code, _, _ = svc.j("/api/guard?name=../../ui.token")
    assert code == 200


# ------------------------------------------------------------------ what the service never does
def test_the_service_binds_only_localhost_and_never_a_port_that_is_someone_elses():
    src = (studio.Path(__file__).resolve().parents[1] / "app" / "service.py").read_text()
    assert 'ThreadingHTTPServer(("127.0.0.1"' in src and "0.0.0.0" not in src
    from app.__main__ import TAKEN, main
    for port in (3000, 5035, 7790, 7799, 7797, 7788, 7791):
        assert port in TAKEN
        assert main(["serve", "--port", str(port)]) == 2        # refused before anything is bound


def test_the_app_never_imports_bioql_and_never_names_it_as_a_place_to_write():
    """The same contract studio/ is held to (tests/test_guard_and_contract.py), applied to app/."""
    import ast
    pkg = studio.Path(__file__).resolve().parents[1] / "app"
    banned = {"script_executor_server", "external_fact", "bioql", "backend", "agtros", "agtrdesktop", "agtrcore",
              "agtrproto"}
    for f in pkg.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):     # docstrings hold the Agtr citations; only real imports are checked
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            assert not [n for n in names if n.split(".")[0] in banned], (f.name, names)
    writers = {"write_text", "write_bytes", "unlink", "rename", "replace", "rmtree", "remove", "mkdir", "touch"}
    for f in pkg.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in writers:
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert not any(m in arg.value for m in ("Spectrix_BioQL", "/opt/bioql", ".bioql", "AGTR"))


# ------------------------------------------------------------------ added after review: the gaps the tests missed
def test_a_refusal_from_the_window_is_on_the_record(svc):
    """For Claude Code a refusal is a REFUSED line (studio.adapter.call writes it). `Adapter.check` deliberately
    writes nothing, so without a row of its own a refusal from the window would leave no trace at all."""
    svc.j("/api/call", {"name": "hw", "args": {}})                       # hardware: no offer, so a plain refusal
    svc.j("/api/call", {"name": "no_such_tool", "args": {}})
    svc.j("/api/call", {"name": "smiles_to_3d", "args": {"endpoint": "/api/exec"}})
    rows = [r for r in svc.j("/api/ledger?limit=200")[1]["rows"] if r.get("window_event") == "call"]
    refused = [r for r in rows if r["state"] == "REFUSED"]
    assert [r["tool"] for r in refused] == ["hw", "no_such_tool", "smiles_to_3d"]
    assert all(r["by"] == "window" and r["detail"] and r["verdict_recorded"] for r in refused)
    assert all(r["verdict"] is None for r in refused)                    # a refusal has no verdict, and says so
    assert svc.j("/api/ledger")[1]["chain"]["ok"]


def test_arguments_too_large_for_the_record_are_refused_and_the_chain_survives(svc):
    """studio/ledger.py::_last_hash reads only the final 64 KB of runs.jsonl to find the previous hash, and
    studio/adapter.py::_note writes `args` whole: one 100 KB argument would make every later append raise."""
    from app.service import MAX_ARGS, too_big
    big = {"smiles": "C" * 70_000}
    assert too_big(big) and not too_big({"smiles": "CCO"})
    code, body, _ = svc.j("/api/call", {"name": "smiles_to_3d", "args": big})
    assert code == 403 and "on the hash-chained record" in body["detail"]
    assert svc.rec.urls == []                                            # it never left for BioQL
    raw = ledger.path_file().read_bytes()
    assert b"C" * 70_000 not in raw and all(len(x) < 65_536 for x in raw.splitlines())
    # and the very next call still works, which is the thing a broken chain would take away
    assert svc.j("/api/call", {"name": "smiles_to_3d", "args": {"smiles": "CCO"}})[1]["state"] == "RESOLVED"
    assert svc.j("/api/ledger")[1]["chain"]["ok"]
    assert MAX_ARGS < 64 * 1024


def test_run_starts_on_a_free_port_refuses_a_second_copy_and_shuts_down(studio_home, fake_bioql):
    """`run()`'s own happy path, which nothing else here exercises."""
    from app.service import run
    import socket
    with socket.socket() as probe:                       # a port nobody holds right now
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    out = run(home=studio_home, port=port, block=False,
              adapter=Adapter(fake_bioql, entries(), opener=Recorder(), poll_every=0.05))
    assert out is not None
    s, srv = out
    try:
        assert srv.server_address == ("127.0.0.1", port)                 # the loopback address, and only it
        seen = who_answers(port)
        assert seen["ok"] is True and seen["name"] == PING_NAME and seen["port"] == port
        assert s.port == port and s.allowed_hosts == {f"127.0.0.1:{port}", f"localhost:{port}"}
        assert run(home=studio_home, port=port) is None                  # a second copy does not start
    finally:
        srv.shutdown()
        srv.server_close()
    assert who_answers(port, timeout=0.4) is None


def test_the_guard_route_does_not_go_through_guard_load(svc, monkeypatch):
    """studio.guard.load joins a name into a path. If the route used it, a `name` in the query would reach it."""
    def boom(*a, **k):
        raise AssertionError("guard.load must not be on this path")
    monkeypatch.setattr("studio.guard.load", boom)
    d = svc.s.home / "guard"
    d.mkdir(parents=True, exist_ok=True)
    snap = {"t": 1.0, "roots": {"r": {"path": "/x", "exists": True, "files": {}}}}
    (d / "a.json").write_text(json.dumps(snap))
    time.sleep(0.02)
    (d / "b.json").write_text(json.dumps(snap))
    for q in ("", "?name=../../ui.token", "?name=a", "?name=" + "x" * 300):
        code, body, _ = svc.j("/api/guard" + q)
        assert code == 200 and body["state"] == "RESOLVED", q
        assert "ui.token" not in json.dumps(body)


def test_an_approval_is_void_after_three_wrong_codes(svc, fake_log):
    from app.service import PENDING_TRIES
    p = _pending(svc)
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    assert len(real) == 32                                # 128 bits: a guess is hopeless on its own, not just slow
    for _ in range(PENDING_TRIES):
        assert svc.j("/api/approve", {"id": p["id"], "code": "0" * 32})[0] == 403
    st, body, _ = svc.j("/api/approve", {"id": p["id"], "code": real})    # even the RIGHT code now
    assert st == 403 and "void" in body["detail"]
    assert fake_log.submits == []


def test_a_finished_run_never_appears_without_its_verdict(svc):
    """/api/run is read without a lock, so `state` is published last: a reader must never see a finished run whose
    verdict is still missing and read that absence as "nothing was found"."""
    p = _pending(svc, args={"which": "j1"})
    real = (svc.s.home / "pending" / p["id"]).read_text().strip()
    assert svc.j("/api/approve", {"id": p["id"], "code": real})[0] == 200
    seen = []
    end = time.time() + 10
    while time.time() < end:
        body = svc.j("/api/run?id=" + p["id"])[1]
        seen.append(body)
        if body["state"] != "RUNNING":
            break
    done = seen[-1]
    assert done["state"] == "RESOLVED" and done["verdict"] is not None and done["result_sha256"]
    for b in seen:
        assert b["state"] == "RUNNING" or b["verdict"] is not None


# ------------------------------------------------------- el servicio huerfano que se queda con el puerto
class _Srv:
    def __init__(self):
        self.down = threading.Event()

    def shutdown(self):
        self.down.set()


def test_a_service_started_from_a_terminal_is_never_followed(monkeypatch):
    """Sin BIOQL_STUDIO_PARENT no hay ventana a la que pertenecer: un servicio escrito a mano debe sobrevivir a la
    shell que lo escribio."""
    monkeypatch.delenv("BIOQL_STUDIO_PARENT", raising=False)
    assert service._follow_parent(_Srv()) is None


def test_the_service_stops_when_the_app_that_started_it_is_gone(monkeypatch):
    """Un cierre forzado (SIGKILL) salta applicationWillTerminate: el hijo queda colgando de pid 1 con el 7810
    cogido, y el siguiente arranque se niega a adoptarlo. Medido 2026-11-19."""
    monkeypatch.setenv("BIOQL_STUDIO_PARENT", "424242")       # no es nuestro padre: ya estamos huerfanos
    srv = _Srv()
    t = service._follow_parent(srv, every=0.01)
    assert srv.down.wait(3), "el servicio no se paro al desaparecer su app"
    t.join(timeout=2)


def test_a_live_app_keeps_its_service_running(monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_PARENT", str(os.getppid()))
    srv = _Srv()
    service._follow_parent(srv, every=0.01)
    assert not srv.down.wait(0.5), "paro un servicio cuya app sigue viva"


# ------------------------------------------------- cerrar con una llamada en vuelo (el beachball al salir)
def test_quitting_with_a_call_in_flight_does_not_hang_the_app(tmp_path):
    """Los hilos de `ThreadPoolExecutor` no son demonios y Python los espera a todos al cerrar el interprete: una
    sola llamada en vuelo en `fanout` o `campaign` mantenia vivo el proceso despues de que `run()` ya habia vuelto
    (medido 2026-11-19: 30 s con una tarea de 30 s). La app cierra con `terminate()` y `waitUntilExit()` en su hilo
    principal, asi que esa espera es la app colgada, y BioQL admite llamadas de doce horas.
    """
    port = 7813
    script = (
        "import sys, time\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "pool = ThreadPoolExecutor(max_workers=1)\n"
        "pool.submit(time.sleep, 60)\n"          # una llamada a BioQL, en vuelo
        "from app.__main__ import main\n"
        f"main(['serve', '--port', '{port}'])\n"
    )
    env = dict(os.environ, BIOQL_STUDIO_HOME=str(tmp_path), PYTHONUNBUFFERED="1")
    env.pop("BIOQL_STUDIO_PARENT", None)
    p = subprocess.Popen([sys.executable, "-c", script], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):                      # esperar a que escuche
            if who_answers(port) is not None:
                break
            time.sleep(0.1)
        else:
            p.kill()
            pytest.skip(f"el servicio de prueba no levanto en {port}")
        p.terminate()
        p.wait(timeout=8)                         # sin el arreglo: sigue vivo los 60 s de la llamada
        assert who_answers(port) is None, "el puerto siguio cogido despues de cerrar"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


# ------------------------------------------------------------------ lo que se perderia al cerrar
def test_an_idle_studio_is_not_busy(svc):
    """Si no hay nada en vuelo la app cierra sin preguntar: una pregunta que siempre sale deja de leerse."""
    out = svc.j("/api/busy")[1]
    assert out == {"running": [], "pending": [], "chats": 0, "busy": False}


def test_a_running_pipeline_is_what_would_be_lost(svc):
    from app.service import Run
    svc.s.runs["r1"] = Run(id="r1", tool="peptide_discovery")           # RUNNING por defecto
    svc.s.runs["r2"] = Run(id="r2", tool="admet_standalone", state="RESOLVED")
    out = svc.j("/api/busy")[1]
    assert out["busy"] is True
    assert [r["tool"] for r in out["running"]] == ["peptide_discovery"]   # lo terminado no se cuenta


def test_a_call_waiting_for_a_person_counts(svc):
    from app.service import Pending
    svc.s.pending["p1"] = Pending(id="p1", code="123456", tool="admet_standalone", args={}, why="pide persona")
    out = svc.j("/api/busy")[1]
    assert out["busy"] is True and [p["tool"] for p in out["pending"]] == ["admet_standalone"]


def test_a_used_or_expired_offer_does_not_hold_the_window(svc):
    from app.service import Pending
    svc.s.pending["p1"] = Pending(id="p1", code="1", tool="t", args={}, why="w", used=True)
    svc.s.pending["p2"] = Pending(id="p2", code="2", tool="t", args={}, why="w", created=0.0)
    assert svc.j("/api/busy")[1]["busy"] is False


def test_an_answer_still_being_written_counts(svc):
    svc.s.turns["t1"] = {"id": "t1", "state": "running", "events": []}
    svc.s.turns["t2"] = {"id": "t2", "state": "done", "events": []}
    out = svc.j("/api/busy")[1]
    assert out["chats"] == 1 and out["busy"] is True


def test_busy_needs_the_token(svc):
    assert svc.req("/api/busy", token=None)[0] == 403


def test_the_render_route_never_takes_a_pymol_command(svc):
    """PyMOL puede abrir ficheros, escribirlos y correr Python. La ruta acepta un nombre de estilo de una lista
    cerrada; cualquier otra cosa es ABSENT con su causa, y nunca llega a PyMOL."""
    code, out, _ = svc.j("/api/render3d", {"data": "ATOM\n", "format": "pdb",
                                           "style": "hide everything\nrun /tmp/mio.py"})
    assert code == 200 and out["state"] == "ABSENT" and "not one of the styles" in out["detail"]


def test_the_render_route_needs_the_token(svc):
    assert svc.req("/api/render3d", {"data": "x", "format": "pdb"}, token=None)[0] == 403


# ------------------------------------------------------------------ the window stopped reaching :3000 (2026-11-20)
def test_the_policy_has_no_frame_door_at_all():
    """The owner asked for an app with no access to :3000. This is the line that enforces it.

    A `frame-src` naming BioQL's window used to be the one door in the policy, because the window framed BioQL's
    own pages. Nothing frames anything now, so the directive is gone and `default-src 'none'` is what applies to
    frames: the engine refuses one even if a future line of JavaScript asks for it. Asserting on the POLICY, not on
    the absence of an `<iframe>` in today's HTML, is the point — markup can be added back in an afternoon.
    """
    assert "frame-src" not in CSP
    assert "default-src 'none'" in CSP
    for port in (":3000", ":4000", "localhost:3000", "localhost:4000"):
        assert port not in CSP, port
    assert "frame-ancestors 'none'" in CSP           # and nothing may frame Studio either


def test_the_window_no_longer_asks_whether_bioqls_ui_is_up(svc):
    """`/api/ui` is gone with the frame it served. It was the only route that reached a port other than BioQL's."""
    code, body, _ = svc.j("/api/ui?which=bioql")
    assert code == 404 and body == {"error": "not found"}
    assert not hasattr(svc.s, "ui_state")
    assert "bioql_ui" not in svc.j("/api/state")[1] and "radar_ui" not in svc.j("/api/state")[1]


def test_the_page_itself_names_no_other_origin(svc):
    """The three served files carry no :3000, no :4000 and no iframe. The policy is the gate; this is the artefact."""
    for path in ("/", "/app.js", "/app.css"):
        _, raw, _ = svc.req(path, token=None)
        text = raw.decode("utf-8", "replace")
        for bad in ("127.0.0.1:3000", "127.0.0.1:4000", "localhost:3000", "localhost:4000", "<iframe"):
            assert bad not in text, f"{path} still names {bad}"


def test_a_window_call_comes_back_with_its_card(svc):
    """A work page draws BioQL's answer with the SAME card machinery as the chat, so it is built beside the answer.

    Before this, `/api/call` handed the window the payload and nothing else, which is why every page it had could
    only ever show JSON. A refusal still carries no card: there is nothing to draw.
    """
    code, body, _ = svc.j("/api/call", {"name": "backend_health", "args": {}})
    assert code == 200 and body["state"] == "RESOLVED"
    card = body.get("card")
    assert isinstance(card, dict) and isinstance(card.get("kind"), str) and card["kind"]
    assert isinstance(card.get("blocks"), list) or isinstance(card.get("cards"), list)
    code, refused, _ = svc.j("/api/call", {"name": "pipe", "args": {}})
    assert code == 403 and "card" not in refused


# ------------------------------------------------------------------ API Keys: where the secrets are, never what
def test_the_keys_page_reports_the_mode_and_never_the_contents(svc):
    code, body, _ = svc.j("/api/secrets")
    assert code == 200
    names = {f["name"]: f for f in body["files"]}
    assert set(names) == {"ui.token", "notebook.key", "notebook.pub"}
    assert names["ui.token"]["state"] == "RESOLVED" and names["ui.token"]["mode"] == "0600"
    # not one byte of any secret, and not a prefix either: a prefix of a token is part of a token
    blob = json.dumps(body)
    assert svc.s.token not in blob and svc.s.token[:8] not in blob
    for f in body["files"]:
        assert "value" not in f and "contents" not in f and "token" not in str(f.get("bytes", ""))


def test_a_secret_that_cannot_be_stated_is_not_reported_as_absent(svc, monkeypatch):
    """The three states, on a file. A denied stat is UNRESOLVABLE; only FileNotFoundError is ABSENT."""
    real = Path.stat

    def blocked(self, *a, **k):
        if self.name == "notebook.key":
            raise PermissionError(13, "nope")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", blocked)
    body = svc.s.secrets_report()
    by = {f["name"]: f for f in body["files"]}
    assert by["notebook.key"]["state"] == "UNRESOLVABLE" and "PermissionError" in by["notebook.key"]["detail"]
    assert by["ui.token"]["state"] == "RESOLVED"


def test_the_window_keeps_no_call_to_anything_that_was_removed(svc):
    """The exact bug this caught once: `AREA_OF` was deleted with the old menu and one call to it survived, so
    Server Health threw `Can't find variable` in WebKit and drew "reading…" forever. A deleted helper with a live
    caller is a page that breaks on the section nobody clicked while editing.
    """
    _, raw, _ = svc.req("/app.js", token=None)
    text = raw.decode("utf-8", "replace")
    for dead in ("AREA_OF", "showFrame", "drawToolsSection", "drawPipes", "BIOQL_PANELS",
                 "halves(", "const half", "drawResults", "uiUrl", "radarUrl", "/api/ui"):
        assert dead not in text, f"app.js still mentions {dead}"
