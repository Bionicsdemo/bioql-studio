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

# What BioQL WRITES while it works, as opposed to what it is made of. A pipeline that finishes drops its result
# under `job_results/` and its report under `artifacts/`; a run that does that has not been edited, it has run.
#
# This does NOT hide anything: an output is still listed, still counted and still makes the report not clean. What
# it changes is which half raises the alarm. Before this, every single pipeline run turned the guard red with
# "BioQL changed", and a guard that is red after every run is a guard nobody reads — which is the same as not
# having one. The alarm is for SOURCE.
OUTPUT_MARKS = ("job_results/", "artifacts/", "outputs/", "reports/", "checkpoints/", "exports/")
OUTPUT_EXT = (".log", ".png", ".jpg", ".svg", ".pdf", ".csv", ".parquet", ".pkl", ".npy", ".npz", ".h5", ".ckpt")


def is_output(path: str) -> bool:
    """Whether this path is something BioQL WROTE rather than something BioQL IS. Said by rule, in one place, so a
    person reading the report can check the rule against the path themselves.

    Paths in a snapshot are built by this module with `/`, so there is no separator to normalise — and no string
    `.replace` here, which the source guard in tests/test_guard_and_contract.py reads as a write.
    """
    p = str(path)
    return any(p.startswith(m) or ("/" + m) in p for m in OUTPUT_MARKS) or p.endswith(OUTPUT_EXT)


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
        # What could not be listed at all. `os.walk` swallows every error it meets unless it is given `onerror`, so
        # without this a root macOS refuses — `~/Documents` is behind a TCC permission, and an app that has not been
        # granted it is simply denied — walks as if it held no files. Two such snapshots then compare with nothing
        # added, removed or changed, and the guard would report BioQL clean because it could not look. That is the
        # third state degrading into the second, which the invariant forbids: a root that cannot be read is
        # UNRESOLVABLE, never "nothing changed".
        unreadable: list[str] = []

        def blocked(err: OSError, _into: list[str] = unreadable) -> None:
            _into.append(f"{getattr(err, 'filename', None) or base}: {err.strerror or type(err).__name__}")

        # `os.path.isdir` answers False for EVERY stat error, not only for "it is not there", so a root whose parent
        # macOS refuses looked exactly like a root that had been deleted: `exists: False` and nothing unreadable.
        # Measured 2026-11-19 with the parent at mode 000 — two such snapshots compared CLEAN, and a readable
        # snapshot against a denied one reported every file as removed, i.e. "BioQL changed". Both are false.
        try:
            exists = stat.S_ISDIR(os.stat(base).st_mode)
        except FileNotFoundError:
            exists = False                          # it is genuinely not there, and that IS information
        except OSError as e:
            exists = False
            unreadable.append(f"{base}: {e.strerror or type(e).__name__}")
        if exists:
            for d, ds, fs in os.walk(base, onerror=blocked):
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
        out["roots"][r["name"]] = {"path": base, "exists": exists, "files": files,
                                    "dataless": skipped_dataless,
                                    "unreadable": unreadable[:20], "unreadable_total": len(unreadable),
                                    **({"git": _git(base)} if r.get("git") and exists else {})}
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
        # A root either side could not list is not a verdict about BioQL: it is "we could not look", and it must not
        # be told as either "clean" or "changed". The lists are dropped rather than reported, because a side that
        # could not be read contributes files that look removed and files that look added without one byte of BioQL
        # having moved. What is left is the cause, and a report that refuses to be clean.
        blind = sorted(set(b.get("unreadable") or []) | set(a.get("unreadable") or []))
        if blind:
            added = removed = changed = []
            git = {}
        # Source and output are counted apart. `clean` still means "nothing moved at all" — it is not softened —
        # but `source_clean` is what the window raises an alarm on, because a finished pipeline writing its own
        # result is not BioQL being edited.
        src = {"added": [x for x in added if not is_output(x)],
               "removed": [x for x in removed if not is_output(x)],
               "changed": [x for x in changed if not is_output(x)]}
        outs = sorted(set(added + removed + changed) - set(src["added"] + src["removed"] + src["changed"]))
        source_dirty = bool(src["added"] or src["removed"] or src["changed"] or git or b["exists"] != a["exists"])
        report["roots"][name] = {"added": added, "removed": removed, "changed": changed, "git": git,
                                 "clean": not dirty and not blind, "files": len(af),
                                 "source": src, "outputs": outs,
                                 "source_clean": not source_dirty and not blind,
                                 "unreadable": blind, "readable": not blind, "compared": not blind}
        report["clean"] = report["clean"] and not dirty and not blind
        report["source_clean"] = report.get("source_clean", True) and not source_dirty and not blind
    report["unreadable"] = sorted({u for r in report["roots"].values() for u in r["unreadable"]})
    report["readable"] = not report["unreadable"]
    report.setdefault("source_clean", True)
    report["outputs_written"] = sum(len(r.get("outputs") or []) for r in report["roots"].values())
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
        lines, blind = [], []
        for n, r in report["roots"].items():
            if r["added"] or r["removed"] or r["changed"] or r["git"]:
                lines.append(f"{n}: +{len(r['added'])} -{len(r['removed'])} ~{len(r['changed'])}"
                             + (f" git {sorted(r['git'])}" if r["git"] else ""))
            if r.get("unreadable"):
                blind.append(f"{n}: {r['unreadable'][0]}" + (f" (+{len(r['unreadable']) - 1} more)"
                                                             if len(r["unreadable"]) > 1 else ""))
        # "Could not look" is not "changed". Said as one, a permission macOS refused would read as BioQL being
        # edited under the session, and the person would go hunting for an edit that never happened.
        say = []
        if lines:
            say.append("BioQL changed during the session: " + "; ".join(lines))
        if blind:
            say.append("part of BioQL could not be read, so the session cannot say it left BioQL as it found it: "
                       + "; ".join(blind))
        super().__init__(" — ".join(say) or "the guard reported not clean without saying why")


@contextlib.contextmanager
def session(roots: list[dict] | None = None):
    """with guard.session() as g: ...  → raises Changed at the end if BioQL changed; g['report'] holds the report."""
    box: dict = {"before": snapshot(roots)}
    yield box
    box["after"] = snapshot(roots)
    box["report"] = compare(box["before"], box["after"])
    if not box["report"]["clean"]:
        raise Changed(box["report"])
