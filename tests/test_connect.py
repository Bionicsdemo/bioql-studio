"""Plugging BioQL into the Claude clients on this Mac — and the limits on what that is allowed to touch.

Nothing here writes to a real config: every test points `CLIENTS` at files under tmp_path. The point of the tests
is the blast radius, because the file being edited is somebody's entire Claude setup.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from studio import connect as cn


@pytest.fixture
def clients(tmp_path, monkeypatch):
    code = tmp_path / ".claude.json"
    desk = tmp_path / "desktop.json"
    code.write_text(json.dumps({
        "mcpServers": {"codegraph": {"command": "x"}, "raymol": {"command": "y"}},
        "anonymousId": "keep-me", "projects": {"/some/where": {"allowedTools": ["a", "b"]}},
    }, indent=2))
    desk.write_text(json.dumps({"mcpServers": {"other": {"command": "z"}}}, indent=2))
    monkeypatch.setattr(cn, "CLIENTS", (
        {"name": "Claude Code", "path": code},
        {"name": "Claude Desktop", "path": desk},
        {"name": "Claude Science", "path": tmp_path / "not-installed.json"},
    ))
    return {"code": code, "desk": desk}


def test_it_adds_one_key_and_leaves_everything_else_byte_for_byte(clients):
    before = json.loads(clients["code"].read_text())
    res = cn.apply()
    assert res.resolved and len(res.value["changed"]) == 2
    after = json.loads(clients["code"].read_text())
    # every other server, and every other setting, is exactly as it was
    assert after["anonymousId"] == before["anonymousId"]
    assert after["projects"] == before["projects"]
    assert after["mcpServers"]["codegraph"] == before["mcpServers"]["codegraph"]
    assert after["mcpServers"]["raymol"] == before["mcpServers"]["raymol"]
    assert set(after["mcpServers"]) == set(before["mcpServers"]) | {"bioql"}
    assert after["mcpServers"]["bioql"]["args"] == ["-m", "studio", "mcp"]


def test_the_file_is_copied_aside_before_it_is_touched(clients):
    cn.apply()
    backups = list(clients["code"].parent.glob(".claude.json.bioql-backup-*"))
    assert backups, "an edit to somebody's whole Claude setup must be undoable"
    assert "bioql" not in json.loads(backups[0].read_text()).get("mcpServers", {})


def test_removing_puts_it_back_exactly(clients):
    before = clients["code"].read_text()
    cn.apply()
    assert "bioql" in json.loads(clients["code"].read_text())["mcpServers"]
    out = cn.apply(remove=True)
    assert out.resolved
    assert json.loads(clients["code"].read_text())["mcpServers"] == json.loads(before)["mcpServers"]


def test_a_client_that_is_not_installed_is_skipped_and_never_created(clients, tmp_path):
    cn.apply()
    assert not (tmp_path / "not-installed.json").exists()
    res = cn.apply()                                   # a second run changes nothing
    # ABSENT, and `.value` RAISES on ABSENT by design — that invariant is the whole point of Resolution, so what
    # a caller reads here is the meta, exactly as the command line does through `as_dict()`.
    assert res.absent
    with pytest.raises(LookupError):
        _ = res.value
    assert all("already registered" in s["detail"] or "not on this Mac" in s["detail"]
               for s in res.meta["skipped"])


def test_a_config_that_cannot_be_parsed_is_reported_and_not_rewritten(clients):
    clients["code"].write_text("{ this is not json")
    res = cn.apply()
    assert [f["client"] for f in res.value["failed"]] == ["Claude Code"]
    assert clients["code"].read_text() == "{ this is not json"      # untouched
    assert [c["client"] for c in res.value["changed"]] == ["Claude Desktop"]


def test_status_tells_the_three_situations_apart(clients):
    rows = {r["client"]: r for r in cn.status()}
    assert rows["Claude Code"]["state"] == "NOT_REGISTERED"
    assert rows["Claude Science"]["state"] == "ABSENT"
    cn.apply()
    rows = {r["client"]: r for r in cn.status()}
    assert rows["Claude Code"]["state"] == "REGISTERED"
    # a registration left pointing at a directory that is no longer the one running is STALE, not fine
    d = json.loads(clients["code"].read_text())
    d["mcpServers"]["bioql"]["cwd"] = "/somewhere/else"
    clients["code"].write_text(json.dumps(d))
    assert {r["client"]: r for r in cn.status()}["Claude Code"]["state"] == "STALE"


def test_the_registration_carries_no_secret():
    e = cn.entry()
    blob = json.dumps(e).lower()
    for bad in ("token", "secret", "key", "password", "sk-"):
        assert bad not in blob, f"the registration mentions {bad!r}"
    assert e["args"] == ["-m", "studio", "mcp"]
    assert Path(e["cwd"]).name and (Path(e["cwd"]) / "studio").is_dir()


def test_the_gate_is_not_what_this_widens():
    """Registering BioQL elsewhere must not become a way around the approval gate. The gate lives in the adapter
    and travels with the server, so this is asserted where it is decided, not where it is registered."""
    from studio import allowlist as al
    from studio.adapter import Adapter, Refused
    ents = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    ents["pipe"] = al._norm("pipe", {"method": "POST", "path": "/api/quantum/x", "kind": "compute", "why": "costs"})
    a = Adapter("http://127.0.0.1:1", ents, log=False)
    with pytest.raises(Refused):
        a.check("pipe", {})                                  # no approval: refused, whoever is calling
    assert a.check("pipe", {}, approved_by="a person")        # and only a person changes that
