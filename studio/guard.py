"""The no-change guard: proof that a Studio session left BioQL as it found it.

`snapshot()` fingerprints BioQL's source trees (size, mtime, inode, and the sha256 of every file up to 4 MB);
`compare()` lists what was added, removed or changed. `session()` wraps a block of work: snapshot before, snapshot
after, and the session FAILS if anything under BioQL changed.

Honest limits, stated rather than hidden:
- the guard sees THAT a file changed, not WHO changed it. BioQL's own running services, or another person working in
  the repository at the same time, show up too; the report lists each file so a person can tell;
- runtime folders that BioQL writes as part of running (logs, caches, results, node_modules, .git internals) are left
  out, so a change there is not seen;
- git state is read with `--no-optional-locks`, so reading it never rewrites BioQL's index.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

from . import home, write_private

DATALESS = 0x40000000           # an iCloud file whose bytes are not on disk: never read it (that would download it)
HASH_UP_TO = 4 * 1024 * 1024
EXT = {".py", ".pyx", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".json", ".sh", ".toml", ".yaml", ".yml", ".cfg",
       ".ini", ".txt", ".md", ".html", ".css", ".plist", ".swift", ".sql", ".env", ".lock", ".ipynb", ".tex"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".codegraph", "graphify-out",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".local_stack_logs", "logs", ".remember", ".next", ".vite",
             "runtime", "results", "outputs", "cache", ".cache", "tmp"}

HOME = Path.home()
ROOTS = [
    {"name": "Spectrix_BioQL", "path": str(HOME / "Documents/Proyectos/Spectrix_BioQL"), "git": True},
    {"name": "opt_bioql", "path": "/opt/bioql"},
    {"name": "dot_bioql", "path": str(HOME / ".bioql")},
]


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: str) -> dict:
    def run(*a: str) -> str:
        r = subprocess.run(["git", "--no-optional-locks", "-C", root, *a], capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 else ""
    porcelain = run("status", "--porcelain=v1", "--untracked-files=no")
    return {"head": run("rev-parse", "HEAD").strip() or None, "tracked_changes": sorted(porcelain.splitlines())}


def snapshot(roots: list[dict] | None = None) -> dict:
    out = {"t": time.time(), "roots": {}}
    for r in roots or ROOTS:
        base = r["path"]
        files: dict[str, list] = {}
        skipped_dataless = 0
        if os.path.isdir(base):
            for d, ds, fs in os.walk(base):
                ds[:] = sorted(x for x in ds if x not in SKIP_DIRS)
                for f in fs:
                    if os.path.splitext(f)[1].lower() not in EXT and f not in ("Makefile", "Dockerfile"):
                        continue
                    p = os.path.join(d, f)
                    try:
                        s = os.lstat(p)
                    except OSError:
                        continue
                    if not stat.S_ISREG(s.st_mode):
                        continue
                    if getattr(s, "st_flags", 0) & DATALESS:
                        skipped_dataless += 1
                        files[os.path.relpath(p, base)] = [s.st_size, s.st_mtime_ns, s.st_ino, "dataless"]
                        continue
                    try:
                        h = _sha(p) if s.st_size <= HASH_UP_TO else None
                    except OSError:
                        h = "unreadable"
                    files[os.path.relpath(p, base)] = [s.st_size, s.st_mtime_ns, s.st_ino, h]
        out["roots"][r["name"]] = {"path": base, "exists": os.path.isdir(base), "files": files,
                                    "dataless": skipped_dataless, **({"git": _git(base)} if r.get("git") and os.path.isdir(base) else {})}
    return out


def _differs(b: list, a: list) -> bool:
    """By content when both sides were hashed; by size and mtime otherwise (large or dataless files)."""
    hashed = lambda x: isinstance(x[3], str) and len(x[3]) == 64
    if hashed(b) and hashed(a):
        return b[3] != a[3]
    return b[:2] != a[:2]


def compare(before: dict, after: dict) -> dict:
    report: dict = {"clean": True, "roots": {}}
    for name, b in before["roots"].items():
        a = after["roots"].get(name, {"files": {}, "exists": False})
        bf, af = b["files"], a["files"]
        added = sorted(set(af) - set(bf))
        removed = sorted(set(bf) - set(af))
        changed = sorted(k for k in set(bf) & set(af) if _differs(bf[k], af[k]))
        git = {}
        if "git" in b:
            if b["git"]["head"] != a.get("git", {}).get("head"):
                git["head"] = [b["git"]["head"], a.get("git", {}).get("head")]
            if b["git"]["tracked_changes"] != a.get("git", {}).get("tracked_changes"):
                git["tracked_changes"] = [b["git"]["tracked_changes"], a.get("git", {}).get("tracked_changes")]
        dirty = bool(added or removed or changed or git or b["exists"] != a["exists"])
        report["roots"][name] = {"added": added, "removed": removed, "changed": changed, "git": git, "clean": not dirty,
                                 "files": len(af)}
        report["clean"] = report["clean"] and not dirty
    return report


def save(name: str, snap: dict) -> Path:
    p = home() / "guard" / f"{name}.json"
    write_private(p, json.dumps(snap))
    return p


def load(name: str) -> dict:
    return json.loads((home() / "guard" / f"{name}.json").read_text())


class Changed(RuntimeError):
    def __init__(self, report: dict) -> None:
        self.report = report
        lines = []
        for n, r in report["roots"].items():
            if not r["clean"]:
                lines.append(f"{n}: +{len(r['added'])} -{len(r['removed'])} ~{len(r['changed'])}"
                             + (f" git {sorted(r['git'])}" if r["git"] else ""))
        super().__init__("BioQL changed during the session: " + "; ".join(lines))


@contextlib.contextmanager
def session(roots: list[dict] | None = None):
    """with guard.session() as g: ...  → raises Changed at the end if BioQL changed; g['report'] holds the report."""
    box: dict = {"before": snapshot(roots)}
    yield box
    box["after"] = snapshot(roots)
    box["report"] = compare(box["before"], box["after"])
    if not box["report"]["clean"]:
        raise Changed(box["report"])
