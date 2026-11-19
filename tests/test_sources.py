"""The external sources, proved by their negatives.

Nothing here touches the network, starts a server or runs `claude`. `Path.home` is pointed at `tmp_path` for every
test in this file, so a defaulted path cannot reach the owner's real `~/.claude.json`, and every reader takes its
files as parameters.
"""
import json
import os
import re

import pytest

from studio import home, sources
from studio.ledger import digest
from studio.resolution import Resolution, absent, resolved, unresolvable


def written(cfg):
    """The config file's text, read through the handle the way a caller would (os.fspath)."""
    with open(os.fspath(cfg), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """No test may read the real ~/.claude.json, not even by forgetting a parameter."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(sources.Path, "home", staticmethod(lambda: h))
    return h


def claude_json(path, user=None, projects=None):
    path.write_text(json.dumps({"mcpServers": user or {}, "projects": projects or {}}))
    return path


PUBMED = {"command": "npx", "args": ["-y", "pubmed-mcp"], "env": {"NCBI_API_KEY": "sk-secret-pubmed"}}
CHEMBL = {"url": "https://example.invalid/chembl", "headers": {"Authorization": "Bearer header-secret"}}
RAYMOL = {"command": "raymol-server", "args": ["--stdio"]}


@pytest.fixture
def owner(tmp_path, fake_home):
    """One owner's real-shaped config: two servers in the user scope, one in a project's own .mcp.json."""
    root = tmp_path / "proj"
    root.mkdir()
    cj = claude_json(fake_home / ".claude.json", user={"pubmed": PUBMED, "raymol": RAYMOL},
                     projects={str(root): {"mcpServers": {"chembl": CHEMBL}}})
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"opentargets": {"command": "ot-mcp"}}}))
    return {"claude_json": cj, "project_root": root}


# ------------------------------------------------------------------ discovery reports, and never enables
def test_discovery_finds_each_scope_and_keeps_them_apart(owner):
    d = sources.discover(**owner)
    assert sorted(d.servers) == ["chembl", "opentargets", "pubmed", "raymol"]
    assert d.servers["pubmed"]["user"].transport == "stdio"
    assert d.servers["chembl"]["project"].transport == "http"
    assert d.servers["opentargets"]["repo"].origin.endswith(".mcp.json")
    assert d.unreadable() == []


def test_nothing_is_enabled_by_default(owner):
    assert sources.load() == {}
    out = sources.listing(**owner)
    assert out["enabled"] == []
    assert [r["name"] for r in out["discovered"] if r["enabled"]] == []
    # discovery alone writes nothing
    assert not sources.path_file().exists()
    with sources.config_for(**owner) as cfg:
        assert json.loads(written(cfg)) == {"mcpServers": {}}


def test_a_bad_name_is_refused_everywhere(owner):
    for bad in ["pub med", "pubmed\n", "", "x" * 65, "a/b", None, "../etc/passwd"]:
        with pytest.raises(sources.Refused):
            sources.check_name(bad)
    with pytest.raises(sources.Refused):
        sources.enable("pub med", **owner)


def test_a_trailing_newline_does_not_pass_as_a_name():
    """`re.match` with `^…$` accepts "pubmed\\n"; `fullmatch` is why this is refused."""
    assert re.match(r"^[A-Za-z0-9_.-]{1,64}$", "pubmed\n")       # the shape that would have passed
    with pytest.raises(sources.Refused):
        sources.check_name("pubmed\n")


def test_studio_own_server_name_cannot_be_taken(tmp_path, fake_home):
    cj = claude_json(fake_home / ".claude.json", user={"bioql": {"command": "impostor"}})
    with pytest.raises(sources.Refused, match="Studio's own server name"):
        sources.enable("bioql", claude_json=cj)


def test_a_server_not_in_the_owner_config_cannot_be_enabled(owner):
    with pytest.raises(sources.Refused, match="not in your own MCP config"):
        sources.enable("alphafold", **owner)
    with pytest.raises(sources.Refused, match="Studio ships no definition"):
        sources.enable("clinicaltrials", **owner)
    assert sources.load() == {}


def test_the_six_designed_names_ship_no_definitions():
    """Naming a source in the design is not shipping a way to reach it."""
    for n, why in sources.DESIGNED.items():
        assert isinstance(why, str) and why
        assert "command" not in why and "http" not in why
    assert set(sources.DESIGNED) == {"pubmed", "chembl", "opentargets", "clinicaltrials", "alphafold", "raymol"}


def test_a_definition_without_command_or_url_is_refused(tmp_path, fake_home):
    cj = claude_json(fake_home / ".claude.json", user={"empty": {"note": "nothing to start"}})
    with pytest.raises(sources.Refused, match="neither a command nor a url"):
        sources.enable("empty", claude_json=cj)


# ------------------------------------------------------------------ the three states of a file
def test_a_missing_claude_json_is_absent_not_a_failure(tmp_path, fake_home):
    d = sources.discover(claude_json=tmp_path / "nope.json")
    assert d.reads["user"].absent and d.unreadable() == []
    with pytest.raises(sources.Refused, match="not in your own MCP config"):
        sources.enable("pubmed", claude_json=tmp_path / "nope.json")


@pytest.mark.parametrize("body", ['{"mcpServers": ', "[1, 2, 3]", "null", "not json at all"])
def test_an_unparsable_claude_json_is_unresolvable_never_empty(tmp_path, fake_home, body):
    """Agtr folds a corrupt file into `{}` (dev.py:1544-1547), so it reads as "you have no servers"."""
    f = tmp_path / ".claude.json"
    f.write_text(body)
    d = sources.discover(claude_json=f)
    assert d.reads["user"].unresolvable
    with pytest.raises(sources.Refused) as why:
        sources.enable("pubmed", claude_json=f)
    assert "could not read" in str(why.value) and "cannot tell whether" in str(why.value)
    assert "not in your own MCP config" not in str(why.value)


def test_an_unreadable_file_says_nothing_about_its_content(tmp_path, fake_home):
    f = tmp_path / ".claude.json"
    f.write_text('{"mcpServers": {"pubmed": {"command": "x", "env": {"K": "leak-me"}}}} trailing')
    r = sources.discover(claude_json=f).reads["user"]
    assert r.unresolvable and "leak-me" not in r.detail and "trailing" not in r.detail


def test_one_unreadable_file_does_not_hide_the_other(tmp_path, fake_home):
    root = tmp_path / "p"
    root.mkdir()
    (root / ".mcp.json").write_text("{{{")
    cj = claude_json(fake_home / ".claude.json", user={"raymol": RAYMOL})
    d = sources.discover(claude_json=cj, project_root=root)
    assert d.reads["repo"].unresolvable and "raymol" in d.servers
    assert sources.enable("raymol", claude_json=cj, project_root=root)["scope"] == "user"


# ------------------------------------------------------------------ scopes are not merged
def test_a_corrupt_file_does_not_read_as_a_removed_server(tmp_path, fake_home):
    """Naming the scope does not make the file readable. Saying "not in your user MCP config" about a file Studio
    could not parse is the third state degrading into the second."""
    f = fake_home / ".claude.json"
    claude_json(f, user={"raymol": RAYMOL})
    sources.enable("raymol", claude_json=f)
    f.write_text("{ not json")
    for call in (lambda: sources.config_for(claude_json=f),
                 lambda: sources.definitions(["raymol"], claude_json=f),
                 lambda: sources.enable("raymol", scope="user", claude_json=f)):
        with pytest.raises(sources.Refused) as why:
            call()
        assert "could not read" in str(why.value) and "not in your user MCP config" not in str(why.value)


@pytest.mark.parametrize("bad", ["[]", '"x"', "7"])
def test_an_mcpservers_of_the_wrong_type_is_unresolvable(tmp_path, fake_home, bad):
    """A MISSING key is ABSENT: the owner has no servers there. A key holding something Studio cannot read as
    servers is UNRESOLVABLE — there IS something there."""
    f = fake_home / ".claude.json"
    f.write_text('{"mcpServers": %s}' % bad)
    assert sources.discover(claude_json=f).reads["user"].unresolvable
    f.write_text('{"other": 1}')
    assert sources.discover(claude_json=f).reads["user"].absent


def test_the_same_name_in_two_scopes_is_not_silently_replaced(tmp_path, fake_home):
    """Agtr's `dict.update` chain (dev.py:1548-1551) lets a repo's .mcp.json — a file an agent can write — shadow a
    user server the owner approved."""
    root = tmp_path / "p"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"pubmed": {"command": "evil"}}}))
    cj = claude_json(fake_home / ".claude.json", user={"pubmed": PUBMED})
    d = sources.discover(claude_json=cj, project_root=root)
    assert sorted(d.servers["pubmed"]) == ["repo", "user"]
    with pytest.raises(sources.Refused, match="say which scope"):
        sources.enable("pubmed", claude_json=cj, project_root=root)
    sources.enable("pubmed", scope="user", claude_json=cj, project_root=root)
    got = sources.definitions(claude_json=cj, project_root=root)
    assert got["pubmed"]["command"] == "npx"            # the approved one, not the repo's


def test_a_changed_definition_is_a_different_server(tmp_path, fake_home):
    f = fake_home / ".claude.json"
    claude_json(f, user={"raymol": RAYMOL})
    sources.enable("raymol", claude_json=f)
    claude_json(f, user={"raymol": {"command": "raymol-server", "args": ["--stdio", "--but-also=rm -rf"]}})
    with pytest.raises(sources.Refused, match="not the server that was approved"):
        sources.definitions(claude_json=f)
    assert sources.listing(claude_json=f)["discovered"][0]["changed"] is True


def test_a_server_that_vanished_cannot_be_used(tmp_path, fake_home):
    f = fake_home / ".claude.json"
    claude_json(f, user={"raymol": RAYMOL})
    sources.enable("raymol", claude_json=f)
    claude_json(f, user={})
    with pytest.raises(sources.Refused, match="not in your user MCP config"):
        sources.config_for(claude_json=f)


def test_enable_and_disable_round_trip(owner):
    sources.enable("pubmed", **owner)
    assert sources.listing(**owner)["enabled"] == ["pubmed"]
    assert sources.disable("pubmed") is True
    assert sources.disable("pubmed") is False
    assert sources.listing(**owner)["enabled"] == []


def test_the_choice_file_is_private(owner):
    sources.enable("pubmed", **owner)
    assert oct(sources.path_file().stat().st_mode & 0o777) == "0o600"


def test_a_choice_is_per_project(owner):
    sources.enable("pubmed", project="alpha", **owner)
    assert sources.listing(project="alpha", **owner)["enabled"] == ["pubmed"]
    assert sources.listing(project="beta", **owner)["enabled"] == []
    with pytest.raises(sources.Refused, match="is not enabled"):
        sources.definitions(["pubmed"], project="beta", **owner)


# ------------------------------------------------------------------ the per-turn config file
def test_the_config_holds_only_what_was_enabled_and_is_0600(owner, monkeypatch):
    sources.enable("pubmed", **owner)
    sources.enable("opentargets", **owner)
    old = os.umask(0)                                    # the mode must not depend on the runner's umask
    try:
        cfg = sources.config_for(**owner)
    finally:
        os.umask(old)
    body = json.loads(written(cfg))
    assert sorted(body["mcpServers"]) == ["opentargets", "pubmed"]
    assert "raymol" not in body["mcpServers"] and "chembl" not in body["mcpServers"]
    assert oct(cfg.path.stat().st_mode & 0o777) == "0o600"
    cfg.close()
    assert not cfg.path.exists()


def test_the_config_is_written_even_when_nothing_is_enabled(owner):
    """`--strict-mcp-config` with an EMPTY map is what stops a turn inheriting the owner's servers; no file at all
    inherits them all (Agtr's own note, engines.py:35)."""
    cfg = sources.config_for(**owner)
    assert cfg.path.exists() and json.loads(written(cfg)) == {"mcpServers": {}}
    assert sources.mcp_args(cfg) == ["--mcp-config", str(cfg.path), "--strict-mcp-config", "--allowedTools", ""]
    cfg.close()


def test_the_config_is_gone_even_when_the_turn_raises(owner):
    sources.enable("raymol", **owner)
    cfg = None
    with pytest.raises(RuntimeError):
        with sources.config_for(**owner) as c:
            cfg = c
            assert c.path.exists()
            raise RuntimeError("the turn failed")
    assert cfg is not None and not cfg.path.exists()


def test_the_config_file_is_never_opened_then_chmoded(owner, monkeypatch):
    """0600 from the first byte: the only `open` is `os.open` with O_CREAT|O_EXCL and the mode, and no chmod runs.

    Agtr leaks mkstemp's fd, reopens by path and chmods after writing (engines.py:95-97).
    """
    seen = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, fl, *a: seen.append((str(p), fl, a)) or real_open(p, fl, *a))
    monkeypatch.setattr(os, "chmod", lambda *a, **k: pytest.fail("the config was chmod'ed after being written"))
    sources.enable("raymol", **owner)
    cfg = sources.config_for(**owner)
    mine = [s for s in seen if s[0] == str(cfg.path)]
    assert len(mine) == 1
    flags, args = mine[0][1], mine[0][2]
    assert flags & os.O_EXCL and flags & os.O_CREAT and not flags & os.O_TRUNC
    assert args == (0o600,)
    cfg.close()


def test_a_second_turn_gets_its_own_file(owner):
    sources.enable("raymol", **owner)
    a, b = sources.config_for(**owner), sources.config_for(**owner)
    assert a.path != b.path and a.path.parent == home()
    a.close(), b.close()


# ------------------------------------------------------------------ no secret on the command line
def test_no_env_or_header_value_reaches_argv(owner):
    """`ps` shows argv to every process on this machine. Agtr passes Codex's MCP env and url as arguments —
    `-c mcp_servers.X.env={…}` (agtros/mind/providers.py:478-481)."""
    sources.enable("pubmed", **owner)                                  # env: NCBI_API_KEY
    sources.enable("chembl", scope="project", **owner)                 # headers: Authorization, and a url
    cfg = sources.config_for(**owner)
    argv = ["claude", "-p", "--output-format", "stream-json", *sources.mcp_args(cfg)]
    flat = " ".join(argv)
    for secret in ["sk-secret-pubmed", "header-secret", "Bearer header-secret",
                   "https://example.invalid/chembl", "NCBI_API_KEY", "Authorization", "npx", "pubmed-mcp"]:
        assert secret not in flat, f"{secret!r} is on the command line"
    assert sorted(json.loads(written(cfg))["mcpServers"]) == ["chembl", "pubmed"]   # it IS in the 0600 file
    assert "sk-secret-pubmed" in written(cfg)
    cfg.close()


def test_listing_reports_credential_key_names_never_values(owner):
    rows = {r["name"]: r for r in sources.listing(**owner)["discovered"]}
    assert rows["pubmed"]["secret_keys"] == {"env": ["NCBI_API_KEY"]}
    assert rows["chembl"]["secret_keys"] == {"headers": ["Authorization"]}
    assert rows["raymol"]["secret_keys"] == {}
    flat = json.dumps(sources.listing(**owner), default=str)
    for secret in ["sk-secret-pubmed", "header-secret", "example.invalid"]:
        assert secret not in flat


# ------------------------------------------------------------------ the tool prefix
def test_a_dotted_name_is_normalised_for_the_tool_prefix():
    """Agtr builds `mcp__{name}` verbatim (engines.py:99), so `mcp__open.targets` can never match the
    `mcp__open_targets__…` tools Claude actually offers: the allow entry covers nothing."""
    assert sources.prefix("open.targets") == "mcp__open_targets__"
    assert sources.allowed_tools(["open.targets"]) == ["mcp__open_targets__*"]
    assert sources.allowed_tools(["claude-in-chrome"]) == ["mcp__claude-in-chrome__*"]   # the hyphen is kept
    assert sources.allowed_tools([]) == []


def test_two_names_that_collide_under_one_prefix_are_refused(tmp_path, fake_home):
    with pytest.raises(sources.Refused, match="could not tell their tools apart"):
        sources.allowed_tools(["open.targets", "open_targets"])
    cj = claude_json(fake_home / ".claude.json",
                     user={"open.targets": {"command": "a"}, "open_targets": {"command": "b"}})
    sources.enable("open.targets", claude_json=cj)
    sources.enable("open_targets", claude_json=cj)
    with pytest.raises(sources.Refused, match="could not tell their tools apart"):
        sources.config_for(claude_json=cj)


def test_allowed_tools_go_with_the_config(owner):
    sources.enable("pubmed", **owner)
    sources.enable("raymol", **owner)
    cfg = sources.config_for(**owner)
    args = sources.mcp_args(cfg)
    allow = args[args.index("--allowedTools") + 1].split(",")
    assert allow == ["mcp__pubmed__*", "mcp__raymol__*"]       # only what this config holds
    assert "mcp__chembl__*" not in allow and "mcp__bioql__*" not in allow
    cfg.close()


# ------------------------------------------------------------------ a citation is a record
def test_a_citation_records_what_was_asked_and_what_came_back():
    ref = {"doi": "10.1038/s41586-020-2649-2", "pmid": "32939066", "title": "Array programming with NumPy",
           "year": 2020}
    c = sources.cite("pubmed", "NumPy is the array library the pipeline uses", ref, asked="pmid:32939066")
    assert c["state"] == "RESOLVED" and c["source"] == "pubmed" and c["asked"] == "pmid:32939066"
    assert c["sha256"] == digest(ref)
    assert c["provenance"]["doi"]["state"] == "RESOLVED"
    assert c["provenance"]["doi"]["value"] == "10.1038/s41586-020-2649-2"
    assert isinstance(c["t"], float)


def test_an_absent_doi_is_absent_not_unknown():
    c = sources.cite("chembl", "CHEMBL25 is aspirin", {"chembl_id": "CHEMBL25", "doi": "", "url": None},
                     asked="CHEMBL25")
    for key in ("doi", "url", "pmid"):
        assert c["provenance"][key]["state"] == "ABSENT"
        assert "value" not in c["provenance"][key]
    flat = json.dumps(c)
    assert "unknown" not in flat.lower() and "null" not in flat and '""' not in flat
    assert all("value" not in v for v in c["provenance"].values() if v["state"] == "ABSENT")


def test_an_empty_string_does_not_pass_as_a_resolved_field():
    """`resolved("")` passes resolution.py's own check, so the empty string is routed to ABSENT here."""
    assert resolved("", "x").resolved                     # the type allows it
    assert sources.cite("pubmed", "c", {"doi": "   "}, asked="q")["provenance"]["doi"]["state"] == "ABSENT"


def test_a_failed_lookup_is_unresolvable_and_carries_no_digest():
    """A timeout is not "the source has nothing". It is the third state, and it does not degrade into the second."""
    c = sources.cite("pubmed", "aspirin inhibits COX-1", unresolvable("pubmed", "timeout after 30s"),
                     asked="aspirin COX-1")
    assert c["state"] == "UNRESOLVABLE" and "sha256" not in c and "provenance" not in c
    assert "timeout" in c["detail"]


def test_a_source_that_answered_with_nothing_is_absent():
    c = sources.cite("clinicaltrials", "no trial for this compound", absent("ctgov", "0 studies"), asked="X-999")
    assert c["state"] == "ABSENT" and c["sha256"] and all(
        v["state"] == "ABSENT" for v in c["provenance"].values())


def test_no_key_of_a_citation_is_written_empty():
    """An empty string in a record reads as an answer. The key is left out instead — `asked` when no question was
    given, `detail` when the lookup carried none."""
    c = sources.cite("pubmed", "a claim", {"doi": "10.1/x"})
    assert "asked" not in c and "detail" not in c
    assert "" not in [v for v in c.values() if isinstance(v, str)]
    assert '""' not in json.dumps(c)
    u = sources.cite("pubmed", "a claim", unresolvable("pubmed", "timeout"))
    assert "asked" not in u and '""' not in json.dumps(u)


def test_a_text_answer_is_digested_as_it_arrived_and_invents_no_provenance():
    """An MCP tool answers with text (studio/brain.py joins a tool result into a string). Nothing was parsed out of
    it, so there is no provenance to report — not even as ABSENT, which would claim a field was looked for."""
    c = sources.cite("pubmed", "a claim", "PMID 32939066. No DOI in this body.", asked="pmid:32939066")
    assert c["state"] == "RESOLVED" and c["answer"] == "text" and "provenance" not in c
    assert c["sha256"] == digest(b"PMID 32939066. No DOI in this body.")
    assert sources.cite("pubmed", "a claim", "   ", asked="q")["state"] == "ABSENT"


def test_a_citation_refuses_what_is_not_an_answer():
    with pytest.raises(ValueError, match="the claim"):
        sources.cite("pubmed", "   ", {"asked": "q"})
    with pytest.raises(sources.Refused):
        sources.cite("not a name", "a claim", {"asked": "q"})
    with pytest.raises(TypeError):
        sources.cite("pubmed", "a claim", ["not", "an", "answer"], asked="q")


def test_the_question_can_come_from_the_answer_itself():
    c = sources.cite("alphafold", "P0DTC2 is modelled", {"accession": "P0DTC2", "query": "P0DTC2"})
    assert c["asked"] == "P0DTC2"


def test_cite_writes_nothing():
    """A citation is a value. Whether it goes on the signed ledger is the caller's decision, not this module's."""
    before = sorted(p.name for p in home().iterdir())
    sources.cite("pubmed", "c", {"doi": "10.1/x"}, asked="q")
    assert sorted(p.name for p in home().iterdir()) == before


def test_a_resolution_is_not_truthy_by_accident():
    """`if res:` cannot fold three states into two — the guard the whole module leans on."""
    with pytest.raises(TypeError):
        bool(unresolvable("x", "y"))
    assert isinstance(absent("x"), Resolution)


# ------------------------------------------------------------------ through the argv Studio really builds
def test_no_secret_reaches_the_argv_studio_actually_builds(owner, monkeypatch):
    """The hand-rolled argv above is this module's shape; this one is the real builder (studio/brain.py, another
    agent's file, hence the skip). `binary()` returns CLAUDE_BIN without running it, so nothing is started."""
    brain = pytest.importorskip("studio.brain")
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    sources.enable("pubmed", **owner)
    sources.enable("chembl", scope="project", **owner)
    cfg = sources.config_for(**owner)
    argv = brain.command(None, cfg, allowed=[brain.ALLOWED, *sources.allowed_tools(cfg.names)])
    flat = " ".join(argv)
    for secret in ["sk-secret-pubmed", "header-secret", "NCBI_API_KEY", "Authorization",
                   "https://example.invalid/chembl", "npx"]:
        assert secret not in flat, f"{secret!r} is on the command line"
    assert "mcp__pubmed__*" in flat and "mcp__chembl__*" in flat and "mcp__bioql__*" in flat
    assert str(cfg.path) in argv
    cfg.close()
