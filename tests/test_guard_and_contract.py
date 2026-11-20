"""The no-change guard, and the source-level contract: Studio never imports BioQL and never names its folders as a
place to write."""
import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from studio import guard
from studio import guard as sguard
from studio.mcp_server import handle
from studio.adapter import Adapter
from studio import allowlist as al

PKG = Path(__file__).resolve().parents[1] / "studio"


def tree(tmp_path):
    root = tmp_path / "bioql"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "src" / "b.json").write_text("{}")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("1")
    return [{"name": "t", "path": str(root)}], root


def test_guard_clean_and_touch_is_not_a_change(tmp_path):
    roots, root = tree(tmp_path)
    b = guard.snapshot(roots)
    os.utime(root / "src" / "a.py", (1, 1))                 # same bytes, new mtime: content decides
    (root / "node_modules" / "dep.js").write_text("2")      # runtime folder: not watched
    assert guard.compare(b, guard.snapshot(roots))["clean"]


def test_guard_sees_add_change_remove(tmp_path):
    roots, root = tree(tmp_path)
    with pytest.raises(guard.Changed) as e:
        with guard.session(roots):
            (root / "src" / "a.py").write_text("x = 2\n")
            (root / "src" / "b.json").unlink()
            (root / "src" / "c.py").write_text("")
    r = e.value.report["roots"]["t"]
    assert r["changed"] == ["src/a.py"] and r["removed"] == ["src/b.json"] and r["added"] == ["src/c.py"]


def test_guard_reads_git_without_touching_the_index(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    run("init", "-q"); (root / "f.py").write_text("1\n"); run("add", "f.py")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    idx = root / ".git" / "index"
    os.utime(root / "f.py", None)                            # a stale stat that a plain `git status` would rewrite
    before = idx.stat().st_mtime_ns, idx.read_bytes()
    snap = guard.snapshot([{"name": "r", "path": str(root), "git": True}])
    assert snap["roots"]["r"]["git"]["head"]
    assert (idx.stat().st_mtime_ns, idx.read_bytes()) == before


def _code(p: Path) -> ast.Module:
    """The module with its docstrings removed: a guard must not read its own explanation."""
    tree = ast.parse(p.read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body[0] = ast.Pass()
    return tree


def test_studio_never_imports_bioql():
    banned = {"script_executor_server", "external_fact", "bioql", "src", "backend", "pipeline_registry", "agtros",
              "agtrdesktop"}
    found = []
    for f in PKG.rglob("*.py"):
        for node in ast.walk(_code(f)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            found += [(f.name, n) for n in names if n.split(".")[0] in banned]
    assert not found, found


def test_studio_never_writes_into_bioql():
    """No string constant in a write call points into BioQL's folders (docstrings are not code: AST only)."""
    def points_into_bioql(v: str) -> bool:
        """A PATH into BioQL, matched by component and not by substring.

        `.bioql` as a substring also matches `com.molevera.bioqlstudio` — Studio's OWN bundle identifier, which
        `studio/release.py` checks an update against. Reading that as "a write into BioQL" is the same defect the
        project's own notes record twice: match by token, not by substring (an audit rate went from 60% to 94%
        because `"kd" in path` matched `score_brea(kd)own`). A guard that fires on the wrong thing gets relaxed,
        and then it no longer guards.
        """
        if "Spectrix_BioQL" in v or "/opt/bioql" in v:
            return True
        return ".bioql" in [part for part in v.replace("\\", "/").split("/")]

    hits = []
    for f in PKG.rglob("*.py"):
        if f.name == "guard.py":                             # the guard names them to READ them; checked below
            continue
        for node in ast.walk(_code(f)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and points_into_bioql(node.value):
                hits.append((f.name, node.value))
    assert not hits, hits
    g = _code(PKG / "guard.py")
    writers = {"write_text", "write_bytes", "unlink", "rename", "replace", "rmtree", "remove", "chmod", "mkdir", "touch"}
    called = {n.func.attr for n in ast.walk(g) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & writers), called & writers         # the guard only reads what it watches


def test_mcp_exposes_only_the_allowlist_and_refuses_hardware(fake_bioql):
    ents = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    ents["q"] = al._norm("q", {"method": "POST", "path": "/api/mixed", "kind": "hardware", "why": "real QPU"})
    a = Adapter(fake_bioql, ents)
    tools = handle(a, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    # The allowlist, plus tools that are NOT routes and cannot become one:
    #   · `run_many` runs several of these at the same time, through Adapter.call;
    #   · the `studio_*` tools read Studio's own record, or re-read a result through the SAME allowlisted entry.
    # Neither can name a route the list does not hold — which is what this test is really about, so it is checked
    # rather than assumed: not one of them takes an endpoint, a path or a url.
    from studio import claude_tools
    assert names == set(ents) | {"run_many"} | claude_tools.NAMES
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    for t in tools:
        if t["name"] in claude_tools.NAMES:
            props = set(t["inputSchema"].get("properties") or {})
            assert not (props & {"endpoint", "path", "url", "route", "method", "args"}), (t["name"], props)
    # and they are marked as what they are, so a reader of the tool list can tell them from BioQL's routes
    assert all(t["description"].startswith("[") for t in tools)
    # Lo que un lote NO puede hacer: nombrar una ruta en crudo. La entrada de hardware si corre -- la puerta de
    # aprobacion se quito, la razon esta en `Adapter.check` -- pero `/api/mixed` no es una herramienta y nunca lo
    # sera: lo unico llamable es lo que el allowlist nombra.
    batch = handle(a, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "run_many", "arguments": {"calls": [
                           {"name": "q", "args": {}},                        # la entrada de hardware, en un lote
                           {"name": "/api/mixed", "args": {}}]}}})["result"]   # y la ruta en crudo, por su nombre
    runs = json.loads(batch["content"][0]["text"])["runs"]
    assert [r["state"] for r in runs] == ["RESOLVED", "REFUSED"]
    out = handle(a, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "q", "arguments": {}}})["result"]
    assert not out["isError"] and json.loads(out["content"][0]["text"])["state"] == "RESOLVED"
    ok = handle(a, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "smiles_to_3d", "arguments": {"smiles": "CCO"}}})["result"]
    assert not ok["isError"] and json.loads(ok["content"][0]["text"])["state"] == "RESOLVED"
    assert handle(a, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# ------------------------------------------------ una raiz que no se puede leer no es "no cambio nada"
def _root(p):
    return [{"name": "prueba", "path": str(p)}]


def test_a_root_that_cannot_be_listed_is_recorded_as_such(tmp_path):
    """`os.walk` se traga todo error salvo que se le de `onerror`: una raiz que macOS niega (`~/Documents` esta
    detras de un permiso TCC) se recorreria como si no tuviera ficheros."""
    base = tmp_path / "cerrada"
    base.mkdir()
    (base / "algo.py").write_text("x = 1\n")
    base.chmod(0o000)
    try:
        snap = sguard.snapshot(_root(base))
        r = snap["roots"]["prueba"]
        assert r["unreadable_total"] >= 1, "la raiz negada se leyo como vacia"
        assert r["files"] == {}
    finally:
        base.chmod(0o700)


def test_two_unreadable_snapshots_do_not_compare_clean(tmp_path):
    """El fallo entero: dos fotos de una raiz que no se puede mirar no tienen nada anadido, quitado ni cambiado, y
    el guardia diria que BioQL esta limpio porque no pudo mirar. El tercer estado degradando al segundo."""
    base = tmp_path / "cerrada"
    base.mkdir()
    (base / "algo.py").write_text("x = 1\n")
    base.chmod(0o000)
    try:
        a = sguard.snapshot(_root(base))
        b = sguard.snapshot(_root(base))
    finally:
        base.chmod(0o700)
    rep = sguard.compare(a, b)
    assert rep["clean"] is False
    assert rep["readable"] is False and rep["unreadable"]
    # y no se inventa un cambio que no hubo
    assert rep["roots"]["prueba"]["added"] == [] and rep["roots"]["prueba"]["changed"] == []


def test_the_message_does_not_call_a_refused_permission_a_change(tmp_path):
    base = tmp_path / "cerrada"
    base.mkdir()
    base.chmod(0o000)
    try:
        rep = sguard.compare(sguard.snapshot(_root(base)), sguard.snapshot(_root(base)))
    finally:
        base.chmod(0o700)
    msg = str(sguard.Changed(rep))
    assert "could not be read" in msg
    assert "BioQL changed during the session" not in msg


def test_a_readable_root_still_compares_clean(tmp_path):
    base = tmp_path / "abierta"
    base.mkdir()
    (base / "algo.py").write_text("x = 1\n")
    rep = sguard.compare(sguard.snapshot(_root(base)), sguard.snapshot(_root(base)))
    assert rep["clean"] is True and rep["readable"] is True


def test_a_denied_parent_is_not_read_as_a_deleted_root(tmp_path):
    """`os.path.isdir` contesta False ante CUALQUIER error de stat, no solo ante "no esta": una raiz cuyo padre
    niega macOS se leia igual que una raiz borrada — sin nada ilegible — y dos fotos asi comparaban LIMPIO,
    mientras una foto buena contra una negada declaraba todos los ficheros quitados, o sea "BioQL cambio"."""
    padre = tmp_path / "padre"
    raiz = padre / "raiz"
    raiz.mkdir(parents=True)
    (raiz / "a.py").write_text("x = 1\n")
    roots = [{"name": "p", "path": str(raiz)}]
    libre = sguard.snapshot(roots)
    padre.chmod(0o000)
    try:
        negada = sguard.snapshot(roots)
        assert negada["roots"]["p"]["unreadable_total"] >= 1
        assert sguard.compare(negada, sguard.snapshot(roots))["clean"] is False
        rep = sguard.compare(libre, negada)
        assert rep["clean"] is False
        # y no acusa de un borrado que no hubo
        assert rep["roots"]["p"]["removed"] == [] and rep["roots"]["p"]["compared"] is False
    finally:
        padre.chmod(0o700)


def test_a_root_that_really_is_gone_is_still_a_change(tmp_path):
    """Lo contrario tambien: una raiz que de verdad no esta es informacion, no ceguera."""
    raiz = tmp_path / "raiz"
    raiz.mkdir()
    (raiz / "a.py").write_text("x = 1\n")
    roots = [{"name": "p", "path": str(raiz)}]
    antes = sguard.snapshot(roots)
    (raiz / "a.py").unlink()
    raiz.rmdir()
    despues = sguard.snapshot(roots)
    assert despues["roots"]["p"]["unreadable_total"] == 0
    rep = sguard.compare(antes, despues)
    assert rep["clean"] is False and rep["readable"] is True
    assert rep["roots"]["p"]["removed"] == ["a.py"]
