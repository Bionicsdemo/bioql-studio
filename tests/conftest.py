import json
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
        if p == "/api/err":
            return self._send(200, {"error": "no pocket found"})
        if p == "/api/submit":
            return self._send(202, {"job_id": body.get("which", "j2"), "status": "queued", "poll_url": "/evil/route"})
        if p == "/api/mixed":
            return self._send(200, {"a": {"is_real_hardware": True}, "b": [{"is_real_hardware": False}]})
        if p == "/api/plain":
            return self._send(200, {"value": 1, "note": "is_real_hardware mentioned in prose only"})
        return self._send(404, {"error": "not found"})


@pytest.fixture
def fake_bioql():
    FakeBioQL.polls = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBioQL)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
