import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def studio_home(tmp_path, monkeypatch):
    h = tmp_path / "studio-home"
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(h))
    return h


class FakeBioQL(BaseHTTPRequestHandler):
    """A stand-in for BioQL's backend: only the shapes the adapter must handle."""
    polls: dict = {}
    submits: list = []
    # A test sets this to a real path so the service can compare the process start against its mtime.
    manifest_file: str = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path
        if p.startswith("/api/echo"):
            # Hands back the query string exactly as it arrived, so a test can see what the adapter SENT rather
            # than what it meant to send. A GET's arguments used to be built and dropped, and no test noticed.
            from urllib.parse import parse_qsl, urlsplit
            return self._send(200, {"query": dict(parse_qsl(urlsplit(p).query, keep_blank_values=True)),
                                    "raw": urlsplit(p).query, "path": urlsplit(p).path})
        if p == "/health":
            return self._send(200, {"status": "healthy"})
        if p == "/api/tools/registry":
            return self._send(200, {"ok": True, "tools": [
                {"name": "admet_standalone", "kind": "smiles", "endpoint": None, "schema": {"required": ["smiles"]}},
                {"name": "peptide_design", "kind": "http", "endpoint": "/api/quantum/peptide_design",
                 "schema": {"required": ["target"], "optional": ["n"], "cost_tier": "high", "confirmation_required": True, "version": 1}},
                {"name": "qpu_thing", "kind": "http", "endpoint": "/api/quantum/qpu_thing",
                 "schema": {"required": [], "cost_tier": "qpu"}}]})
        if p == "/api/pipeline/job/j1":
            return self._send(200, {"job_id": "j1", "status": "completed",
                                    "result": {"is_real_hardware": False, "quantum_backend": "aer_simulator"}})
        if p == "/api/pipeline/job/j2":
            n = FakeBioQL.polls.get("j2", 0) + 1
            FakeBioQL.polls["j2"] = n
            if n < 2:
                return self._send(200, {"job_id": "j2", "status": "running"})
            return self._send(200, {"job_id": "j2", "status": "completed",
                                    "result": {"is_real_hardware": True, "ibm_job_id": "d0abc123"}})
        if p == "/api/pipeline/job/j3":
            return self._send(200, {"job_id": "j3", "status": "failed", "error": "boom"})
        if p == "/api/pipeline/job/j4":
            return self._send(200, {"job_id": "j4", "status": "partially_degraded"})
        if p == "/api/pipeline/job/j5":          # finished, but the science is on its own route (as BioQL does)
            return self._send(200, {"job_id": "j5", "status": "completed", "result_available": True,
                                    "result_size": 95988, "result_url": "/evil/route", "result": None})
        if p == "/api/pipeline/job/j5/result":
            return self._send(200, {"admet": {"logp": 1.2}, "is_real_hardware": False})
        if p == "/api/pipeline/job/j6":          # finished and says nothing is there
            return self._send(200, {"job_id": "j6", "status": "completed", "result_available": False})
        if p == "/api/pipeline/job/j7":          # a receipt with no job id to follow is not a result
            return self._send(200, {"status": "accepted"})
        if p == "/api/system/manifest":
            # BioQL's real shape, including the defect the manifest page exists to not repeat: the SAME sentence
            # ("editar la copia del repo no cambia nada en esta ruta") is stamped on a diverged module whether the
            # live copy is the repo one or the shadow one. `repo_wins` below has origin "promoted", so for that row
            # BioQL's own executed_from contradicts its own sentence.
            back = "editar la copia del repo no cambia nada en esta ruta"
            return self._send(200, {
                "available": True, "pid": os.getpid(), "python": "3.12.7",
                "promoted_dir": "/repo/src/backend", "shadow_dir": "/opt/bioql",
                "n_loaded": 4, "n_from_promoted": 3, "n_from_shadow": 1, "n_diverged": 2,
                "matches_promoted": False, "digest": "d832e1920a686227",
                "diverged": [
                    {"module": "repo_wins", "executed_from": FakeBioQL.manifest_file or "/repo/src/backend/a.py",
                     "origin": "promoted", "sha256": "aaaa", "twin": "/opt/bioql/a.py", "twin_sha256": "bbbb",
                     "diverged": True, "warning": f"se ejecuta /repo/src/backend/a.py mientras /opt/bioql/a.py "
                                                  f"tiene OTRO contenido: {back}"},
                    {"module": "shadow_wins", "executed_from": "/opt/bioql/b.py", "origin": "shadow",
                     "sha256": "cccc", "twin": "/repo/src/backend/b.py", "twin_sha256": "dddd",
                     "diverged": True, "warning": f"se ejecuta /opt/bioql/b.py mientras /repo/src/backend/b.py "
                                                  f"tiene OTRO contenido: {back}"}],
                "shadow_only": [
                    {"module": "orphan", "executed_from": "/opt/bioql/_c.py", "origin": "shadow",
                     "sha256": None, "twin": None, "twin_sha256": None, "diverged": False,
                     "warning": "se ejecuta desde la copia sombra y NO existe gemelo"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = self.path
        if p == "/api/smiles-to-3d":
            return self._send(200, {"mol": f"SDF for {body.get('smiles')}"})
        if p == "/api/slow":
            time.sleep(2)
            return self._send(200, {"late": True})
        if p == "/api/boom":
            return self._send(500, {"error": "kaboom"})
        if p == "/api/notjson":
            return self._send(200, b"<html>hi</html>", "text/html")
        if p == "/api/bom":                      # what a streaming keepalive can leave in the body
            return self._send(200, b"\xef\xbb\xbf{\"ok\": 1}\x00\n")
        if p == "/api/err":
            return self._send(200, {"error": "no pocket found"})
        if p == "/api/submit":
            return self._send(202, {"job_id": body.get("which", "j2"), "status": "queued", "poll_url": "/evil/route"})
        if p == "/api/pipeline/submit":          # BioQL's own async door: {endpoint, params} -> a RECEIPT
            FakeBioQL.submits.append(body)
            which = (body.get("params") or {}).get("which", "j1")
            if which == "j7":
                return self._send(202, {"status": "accepted", "message": "submitted"})
            return self._send(202, {"job_id": which, "status": "accepted",      # BioQL's real word
                                    "poll_url": "/evil/route", "message": "Pipeline submitted. Poll for results."})
        if p == "/api/mixed":
            return self._send(200, {"a": {"is_real_hardware": True}, "b": [{"is_real_hardware": False}]})
        if p == "/api/plain":
            return self._send(200, {"value": 1, "note": "is_real_hardware mentioned in prose only"})
        return self._send(404, {"error": "not found"})


@pytest.fixture
def fake_log():
    """The running fake's own class, so a test reads the same object the server writes (importing tests.conftest
    would build a second module and a second class)."""
    return FakeBioQL


@pytest.fixture
def fake_bioql():
    FakeBioQL.polls, FakeBioQL.submits, FakeBioQL.manifest_file = {}, [], ""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBioQL)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
