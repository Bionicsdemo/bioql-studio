"""The no-change guard, and the source-level contract: Studio never imports BioQL and never names its folders as a
place to write."""
import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from studio import guard
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
    bioql_marks = ("Spectrix_BioQL", "/opt/bioql", ".bioql")
    hits = []
    for f in PKG.rglob("*.py"):
        if f.name == "guard.py":                             # the guard names them to READ them; checked below
            continue
        for node in ast.walk(_code(f)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(m in node.value for m in bioql_marks):
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
    assert names == set(ents) and all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    out = handle(a, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "q", "arguments": {}}})["result"]
    assert out["isError"] and json.loads(out["content"][0]["text"])["state"] == "REFUSED"
    ok = handle(a, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "smiles_to_3d", "arguments": {"smiles": "CCO"}}})["result"]
    assert not ok["isError"] and json.loads(ok["content"][0]["text"])["state"] == "RESOLVED"
    assert handle(a, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
