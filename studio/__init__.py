"""BioQL Studio: a layer around BioQL that never changes BioQL.

Studio reaches BioQL only through the HTTP entries BioQL already serves (127.0.0.1:5035), through a closed allowlist
the owner approves. It never imports a BioQL module, never writes inside BioQL's folders, and never starts or stops
BioQL's own services.
"""
from __future__ import annotations

import os
from pathlib import Path

# The version has ONE source, `app/__init__.py`, which `build.sh` and `dmg.sh` also read with sed and which the
# update channel compares against what GitHub publishes. `studio` used to keep a second copy, and the two had
# already drifted — `studio` said 0.1.0 while the app was 0.1.2. That is harmless until something compares
# versions, and now something does: a build that reported the wrong number to `release.latest` would see an
# update forever and install it forever.
#
# Read lazily and by TEXT, not by importing `app`: `studio` is the layer below it and must keep working without it.
def _version() -> str:
    import re
    try:
        src = (Path(__file__).resolve().parent.parent / "app" / "__init__.py").read_text()
    except OSError:
        return "0.0.0-unknown"
    m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else "0.0.0-unknown"


VERSION = _version()
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
