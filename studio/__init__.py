"""BioQL Studio: a layer around BioQL that never changes BioQL.

Studio reaches BioQL only through the HTTP entries BioQL already serves (127.0.0.1:5035), through a closed allowlist
the owner approves. It never imports a BioQL module, never writes inside BioQL's folders, and never starts or stops
BioQL's own services.
"""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.1.0"
NAME = "bioql-studio"           # its own identity: never "agtr-desktop" (that name is Agtr Desktop's, on 7790)


def home() -> Path:
    """Studio's own data folder. Outside ~/Documents (iCloud) and outside BioQL and Agtr."""
    raw = os.environ.get("BIOQL_STUDIO_HOME")
    p = Path(raw).expanduser() if raw else Path.home() / "Library" / "Application Support" / "BioQL Studio"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def bioql_url() -> str:
    return os.environ.get("BIOQL_BACKEND_URL", "http://127.0.0.1:5035").rstrip("/")


def write_private(path: Path, text: str) -> None:
    """Atomic write, 0600 from the first byte (never written open and chmod'ed after)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
