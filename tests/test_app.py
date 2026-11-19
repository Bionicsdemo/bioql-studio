"""The app shell's own service, proved on an ephemeral port against a FAKE BioQL.

Nothing here reaches 127.0.0.1:5035, 7810, 7790 or any other real port: the adapter is built with the `fake_bioql`
fixture's base URL and an opener that RECORDS every URL it is asked for, so a test can assert that no request ever
left the allowlist. Both directions are proved: the positives, and — mostly — the negatives.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import studio
from studio import allowlist as al
from studio import ledger
from studio.adapter import Adapter

from app import PING_NAME, PORT
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
    """No request string becomes a filesystem path: a request names one of three keys or it is a 404."""
    for path in ("/../app/service.py", "/..%2f..%2fapp/service.py", "/%2e%2e/service.py", "/web/app.js",
                 "/app.js/", "/ui.token", "/runs.jsonl", "/../../ui.token", "/favicon.ico", "/nope.html"):
        code, raw, _ = svc.req(path, token=None)
        assert code == 404, (path, code)
        assert b"def " not in raw and b"import " not in raw
    for path in ("/", "/index.html", "/app.js", "/app.css"):
        code, raw, _ = svc.req(path, token=None)
        assert code == 200 and raw, path
    assert set(STATIC) == {"/", "/index.html", "/app.js", "/app.css"}


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
def test_the_ledger_records_the_verdict_and_the_findings(svc):
    """studio.adapter._note writes provenance but not the verdict or the findings, although the project's README says
    it does. The window's own row carries them."""
    ents = entries(twin={"method": "POST", "path": "/api/mixed", "kind": "local", "timeout": 30,
                         "why": "provenance that contradicts itself"})
    s2 = Svc(svc.s.home, svc.s.adapter.base, ents)
    try:
        code, body, _ = s2.j("/api/call", {"name": "twin", "args": {}})
        assert code == 200 and body["verdict"] in ("concerns", "contradicted") and body["findings"]
        code, out, _ = s2.j("/api/ledger?limit=100")
        assert code == 200 and out["chain"]["ok"]
        mine = [r for r in out["rows"] if r.get("window_event") == "call"]
        assert mine and mine[-1]["verdict"] == body["verdict"]
        assert [f["code"] for f in mine[-1]["findings"]] == [f["code"] for f in body["findings"]][:20]
        assert mine[-1]["verdict_recorded"] is True
        # the adapter's own rows recorded no verdict, and are shown as such — never as clean
        theirs = [r for r in out["rows"] if "window_event" not in r]
        assert theirs and all(r["verdict_recorded"] is False and r["verdict"] is None for r in theirs)
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
