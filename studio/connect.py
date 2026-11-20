"""Plugging BioQL into every Claude on this Mac, behind the scenes.

Studio is already an MCP server — `python3 -m studio mcp` answers 510 tools over stdio: BioQL's 502 pipelines with
the approval gate on them, `run_many`, and the seven that read Studio's own record. Any Claude client that speaks
MCP can use it. What was missing was the registration, so BioQL only existed inside Studio's own window.

This writes that registration into the clients it finds, so BioQL is there in whatever Claude you already have
open, with nothing new to launch. The gate travels with the server: a `compute` entry refuses without a person's
approval wherever it is called from, and every call lands on the same signed record.

**What it will and will not touch.** It adds, updates or removes ONE key — `bioql` — under `mcpServers`, and
copies the file aside first. Every other server in that file, and every other setting, is left exactly as it was;
a config that cannot be parsed is reported and not rewritten, because a config this rewrites blind is somebody's
whole Claude setup.

**What this widens, said plainly.** Once registered, any Claude session running as this user can call BioQL's
read-only entries without going through Studio's window. That is the point of it — and it is also the cost, so it
is a command somebody runs, never something an app does to a machine on its own. What it does NOT widen: anything
that spends compute or could reach real quantum hardware still refuses without an approval, which this door cannot
give; and every call is still one line on the hash-chained record.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from .resolution import Resolution, absent, resolved, unresolvable

KEY = "bioql"

# The clients, by where they keep their MCP servers. Both use the same `mcpServers` object.
CLIENTS = (
    {"name": "Claude Code", "path": Path.home() / ".claude.json"},
    {"name": "Claude Desktop",
     "path": Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"},
    # Claude Science keeps its own config; it is included by path so that registering works the day it is
    # installed, and simply reports "not on this Mac" until then.
    {"name": "Claude Science",
     "path": Path.home() / "Library" / "Application Support" / "Claude Science" / "config.json"},
)


def package_root() -> Path:
    """The directory `python3 -m studio` must run from: the one holding `studio/`. Inside the app this is the
    bundle's own copy, so a registration keeps working after the repository is moved or deleted."""
    return Path(__file__).resolve().parent.parent


def python_bin() -> str:
    """The interpreter to launch. `sys.executable` is the one actually running this, which is the honest answer —
    a registration naming a `python3` that happens to be first on some other process's PATH is a registration that
    works today and breaks on a machine that is one Homebrew upgrade away."""
    return sys.executable or shutil.which("python3") or "/usr/bin/python3"


def entry() -> dict:
    """What gets written. No secret goes in it: the MCP server reads the allowlist and the record from HOME, at
    0600, and carries no token of its own."""
    return {"command": python_bin(), "args": ["-m", "studio", "mcp"], "cwd": str(package_root()),
            "env": {"PYTHONPATH": str(package_root())}}


def _read(p: Path) -> tuple[dict | None, str | None]:
    if not p.exists():
        return None, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as why:
        return None, f"{type(why).__name__}: {str(why)[:160]}"
    return (d, None) if isinstance(d, dict) else (None, "that config is not a JSON object")


def status() -> list[dict]:
    """Where BioQL is plugged in, one row per client. Three states per row, kept apart: not installed, installed
    and not registered, installed and registered — and a config that cannot be read is none of those."""
    out = []
    want = entry()
    for c in CLIENTS:
        p: Path = c["path"]
        d, bad = _read(p)
        if bad:
            out.append({"client": c["name"], "path": str(p), "state": "UNRESOLVABLE", "detail": bad})
            continue
        if d is None:
            out.append({"client": c["name"], "path": str(p), "state": "ABSENT",
                        "detail": "not on this Mac (no config file)"})
            continue
        mine = (d.get("mcpServers") or {}).get(KEY)
        if not mine:
            out.append({"client": c["name"], "path": str(p), "state": "NOT_REGISTERED",
                        "detail": f"{len(d.get('mcpServers') or {})} other server(s) there; BioQL is not one"})
            continue
        stale = mine.get("cwd") != want["cwd"] or mine.get("command") != want["command"]
        out.append({"client": c["name"], "path": str(p),
                    "state": "STALE" if stale else "REGISTERED",
                    "detail": (f"registered, but pointing at {mine.get('cwd')} with {mine.get('command')}"
                               if stale else f"registered: {mine.get('command')} -m studio mcp"),
                    "cwd": mine.get("cwd")})
    return out


def _write(p: Path, d: dict) -> None:
    # The copy aside first, with the time in its name: this file is somebody's whole Claude setup, and an edit to
    # it that cannot be undone is not an edit anybody should accept from a program.
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + f".bioql-backup-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = p.with_name(p.name + ".tmp")
    mode = (p.stat().st_mode & 0o777) if p.exists() else 0o600
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)


def apply(remove: bool = False) -> Resolution:
    """Register (or unregister) BioQL in every client that is here. A client that is not installed is skipped and
    said; nothing is created for a client that does not exist."""
    done, skipped, failed = [], [], []
    want = entry()
    for c in CLIENTS:
        p: Path = c["path"]
        d, bad = _read(p)
        if bad:
            failed.append({"client": c["name"], "path": str(p), "detail": bad})
            continue
        if d is None:
            skipped.append({"client": c["name"], "path": str(p), "detail": "not on this Mac"})
            continue
        servers = d.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        if remove:
            if KEY not in servers:
                skipped.append({"client": c["name"], "path": str(p), "detail": "was not registered"})
                continue
            servers.pop(KEY)
        else:
            if servers.get(KEY) == want:
                skipped.append({"client": c["name"], "path": str(p), "detail": "already registered, unchanged"})
                continue
            servers[KEY] = want
        d["mcpServers"] = servers
        try:
            _write(p, d)
        except OSError as why:
            failed.append({"client": c["name"], "path": str(p), "detail": f"{type(why).__name__}: {why}"})
            continue
        done.append({"client": c["name"], "path": str(p),
                     "detail": "unregistered" if remove else "registered"})
    out = {"changed": done, "skipped": skipped, "failed": failed, "entry": want,
           "note": ("BioQL is no longer offered to these clients." if remove else
                    "Restart the client to pick it up. Anything that spends compute still refuses without a "
                    "person's approval, wherever it is called from, and every call is one line on the record.")}
    if failed and not done:
        return unresolvable("connect", "; ".join(f"{f['client']}: {f['detail']}" for f in failed), **out)
    if not done:
        return absent("connect", "nothing to change: " + "; ".join(f"{s['client']} ({s['detail']})" for s in skipped),
                      **out)
    return resolved(out, "connect", f"{len(done)} client(s) {'un' if remove else ''}registered")
