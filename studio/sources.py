"""The external scientific sources Claude Code may use: literature and reference data, under the same closed-list
discipline as BioQL itself.

The design names six — PubMed, ChEMBL, Open Targets, ClinicalTrials, AlphaFold and raymol (PyMOL). They are the
evidence behind a claim, and the reason they live under Studio rather than beside it is that a citation Claude
produces must be traceable like any other call: which server, what was asked, what came back, and when.

Three things this module will not do.

1. It enables nothing by itself. Discovery reads the servers the owner already configured (`~/.claude.json`, for
   the user and for one project, and a project's own `.mcp.json`) and REPORTS them. Turning one on is the owner's
   act, kept in `home()/sources.json` (0600). Nothing is on by default, and a name Studio did not find in the
   owner's own config cannot be enabled at all — Studio ships no definitions of its own, not even for the six names
   above, because a built-in definition would be Studio choosing an external service on the owner's behalf.
2. It never puts a secret on a command line. Agtr passes Codex's MCP definitions as config overrides —
   `-c mcp_servers.X.env={…}` and `-c mcp_servers.X.url=…` (agtros/mind/providers.py:478-481) — and `ps` shows argv
   to every process on this machine. Here the definitions, env included, go only into the per-turn config file,
   which is 0600 from its first byte; `mcp_args()` is the whole of what reaches argv, and `listing()` reports the
   KEY NAMES of `env` and `headers`, never their values.
3. It never folds "could not read" into "not there". A missing `~/.claude.json` is ABSENT: the owner has no
   servers. A `~/.claude.json` that will not parse is UNRESOLVABLE: Studio does not know what the owner has, and
   `enable` says so instead of "that is not in your config". Agtr folds both into `{}` (agtrdesktop/dev.py:1544-1547
   and 1550-1553), so a corrupt file reads as an empty one.

Why a definition's digest is stored with the owner's choice: Agtr stores the choice by name only
(agtrdesktop/dev.py:1562) and merges the three files with `dict.update` (dev.py:1548-1551), so a repo's own
`.mcp.json` silently replaces a same-named server the owner approved in his user scope — and that file lives inside
the tree an agent writes to. Here each choice remembers the scope it was approved in and the sha256 of the
definition that was approved; `config_for` refuses when either has moved.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import home, write_private
from .ledger import digest
from .resolution import Resolution, absent, resolved, unresolvable

# Claude Code's own shape for a server name. `fullmatch`, never `match` with `^…$`: the latter accepts a trailing
# newline, and "pubmed\n" would be a different key in the config file than in the tool prefix.
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# Studio's own MCP server (studio/brain.py registers it as "bioql"). An external server under that name would
# answer under the `mcp__bioql__` prefix Claude is told is BioQL's, so the name is not available to a source.
RESERVED = ("bioql",)

# Where a definition can hold a credential. Reported by key name only, never by value.
SECRET = ("env", "headers")

SCOPES = ("user", "project", "repo")

# The six the design names, as prose for the owner's list — NOT definitions. Studio does not ship a command, a URL
# or a token for any of them: if one is not in the owner's own MCP config, Studio reports it missing rather than
# inventing a way to reach it. (raymol is the PyMOL server; it is in this owner's user scope today.)
DESIGNED = {
    "pubmed": "Literature: the published record behind a biological claim.",
    "chembl": "Bioactivity and measured potency against a target.",
    "opentargets": "Target-disease association evidence.",
    "clinicaltrials": "Trials: what has been tried in people, and what happened.",
    "alphafold": "Predicted structures, with their per-residue confidence.",
    "raymol": "PyMOL: structure handling and depiction.",
}


class Refused(PermissionError):
    """Studio will not do this. Not a fact about the source: a rule of Studio."""


def prefix(name: str) -> str:
    """The `mcp__<name>__` prefix Claude Code gives this server's tools.

    Only the dot is replaced. Agtr builds `mcp__{name}` verbatim (agtrdesktop/engines.py:99), so a server named
    `open.targets` is allowed under a prefix that can never match the `mcp__open_targets__…` tools Claude actually
    offers — the allow entry silently covers nothing. The hyphen is kept, because Claude Code keeps it: this very
    session's browser tools are `mcp__claude-in-chrome__…`.
    """
    return "mcp__" + name.replace(".", "_") + "__"


def allowed_tools(names) -> list[str]:
    """The `mcp__<name>__*` entries that go with a config holding `names`.

    Two names that normalise to the same prefix (`open.targets` and `open_targets`) are refused: one entry would
    silently authorise both servers' tools, and nothing downstream could tell which one answered.
    """
    out, seen = [], {}
    for n in names:
        check_name(n)
        p = prefix(n)
        if p in seen and seen[p] != n:
            raise Refused(f"{n!r} and {seen[p]!r} both become {p}*: Claude could not tell their tools apart")
        seen[p] = n
        if p + "*" not in out:
            out.append(p + "*")
    return out


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused(f"{name!r} is not a server name ([A-Za-z0-9_.-], 1-64)")
    if name.replace(".", "_").lower() in RESERVED:
        raise Refused(f"{name!r} is Studio's own server name; an external source cannot answer under {prefix(name)}")
    return name


# ---------------------------------------------------------------- discovery: read-only, and three-state per file
@dataclass(frozen=True)
class Server:
    name: str
    scope: str
    origin: str                  # the file it was read from
    definition: dict
    digest: str

    @property
    def transport(self) -> str:
        return "stdio" if self.definition.get("command") else ("http" if self.definition.get("url") else "")

    def secret_keys(self) -> dict:
        """Which credential-bearing keys the definition has — their NAMES. A value never leaves this module."""
        return {k: sorted(str(x) for x in (self.definition.get(k) or {})) for k in SECRET
                if isinstance(self.definition.get(k), dict) and self.definition.get(k)}

    def listed(self) -> dict:
        return {"name": self.name, "scope": self.scope, "origin": self.origin, "transport": self.transport,
                "digest": self.digest, "secret_keys": self.secret_keys(),
                "why": DESIGNED.get(self.name.replace(".", "_").lower(), "")}


def _scope_read(doc: dict, key: str, origin: str, what: str) -> Resolution:
    """How one scope's `mcpServers` read. A MISSING key is ABSENT (the owner has none there); a key of the wrong
    type is UNRESOLVABLE (there is something there and Studio cannot read it as servers)."""
    if key not in doc:
        return absent(origin, f"no mcpServers {what}")
    if not isinstance(doc[key], dict):
        return unresolvable(origin, f"mcpServers {what} is a JSON {type(doc[key]).__name__}, not an object")
    return resolved(True, origin, f"mcpServers {what}")


def _servers(raw, scope: str, origin: str) -> dict[str, Server]:
    out = {}
    if not isinstance(raw, dict):            # a wrong type is reported by _scope_read, not read as servers here
        return out
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(k, str) and NAME.fullmatch(k):
            out[k] = Server(k, scope, origin, v, digest(v))
    return out


def _read_json(path: Path) -> Resolution:
    """An object from `path`. ABSENT: no such file. UNRESOLVABLE: there but unreadable or not an object.

    `FileNotFoundError` is an `OSError`, so it is caught first — that is the whole difference between "the owner has
    no servers" and "Studio could not find out". No file content goes into the detail.
    """
    src = str(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return absent(src, "no such file")
    except OSError as why:
        return unresolvable(src, f"unreadable: {type(why).__name__}")
    except UnicodeDecodeError:
        return unresolvable(src, "not UTF-8 text")
    try:
        doc = json.loads(raw)
    except ValueError:
        return unresolvable(src, "will not parse as JSON")
    if not isinstance(doc, dict):
        return unresolvable(src, f"JSON {type(doc).__name__}, not an object")
    return resolved(doc, src, "read")


@dataclass(frozen=True)
class Discovery:
    """What the owner already has. `servers`: name → scope → Server (never merged: a name can exist twice)."""
    servers: dict = field(default_factory=dict)
    reads: dict = field(default_factory=dict)        # scope → Resolution for the file that scope came from

    def unreadable(self) -> list[Resolution]:
        return [r for r in self.reads.values() if r.unresolvable]

    def find(self, name: str, scope: str | None = None) -> Server:
        """The one Server `name` means, or a refusal that says why there is no one answer."""
        check_name(name)
        found = self.servers.get(name) or {}
        if scope:
            if scope not in SCOPES:
                raise Refused(f"unknown scope {scope!r}")
            if scope in found:
                return found[scope]
            # Naming the scope does not make the file readable: a corrupt ~/.claude.json must not come back as
            # "it is not in your user config", which is a statement about the owner's config we cannot make.
            blind = self.reads.get(scope)
            if blind is not None and blind.unresolvable:
                raise Refused(f"could not read {blind.source} ({blind.detail}) — so Studio cannot tell whether "
                              f"{name!r} is still in your {scope} MCP config")
            raise Refused(f"{name!r} is not in your {scope} MCP config")
        if len(found) > 1:
            raise Refused(f"{name!r} is in {', '.join(sorted(found))}: say which scope, they are different servers")
        if not found:
            blind = self.unreadable()
            if blind:
                # The third state must not degrade into the second: this is not "you do not have it".
                raise Refused("could not read " + "; ".join(f"{r.source} ({r.detail})" for r in blind)
                              + f" — so Studio cannot tell whether {name!r} is one of yours")
            raise Refused(f"{name!r} is not in your own MCP config, and Studio ships no definition for it")
        return next(iter(found.values()))


def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def discover(claude_json: Path | None = None, project_mcp: Path | None = None,
             project_root: Path | str | None = None) -> Discovery:
    """Read-only. The owner's user scope and one project's scope from `claude_json`, and the project's own
    `.mcp.json`. Every path is a parameter so nothing in a test can reach the real files.
    """
    cj = Path(claude_json) if claude_json else claude_json_path()
    root = Path(project_root) if project_root else None
    if project_mcp is None and root is not None:
        project_mcp = root / ".mcp.json"

    reads: dict[str, Resolution] = {}
    servers: dict[str, dict[str, Server]] = {}

    def add(found: dict[str, Server]) -> None:
        for n, sv in found.items():
            servers.setdefault(n, {})[sv.scope] = sv

    top = _read_json(cj)
    if top.resolved:
        doc = top.value
        reads["user"] = _scope_read(doc, "mcpServers", str(cj), "at the top level")
        add(_servers(doc.get("mcpServers"), "user", str(cj)))
        if root is not None:
            projects = doc.get("projects")
            if "projects" in doc and not isinstance(projects, dict):
                reads["project"] = unresolvable(str(cj), f"projects is a JSON {type(projects).__name__}")
            else:
                per = (projects or {}).get(str(root))
                reads["project"] = _scope_read(per, "mcpServers", str(cj), f"for {root}") if isinstance(per, dict) \
                    else absent(str(cj), f"no entry for {root}")
                add(_servers(per.get("mcpServers") if isinstance(per, dict) else None, "project", str(cj)))
    else:
        reads["user"] = top
        if root is not None:
            reads["project"] = top                    # one unreadable file, both of its scopes unknown

    if project_mcp is not None:
        got = _read_json(Path(project_mcp))
        if got.resolved:
            reads["repo"] = _scope_read(got.value, "mcpServers", str(project_mcp), "in .mcp.json")
            add(_servers(got.value.get("mcpServers"), "repo", str(project_mcp)))
        else:
            reads["repo"] = got
    return Discovery(servers, reads)


# ---------------------------------------------------------------- the owner's choice
def path_file() -> Path:
    return home() / "sources.json"


def load(file: Path | None = None) -> dict:
    """`{project: {name: {scope, digest, origin, t}}}`. A missing file is no choices; an unreadable one is an
    error, never a silent empty list — the same rule as `allowlist.load`."""
    f = file or path_file()
    if not f.exists():
        return {}
    raw = json.loads(f.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{f}: expected an object of projects")
    out = {}
    for proj, chosen in raw.items():
        if not isinstance(chosen, dict):
            raise ValueError(f"{f}: {proj!r} is not an object")
        out[str(proj)] = {str(n): dict(c) for n, c in chosen.items() if isinstance(c, dict) and NAME.fullmatch(str(n))}
    return out


def _save(doc: dict, file: Path | None = None) -> None:
    write_private(file or path_file(), json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def enable(name: str, project: str = "", scope: str | None = None, file: Path | None = None,
           claude_json: Path | None = None, project_mcp: Path | None = None,
           project_root: Path | str | None = None) -> dict:
    """Turn one of the owner's own servers on for Studio. Refuses anything Studio did not find in the owner's config.

    `project` is the key the choice is kept under ("" is Studio itself, with no project); the design asks for both
    "per project" and a bare `enable(name)`, so the parameter is optional and defaults to the one shared list.
    """
    sv = discover(claude_json, project_mcp, project_root).find(check_name(name), scope)
    if not sv.transport:
        raise Refused(f"{name!r} has neither a command nor a url: there is nothing to start or to call")
    doc = load(file)
    doc.setdefault(str(project), {})[name] = {"scope": sv.scope, "digest": sv.digest, "origin": sv.origin,
                                              "t": round(time.time(), 3)}
    _save(doc, file)
    return sv.listed()


def disable(name: str, project: str = "", file: Path | None = None) -> bool:
    """Turn it off. True if it was on. No name check: a name that got into the file by hand must be removable."""
    doc = load(file)
    gone = (doc.get(str(project)) or {}).pop(name, None) is not None
    if gone:
        _save(doc, file)
    return gone


def listing(project: str = "", file: Path | None = None, claude_json: Path | None = None,
            project_mcp: Path | None = None, project_root: Path | str | None = None) -> dict:
    """What there is, what is on, and what Studio could not read — the three kept apart.

    `discovered` carries each definition's credential KEY NAMES and never a value.
    """
    d = discover(claude_json, project_mcp, project_root)
    on = (load(file).get(str(project)) or {})
    rows = []
    for name in sorted(d.servers):
        for scope in sorted(d.servers[name]):
            sv = d.servers[name][scope]
            chosen = on.get(name)
            rows.append({**sv.listed(),
                         "enabled": bool(chosen and chosen.get("scope") == scope),
                         "changed": bool(chosen and chosen.get("scope") == scope and chosen.get("digest") != sv.digest)})
    return {"project": str(project), "discovered": rows,
            "enabled": sorted(on), "reserved": list(RESERVED),
            "designed": DESIGNED,
            "reads": {k: r.as_dict() for k, r in d.reads.items()},
            "unreadable": [r.as_dict() for r in d.unreadable()]}


def definitions(names=None, project: str = "", file: Path | None = None, claude_json: Path | None = None,
                project_mcp: Path | None = None, project_root: Path | str | None = None) -> dict[str, dict]:
    """The definitions for `names` (default: everything on for `project`), verified against what was approved.

    Refuses a name that is not on, that has gone from the owner's config, or whose definition has changed since the
    owner approved it — a changed definition is a different server with the same name.
    """
    d = discover(claude_json, project_mcp, project_root)
    on = (load(file).get(str(project)) or {})
    want = sorted(on) if names is None else [check_name(n) for n in names]
    out = {}
    for n in want:
        chosen = on.get(n)
        if not chosen:
            raise Refused(f"{n!r} is not enabled for {project or 'Studio'}; nothing is on until the owner turns it on")
        sv = d.find(n, chosen.get("scope"))
        if sv.digest != chosen.get("digest"):
            raise Refused(f"{n!r} in {sv.scope} is not the server that was approved (its definition changed); "
                          f"enable it again after looking at it")
        out[n] = dict(sv.definition)
    return out


# ---------------------------------------------------------------- the per-turn config file
@dataclass
class TurnConfig:
    """The private MCP config of one turn. A path (`os.fspath`/`str` work on it) plus the `close()` that removes it.

    Agtr's copy leaks the fd `mkstemp` opened, reopens the file by path and chmods it AFTER writing
    (agtrdesktop/engines.py:95-97), and deletes it outside any `finally`, by substring-matching its own argv
    (agtrdesktop/dev.py:2170-2172) — so a turn that raised, or an agent killed on a timeout, leaves a file holding
    tokens behind in the shared temp directory.
    """
    path: Path
    names: list

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def close(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "TurnConfig":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def config_for(names=None, project: str = "", path: Path | None = None, file: Path | None = None,
               claude_json: Path | None = None, project_mcp: Path | None = None,
               project_root: Path | str | None = None) -> TurnConfig:
    """Write the config Claude Code is given for one turn, holding ONLY the enabled servers.

    With nothing enabled it writes an EMPTY server map and still writes the file: with `--strict-mcp-config` an
    empty config is what stops the turn inheriting the owner's servers by accident (Agtr's own note,
    agtrdesktop/engines.py:35). Omitting the file would inherit them all.

    0600 from the first byte: `os.open` with `O_CREAT|O_EXCL`, so the file is created by this call and never exists
    world-readable for an instant. Not `write_private`, which uses `O_TRUNC` and a predictable `.tmp` neighbour.
    """
    servers = definitions(names, project, file, claude_json, project_mcp, project_root)
    allowed_tools(servers)                      # refuses a pair that would collide under one mcp__…__ prefix
    p = Path(path) if path else home() / f"mcp.turn-{secrets.token_hex(8)}.json"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps({"mcpServers": servers}, indent=1, ensure_ascii=False) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return TurnConfig(p, sorted(servers))


def mcp_args(cfg: TurnConfig | Path | str, names=None, allowed_also=()) -> list[str]:
    """The whole of what the sources put on Claude Code's command line: a path and tool prefixes.

    Only this config's own servers are allowed from here; Studio's own `mcp__bioql__*` is the caller's to add,
    since it is the caller that puts the `bioql` server in the same file.

    No definition, no url, no header and no env value is ever here. `ps` shows argv to every process on this
    machine, which is what makes Agtr's `-c mcp_servers.X.env={…}` (agtros/mind/providers.py:478) a leak.
    """
    if names is None:
        names = cfg.names if isinstance(cfg, TurnConfig) else []
    # Only what this config holds. Allowing `mcp__bioql__*` from here would name a server the file does not carry,
    # and under --strict-mcp-config the entry would cover nothing: Studio's own server is the caller's to add
    # (studio/brain.py puts it in the same config).
    allow = list(allowed_also) + allowed_tools(names)
    return ["--mcp-config", os.fspath(cfg), "--strict-mcp-config", "--allowedTools", ",".join(dict.fromkeys(allow))]


# ---------------------------------------------------------------- a citation is a record
# The provenance a source may give. Nothing here is invented: a field the source did not give is ABSENT, which is
# information ("openFDA answered and has no DOI"), and is not the string "unknown".
PROVENANCE = ("doi", "pmid", "pmcid", "nct_id", "chembl_id", "uniprot", "ensembl_id", "accession",
              "url", "title", "year", "version", "retrieved_at")


def _field(source: str, record: dict, key: str) -> Resolution:
    v = record.get(key)
    # `resolved("")` passes resolution.py's own check, so the empty string is routed here: a source that answered
    # with an empty DOI did not give a DOI.
    if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict, tuple)) and not v):
        return absent(source, f"the answer carried no {key}")
    return resolved(v.strip() if isinstance(v, str) else v, source, key)


def cite(source: str, claim: str, ref, asked: str = "") -> dict:
    """The record a later reader follows: which server, what was asked, what came back (its sha256), and when.

    `ref` is what the source answered — an object, or the text an MCP tool returned — or a `Resolution` carrying how
    the lookup went. If the lookup failed (`UNRESOLVABLE`), the whole citation is UNRESOLVABLE: there is no sha256
    of an answer that never came, and not one provenance field reads ABSENT, because ABSENT would claim the source
    answered. Text is digested as it arrived and carries no `provenance` at all: nothing was parsed out of it, and
    a parsed-looking empty field would be an invention.

    A key is OMITTED rather than written empty — `asked` when no question was given, `detail` when there is none.
    An empty string in a record reads as an answer; it is not one.
    """
    check_name(source)
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("a citation needs the claim it supports")
    detail = ""
    if isinstance(ref, Resolution):
        if ref.unresolvable:
            # Degrading this to "the source has nothing" is the defect the three-state invariant exists to stop.
            out = {"kind": "citation", "state": ref.state, "source": source, "claim": claim.strip(),
                   "detail": ref.detail, "from": ref.source, "t": round(time.time(), 3)}
            return {**out, "asked": asked.strip()} if asked.strip() else out
        ref, detail = ({} if ref.absent else ref.value), ref.detail
    if isinstance(ref, (str, bytes)):
        raw = ref.encode("utf-8") if isinstance(ref, str) else ref
        out = {"kind": "citation", "state": "RESOLVED" if raw.strip() else "ABSENT", "source": source,
               "claim": claim.strip(), "answer": "text", "sha256": digest(raw), "t": round(time.time(), 3)}
    elif isinstance(ref, dict):
        asked = asked or str(ref.get("asked") or ref.get("query") or ref.get("id") or "")
        out = {"kind": "citation", "state": "RESOLVED" if ref else "ABSENT", "source": source,
               "claim": claim.strip(), "answer": "object", "sha256": digest(ref), "t": round(time.time(), 3),
               "provenance": {k: _field(source, ref, k).as_dict() for k in PROVENANCE}}
    else:
        raise TypeError(f"a citation's ref is the source's answer (an object or its text), "
                        f"not {type(ref).__name__}")
    for k, v in (("asked", asked.strip()), ("detail", detail)):
        if v:
            out[k] = v
    return out
