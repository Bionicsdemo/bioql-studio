"""PyMOL, as a renderer. Studio's own picture of a structure it already has.

PyMOL cannot live inside the window — it is a native OpenGL program and the window is a WKWebView whose policy is
`default-src 'none'`. What PyMOL is unmatched at is the still: a ray-traced cartoon at publication quality.
Measured on this Mac, 2026-11-19, with the 129 KB KRAS protein of job 10620ccd-07b: **0.45 s of ray tracing**,
900x700, PyMOL 3.1.0 from Homebrew.

So the division is this, and it is deliberate:
  · the interactive view stays in the window (3Dmol), where you turn the molecule;
  · the still comes from PyMOL, on request, when you want the picture.

What this module will NOT do, and why it is written this way:

  1. **It never runs a script anybody sent.** The commands are a fixed string in this file. The caller chooses a
     style out of a closed list and nothing else. PyMOL's language can open files, write files and `run` Python —
     a render route that accepted commands would be a shell with a molecule in front of it.
  2. **The structure goes in as a FILE, passed as an argument**, never interpolated into the script, so no content
     can close a quote and become a command.
  3. **It is bounded**: a size cap, a timeout, a temp directory of its own that is removed afterwards, and no
     network (PyMOL would happily `fetch` a PDB id from the internet; nothing here gives it one).
  4. **It states what rendered it.** The answer carries PyMOL's own version and the seconds it took. A picture with
     no provenance is a drawing.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .resolution import Resolution, absent, resolved, unresolvable

# Where PyMOL is, if it is here at all. No PATH search beyond these: an app opened from the Finder has
# LaunchServices' PATH, and a `pymol` found somewhere unexpected is not a dependency anybody declared.
WHERE_PYMOL = ("/opt/homebrew/bin/pymol", "/usr/local/bin/pymol", "/opt/homebrew/opt/pymol/bin/pymol")

# The formats PyMOL is given, and the extension each must be written with: PyMOL reads the format off the file's
# extension, so this mapping IS the format declaration.
EXT = {"pdb": "pdb", "cif": "cif", "mmcif": "cif", "sdf": "sdf", "mol": "mol", "mol2": "mol2", "xyz": "xyz"}

MAX_BYTES = 8 * 1024 * 1024          # a structure larger than this is not rendered; it is said, not truncated
TIMEOUT = 90.0                       # 0.45 s measured on a 129 KB protein; ninety is a hang, not a slow render
MAX_PX = 2000

# The closed list of looks. A style is a NAME here, never commands from the caller.
STYLES: dict[str, str] = {
    "cartoon": "hide everything\nshow cartoon\nspectrum count, rainbow, name CA\n"
               "show sticks, not polymer\ncolor grey70, not polymer and elem C",
    "surface": "hide everything\nshow surface\nset transparency, 0.25\ncolor grey80\n"
               "show sticks, not polymer\ncolor marine, not polymer and elem C",
    "sticks": "hide everything\nshow sticks\nutil.cbaw()\nshow nonbonded",
    "spheres": "hide everything\nshow spheres\nutil.cbag()",
}

# Everything else about the picture. Fixed, in this file, and never built out of a request.
FRAME = (
    "bg_color white\n"
    "set ray_opaque_background, 1\n"
    "set antialias, 2\n"
    "set ray_shadows, 0\n"
    "set specular, 0.2\n"
    "set cartoon_fancy_helices, 1\n"
    "set depth_cue, 0\n"
    "orient\n"
    "zoom all, 2\n"
)


def binary() -> str | None:
    """PyMOL's path, or None. `shutil.which` first so a PATH that has it wins, then the places it installs."""
    exe = os.environ.get("PYMOL_BIN") or shutil.which("pymol")
    if exe and os.access(exe, os.X_OK):
        return exe
    for raw in WHERE_PYMOL:
        p = Path(raw).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def version(exe: str | None = None) -> Resolution:
    """What PyMOL says it is. ABSENT when PyMOL is not installed — that is an answer about this Mac, not a failure
    to ask — and UNRESOLVABLE when it is there and would not answer."""
    exe = exe or binary()
    if not exe:
        return absent("pymol", "PyMOL is not on this Mac (`brew install pymol` puts it in /opt/homebrew/bin)")
    try:
        r = subprocess.run([exe, "-cq", "-d", "print('VER', cmd.get_version()[0])"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as why:
        return unresolvable("pymol", f"{type(why).__name__}: {str(why)[:160]}")
    for line in (r.stdout or "").splitlines():
        if line.strip().startswith("VER "):
            return resolved(line.strip()[4:].strip(), "pymol", f"{exe} -cq")
    return unresolvable("pymol", f"PyMOL answered without a version (exit {r.returncode})")


def render(data: str, fmt: str, style: str = "cartoon", width: int = 1000, height: int = 760) -> Resolution:
    """One structure, ray-traced. RESOLVED carries `{png_base64, seconds, pymol, style, width, height}`.

    The three states are kept apart on purpose: no PyMOL on this Mac is ABSENT (ask somebody to install it), and a
    PyMOL that crashed, hung or wrote nothing is UNRESOLVABLE — never an empty picture, and never the interactive
    view quietly standing in for a render that did not happen.
    """
    if not isinstance(data, str) or not data.strip():
        return absent("pymol", "there is no structure text to render")
    if len(data) > MAX_BYTES:
        return absent("pymol", f"the structure is {len(data)} bytes and this renderer takes at most {MAX_BYTES}; "
                               f"it is not cut down, because a cut structure is a different molecule")
    ext = EXT.get(str(fmt).strip().lower())
    if ext is None:
        return absent("pymol", f"{fmt!r} is not a format this renderer writes to disk ({', '.join(sorted(EXT))})")
    look = STYLES.get(str(style).strip().lower())
    if look is None:
        return absent("pymol", f"{style!r} is not one of the styles ({', '.join(sorted(STYLES))})")
    w, h = max(200, min(MAX_PX, int(width))), max(200, min(MAX_PX, int(height)))
    exe = binary()
    if not exe:
        return absent("pymol", "PyMOL is not on this Mac (`brew install pymol` puts it in /opt/homebrew/bin)")

    work = Path(tempfile.mkdtemp(prefix="bioql-studio-pymol-"))
    try:
        src = work / f"structure.{ext}"
        src.write_text(data, encoding="utf-8")
        out = work / "render.png"
        # The script is this file's own text. The only thing that varies is the style name chosen from STYLES and
        # the size, and the two paths, which this process created.
        script = f"{look}\n{FRAME}ray {w}, {h}\npng {out}, dpi=144\n"
        started = time.monotonic()
        try:
            r = subprocess.run([exe, "-cq", str(src), "-d", script],
                               capture_output=True, text=True, timeout=TIMEOUT,
                               cwd=str(work), env={"PATH": "/usr/bin:/bin", "HOME": str(work)})
        except subprocess.TimeoutExpired:
            return unresolvable("pymol", f"PyMOL did not finish in {TIMEOUT:.0f}s")
        except OSError as why:
            return unresolvable("pymol", f"{type(why).__name__}: {str(why)[:160]}")
        seconds = round(time.monotonic() - started, 2)
        if not out.is_file() or out.stat().st_size == 0:
            tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["no output"])[-1][:200]
            return unresolvable("pymol", f"PyMOL wrote no image (exit {r.returncode}): {tail}")
        png = base64.b64encode(out.read_bytes()).decode("ascii")
        ver = version(exe)
        return resolved({"png_base64": png, "seconds": seconds, "style": str(style).strip().lower(),
                         "width": w, "height": h,
                         "pymol": ver.value if ver.resolved else None},
                        "pymol", f"{exe} -cq · ray {w}x{h} · {seconds}s")
    finally:
        shutil.rmtree(work, ignore_errors=True)
