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


def test_what_this_widens_is_the_allowlist_and_nothing_more():
    """Registrar BioQL en otro cliente no puede ampliar lo que se puede llamar.

    Antes este test afirmaba que la puerta de aprobacion viajaba con el servidor. La puerta ya no existe -- la
    razon esta escrita en `Adapter.check` -- asi que lo que hay que afirmar es lo que SI limita: solo se puede
    llamar lo que esta en el allowlist, y la ruta la pone el allowlist, nunca el que llama. Eso vale igual desde
    esta ventana que desde cualquier Claude en el que se registre.
    """
    from studio import allowlist as al
    from studio.adapter import Adapter, Refused
    ents = {k: al._norm(k, v) for k, v in al.DEFAULTS.items()}
    ents["pipe"] = al._norm("pipe", {"method": "POST", "path": "/api/quantum/x", "kind": "compute", "why": "costs"})
    a = Adapter("http://127.0.0.1:1", ents, log=False)
    assert a.check("pipe", {})                               # lo que esta en la lista, se llama
    with pytest.raises(Refused):
        a.check("no_esta_en_la_lista", {})                    # lo que no esta, no
    with pytest.raises(Refused):
        a.check("pipe", {"endpoint": "/api/ssh/execute"})      # y la ruta nunca la elige el que llama


def test_a_write_by_the_client_itself_is_not_thrown_away(clients, monkeypatch):
    """`~/.claude.json` is a LIVE file: Claude Code writes to it while it runs, so read-modify-write over it is
    last-writer-wins.

    This was not theoretical. Between the backup copy taken by a registration and the check made seconds later,
    Claude Code had already updated its own `pluginUsage` counter. Today that was a counter; the same file holds
    each project's allowed tools.
    """
    p = clients["code"]
    other = {"wrote": False}
    real_copy = cn.shutil.copy2

    def meddle(src, dst):
        # stands in for the other process: it writes between this edit's read and its replace
        out = real_copy(src, dst)
        if not other["wrote"]:
            other["wrote"] = True
            d = json.loads(Path(src).read_text())
            d["pluginUsage"] = {"someplugin": {"usageCount": 7}}
            Path(src).write_text(json.dumps(d, indent=2))
        return out

    monkeypatch.setattr(cn.shutil, "copy2", meddle)
    res = cn.apply()
    assert res.resolved
    after = json.loads(p.read_text())
    assert after["pluginUsage"] == {"someplugin": {"usageCount": 7}}, "the other process's write was clobbered"
    assert "bioql" in after["mcpServers"]                       # and ours landed too
    assert after["anonymousId"] == "keep-me"


def test_a_file_that_will_not_hold_still_is_reported_rather_than_forced(clients, monkeypatch):
    p = clients["code"]

    # The churn has to happen on EVERY attempt, inside the window between this edit's read and its verification —
    # patching the one-time backup only proved the retry works (it did, and that was the earlier version of this
    # test passing for the wrong reason). `json.dump` runs once per attempt, in exactly that window.
    real_dump = cn.json.dump

    def dump_and_churn(obj, fh, **kw):
        out = real_dump(obj, fh, **kw)
        d = json.loads(p.read_text())
        d["churn"] = os.urandom(4).hex()
        p.write_text(json.dumps(d))
        return out

    monkeypatch.setattr(cn.json, "dump", dump_and_churn)
    res = cn.apply()
    # RESOLVED carries the detail as the value (Claude Desktop did change); ABSENT and UNRESOLVABLE carry it as
    # meta, because `.value` raises on those by design.
    data = res.value if res.resolved else res.meta
    assert [f["client"] for f in data["failed"]] == ["Claude Code"]
    assert "kept changing" in data["failed"][0]["detail"]
    assert "bioql" not in json.loads(p.read_text()).get("mcpServers", {})


def test_the_installed_app_is_preferred_over_a_checkout(monkeypatch, tmp_path):
    """A registration pointing at somebody's working copy dies the day they move the folder and never picks up an
    update. The bundle's copy is what the update channel replaces, so that is what gets registered."""
    fake = tmp_path / "BundleResources"
    (fake / "studio").mkdir(parents=True)
    (fake / "studio" / "__init__.py").write_text("")
    monkeypatch.setattr(cn, "INSTALLED", fake)
    assert cn.package_root() == fake
    monkeypatch.setattr(cn, "INSTALLED", tmp_path / "nothing-here")
    assert (cn.package_root() / "studio" / "__init__.py").is_file()      # falls back to the checkout
