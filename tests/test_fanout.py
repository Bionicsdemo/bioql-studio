"""Several calls at once: that they really overlap, that the gate is still per call, and that one failing does not
take the others with it.

The point of the fan-out is wall-clock: four independent questions should cost one wait, not four. A test that only
checked the answers would pass on a serial loop — which is exactly what this replaces — so the first test here
measures the clock.
"""
import json
import sys
import threading
import time

import pytest

from studio import allowlist as al
from studio import fanout, ledger, mcp_server
from studio import ledger
from studio.adapter import Adapter, Refused


def entries(**extra):
    base = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    for k, v in extra.items():
        base[k] = al._norm(k, v)
    return base


def ad(url, log=False, **extra):
    return Adapter(url, entries(**extra), poll_every=0.05, log=log)


SLOW = {"method": "POST", "path": "/api/slow", "kind": "local", "approval": False, "why": "two seconds"}


# ------------------------------------------------------------------ it actually overlaps
def test_four_slow_calls_cost_one_wait_not_four(fake_bioql):
    """`/api/slow` sleeps 2 s in the fake backend. Four of them serially is ~8 s; together it must be ~2 s."""
    a = ad(fake_bioql, slow=SLOW)
    began = time.time()
    out = fanout.run_many(a, [{"name": "slow", "args": {}} for _ in range(4)], workers=4)
    took = time.time() - began
    assert [r["state"] for r in out["runs"]] == ["RESOLVED"] * 4
    assert took < 5, f"four 2-second calls took {took:.1f}s: they did not overlap"


def test_the_order_given_is_the_order_returned(fake_bioql):
    """They finish in whatever order they finish; a reader comparing the fourth answer with the fourth question
    must not have to check."""
    a = ad(fake_bioql, slow=SLOW)
    calls = [{"name": "smiles_to_3d", "args": {"smiles": s}} for s in ("CCO", "CCC", "CCCC", "CCCCC")]
    out = fanout.run_many(a, calls, workers=4)
    assert [r["args"]["smiles"] for r in out["runs"]] == ["CCO", "CCC", "CCCC", "CCCCC"]
    assert [r["i"] for r in out["runs"]] == [0, 1, 2, 3]


# ------------------------------------------------------------------ the gate is per call, not per batch
def test_a_batch_records_every_call_including_the_expensive_one(fake_bioql):
    """Antes esto comprobaba que un `compute` escondido entre llamadas baratas no pasaba la puerta. La puerta ya no
    esta -- Studio ejecuta, como :3000 -- asi que lo que hay que comprobar es lo que si quedo: que un lote no es
    una forma de que algo corra SIN QUEDAR ESCRITO. Tres llamadas, tres lineas, todas con el mismo grupo."""
    a = ad(fake_bioql, log=True,
           heavy={"method": "POST", "path": "/api/pipeline/submit", "kind": "compute", "via": "submit"},
           slow=SLOW)
    out = fanout.run_many(a, [{"name": "backend_health", "args": {}},
                              {"name": "heavy", "args": {}},
                              {"name": "backend_health", "args": {}}])
    assert [r["state"] for r in out["runs"]] == ["RESOLVED", "RESOLVED", "RESOLVED"]
    assert out["counts"] == {"RESOLVED": 3}
    rows = [json.loads(l) for l in ledger.path_file().read_text().splitlines() if l.strip()]
    mias = [r for r in rows if r.get("group") == out["group"] and r.get("tool")]
    assert len([r for r in mias if r["tool"] in ("backend_health", "heavy")]) == 3
    caro = [r for r in mias if r["tool"] == "heavy"]
    assert caro and caro[0]["kind"] == "compute", "la cara no quedo marcada como compute en el registro"


def test_an_unknown_tool_is_refused_without_touching_the_others(fake_bioql):
    a = ad(fake_bioql)
    out = fanout.run_many(a, [{"name": "backend_health", "args": {}}, {"name": "no_such_tool", "args": {}}])
    assert out["runs"][0]["state"] == "RESOLVED"
    assert out["runs"][1]["state"] == "REFUSED" and "allowlist" in out["runs"][1]["detail"]


def test_one_call_that_cannot_be_asked_is_its_own_unresolvable(fake_bioql):
    """A backend that answers 500 for one route is that ROUTE's UNRESOLVABLE, never the batch's."""
    a = ad(fake_bioql, boom={"method": "POST", "path": "/api/boom", "kind": "local", "approval": False,
                             "why": "always 500"})
    out = fanout.run_many(a, [{"name": "boom", "args": {}}, {"name": "backend_health", "args": {}}])
    assert out["runs"][0]["state"] == "UNRESOLVABLE"
    assert out["runs"][1]["state"] == "RESOLVED"


def test_args_that_are_not_an_object_are_refused_not_coerced(fake_bioql):
    a = ad(fake_bioql)
    out = fanout.run_many(a, [{"name": "backend_health", "args": ["smiles", "CCO"]}])
    assert out["runs"][0]["state"] == "REFUSED"


# ------------------------------------------------------------------ the brakes on the batch itself
def test_an_empty_batch_is_an_error_not_an_empty_success(fake_bioql):
    a = ad(fake_bioql)
    with pytest.raises(Refused):
        fanout.run_many(a, [])
    with pytest.raises(Refused):
        fanout.run_many(a, "backend_health")


def test_more_than_the_cap_is_refused_with_what_to_do_instead(fake_bioql):
    a = ad(fake_bioql)
    with pytest.raises(Refused) as why:
        fanout.run_many(a, [{"name": "backend_health"}] * (fanout.MAX_CALLS + 1))
    assert "batches" in str(why.value)


@pytest.mark.parametrize("asked,want", [(1, 1), (4, 4), (0, 1), (-3, 1), (99, fanout.MAX_WORKERS)])
def test_the_worker_count_is_bounded(asked, want):
    assert fanout.workers_for(asked) == want


# ------------------------------------------------------------------ the record
def test_every_call_keeps_its_line_and_they_share_a_group(fake_bioql, tmp_path, monkeypatch):
    f = tmp_path / "runs.jsonl"
    monkeypatch.setattr(ledger, "path_file", lambda: f)
    a = Adapter(fake_bioql, entries(slow=SLOW), poll_every=0.05, log=True)
    out = fanout.run_many(a, [{"name": "backend_health", "args": {}},
                              {"name": "smiles_to_3d", "args": {"smiles": "CCO"}},
                              {"name": "no_such_tool", "args": {}}])
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    calls = [r for r in rows if r.get("group") == out["group"] and r["tool"] != "run_many"]
    assert sorted(r["tool"] for r in calls) == ["backend_health", "no_such_tool", "smiles_to_3d"]
    assert {r["state"] for r in calls} == {"RESOLVED", "REFUSED"}
    # and a summary line that COUNTS those lines, rather than standing in for them
    summary = [r for r in rows if r["tool"] == "run_many"][0]
    assert summary["group"] == out["group"] and summary["counts"] == out["counts"]
    assert summary["state"] == "GROUP"
    assert ledger.verify(f)["ok"] is True


def test_a_call_that_runs_alone_still_records_no_group(fake_bioql, tmp_path, monkeypatch):
    f = tmp_path / "runs.jsonl"
    monkeypatch.setattr(ledger, "path_file", lambda: f)
    a = Adapter(fake_bioql, entries(), poll_every=0.05, log=True)
    a.call("backend_health", {})
    row = json.loads(f.read_text().splitlines()[0])
    assert row["group"] is None


# ------------------------------------------------------------------ through the MCP server, as Claude sees it
def test_claude_is_offered_run_many_with_the_other_tools(fake_bioql):
    names = [t["name"] for t in mcp_server.tool_list(ad(fake_bioql))]
    assert "run_many" in names and "backend_health" in names
    one = [t for t in mcp_server.tool_list(ad(fake_bioql)) if t["name"] == "run_many"][0]
    assert one["inputSchema"]["required"] == ["calls"]


def test_the_server_runs_a_batch_and_reports_each_state(fake_bioql):
    """Un lote mezcla lo que corre y lo que no, y cada uno lleva su propio estado.

    Lo que falla aqui ya no es una aprobacion -- la puerta se quito -- sino un nombre que no existe, que es lo
    unico que sigue siendo motivo para negarse.
    """
    a = ad(fake_bioql, heavy={"method": "POST", "path": "/api/pipeline/submit", "kind": "compute", "via": "submit"})
    out = mcp_server.handle(a, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                "params": {"name": "run_many", "arguments": {"calls": [
                                    {"name": "backend_health", "args": {}},
                                    {"name": "no_existe", "args": {}}]}}})
    body = json.loads(out["result"]["content"][0]["text"])
    assert [r["state"] for r in body["runs"]] == ["RESOLVED", "REFUSED"]
    # una negativa dentro de un lote no es el lote fallando: cuatro respuestas de cinco no se esconden
    assert out["result"]["isError"] is False


def test_a_batch_accepts_the_names_the_client_actually_shows_the_model(fake_bioql):
    """EL fallo que hacia inusable `run_many` desde Claude Desktop y Claude Code.

    Un cliente MCP ensena estas herramientas bajo SU propio espacio de nombres: `backend_health` aparece como
    `mcp__bioql__backend_health`. En una llamada normal el protocolo quita ese prefijo antes de llegar aqui, pero
    `run_many` recibe los nombres como ARGUMENTOS, asi que el modelo escribe el unico nombre que se le enseno --
    con prefijo -- y todo el lote volvia "not in Studio's allowlist". Parecia una herramienta que falta o un
    backend caido. Era Studio negando sus propias herramientas con el nombre que sus propios clientes muestran.
    """
    a = ad(fake_bioql, log=True)
    out = mcp_server.handle(a, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                "params": {"name": "run_many", "arguments": {"calls": [
                                    {"name": "mcp__bioql__backend_health", "args": {}},
                                    {"name": "backend_health", "args": {}}]}}})
    body = json.loads(out["result"]["content"][0]["text"])
    assert [r["state"] for r in body["runs"]] == ["RESOLVED", "RESOLVED"], body["runs"]
    # y el prefijo del cliente no crea una segunda herramienta en el registro
    rows = [json.loads(l) for l in ledger.path_file().read_text().splitlines() if l.strip()]
    nombres = {r["tool"] for r in rows if "backend_health" in str(r.get("tool"))}
    assert nombres == {"backend_health"}, f"el registro guardo el nombre del cliente: {nombres}"


def test_an_unknown_name_is_still_refused_prefix_or_not(fake_bioql):
    """Quitar el prefijo nunca puede convertirse en adivinar otra herramienta."""
    a = ad(fake_bioql)
    for nombre in ("mcp__bioql__no_existe", "mcp__otro__backend_health_x", "no_existe"):
        with pytest.raises(Refused):
            a.call(nombre)


def test_a_batch_where_nothing_resolved_is_an_error(fake_bioql):
    out = mcp_server.handle(ad(fake_bioql), {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                             "params": {"name": "run_many", "arguments": {"calls": [
                                                 {"name": "no_such_tool", "args": {}}]}}})
    assert out["result"]["isError"] is True


class _Pipe:
    """stdin for the loop under test: lines appear when the test writes them, and the loop blocks in between, as a
    real pipe does."""

    def __init__(self):
        self.lines, self.ready, self.closed = [], threading.Event(), False

    def put(self, obj):
        self.lines.append(json.dumps(obj) + "\n")
        self.ready.set()

    def close(self):
        self.closed = True
        self.ready.set()

    def __iter__(self):
        while True:
            if not self.lines:
                if self.closed:
                    return
                self.ready.wait(5)
                self.ready.clear()
                continue
            yield self.lines.pop(0)


class _Out:
    def __init__(self):
        self.lines, self.lock, self.got = [], threading.Lock(), threading.Event()

    def write(self, s):
        with self.lock:
            if s.strip():
                self.lines.append(s)
                self.got.set()

    def flush(self):
        pass

    def wait_for(self, mid, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for line in self.lines:
                    if json.loads(line).get("id") == mid:
                        return json.loads(line)
            self.got.wait(0.1)
            self.got.clear()
        raise AssertionError(f"no answer for id {mid} within {timeout}s; got {len(self.lines)} line(s)")


def test_the_loop_answers_one_call_while_another_is_still_running(fake_bioql, monkeypatch):
    """The loop itself, not just the handler: it used to read a line, do the work and only then read the next, so a
    submitted pipeline held the pipe for as long as it ran — with a 12-hour ceiling, Claude could not even ask
    whether the backend was alive while one was going."""
    a = ad(fake_bioql, slow=SLOW)
    monkeypatch.setattr(mcp_server, "Adapter", lambda *x, **k: a)
    pipe, out = _Pipe(), _Out()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(sys, "stdout", out)
    loop = threading.Thread(target=mcp_server.main, daemon=True)
    loop.start()

    pipe.put({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "slow", "arguments": {}}})          # two seconds in the fake backend
    time.sleep(0.2)
    began = time.time()
    pipe.put({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "backend_health", "arguments": {}}})
    quick = out.wait_for(2)
    waited = time.time() - began
    assert waited < 1.5, f"the quick read waited {waited:.1f}s behind the slow call"
    assert json.loads(quick["result"]["content"][0]["text"])["value"]["status"] == "healthy"
    slow = out.wait_for(1)                                            # and the slow one still arrives, whole
    assert json.loads(slow["result"]["content"][0]["text"])["state"] == "RESOLVED"
    pipe.close()
    loop.join(timeout=10)
