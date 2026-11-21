"""PyMOL, as a camera. A ray-traced approach of a ligand into its site, as FRAMES — not as a GIF.

Why this exists at all. BioQL has its own cinematic animation route (`/api/render/docking_animation`,
`pymol_flask_endpoint.py:582`) and the recipe in it is good: fancy helices, a translucent surface, a glow on the
binding site, ray tracing with shadows, a smoothstep approach and a camera orbit. It cannot run on this Mac. Its
per-frame script does `import pymol2`, and BioQL's interpreter (`~/.bioql/venv/bin/python`) has no `pymol2` —
measured 2026-11-20: eight frames asked for, eight frames failed, and the route answered
`{"error": "No frames rendered"}` in 2.2 s with every real cause swallowed. PyMOL 3.1.0 IS on this Mac, as the
Homebrew binary, which is exactly what `studio/render3d.py` already drives at 0.45 s a still.

So Studio renders the film itself, down the path that is known to work, and keeps BioQL untouched.

Three things here are deliberately different from BioQL's version, and each is a measured reason:

  1. **One PyMOL, not one per frame.** BioQL spawns a process per frame; PyMOL's startup is about a second, so
     24 frames paid 24 startups before any ray tracing. Here the loop is inside a single session.
  2. **Frames, not a GIF.** A GIF loops and cannot be scrubbed, stepped or held. The window gets the frames and
     owns the playback, so the film has a timeline — which is the whole point of looking at an approach.
  3. **No network, and no PDB id.** BioQL's route fetches a structure from RCSB. This one renders the structure the
     window ALREADY has, which is the one the run actually produced. A picture of a different protein than the one
     that was computed is the most expensive kind of wrong.

The security posture is `render3d.py`'s, unchanged and slightly stricter: the PyMOL script is a fixed string in
this file, the structures go in as files, and **nothing from the caller is interpolated into code** — the varying
numbers travel in a JSON file the script reads. PyMOL's language can open files, write files and `run` Python; a
route that let a caller shape that text would be a shell with a molecule in front of it.

A short film is never passed off as a whole one: if fewer frames come back than were asked for, that is
UNRESOLVABLE with the count, not a film that quietly skips. That failure mode is the one BioQL's route shipped.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .render3d import EXT, MAX_BYTES, binary, version
from .resolution import Resolution, absent, resolved, unresolvable

FRAMES_DEFAULT = 24
FRAMES_MAX = 36
MAX_PX = 1600
TIMEOUT = 420.0          # a still is 0.45 s; 36 ray-traced frames with shadows is minutes, and seven is a hang

# How far out the ligand starts, in angstrom, along the axis from the receptor's centre through the ligand's.
APPROACH_A = 18.0
ORBIT_DEG = 180.0        # how far the camera travels over the whole film

# ---------------------------------------------------------------------------------------------- the script
# PyMOL runs this as Python (`pymol -cq film.py`). It is this file's own text: no caller value is ever formatted
# into it. Everything that varies is read from `film.json`, which this module writes beside it.
SCRIPT = r'''
import json, os
from pymol import cmd

# NOT `__file__`: PyMOL execs this script with `__file__` pointing into its own site-packages
# (/opt/homebrew/Cellar/pymol/.../pymol/film.json — measured 2026-11-20), so the config was looked for inside
# PyMOL's install. HOME is set by the parent to the work directory and is the one path this script can trust.
cfg = json.load(open(os.path.join(os.environ["HOME"], "film.json")))

cmd.load(cfg["receptor"], "receptor")
has_lig = bool(cfg.get("ligand"))
if has_lig:
    cmd.load(cfg["ligand"], "ligand")

# --- the look.
#
# This started as BioQL's own recipe and was rewritten after looking at what it produces on a real 367 KB protein
# (2026-11-20): a black background with ambient 0.25, a surface at transparency 0.85 over the whole receptor and a
# green glow sphere scaled off the ligand's extent gave a near-black blob with a green disc swallowing it, the
# cartoon invisible under the surface. Cinematic settings that are never looked at are just settings.
#
# What a medicinal chemist needs to read in one glance: the fold, the pocket, and the ligand's chemistry. So:
# cartoon for the fold, a translucent surface ONLY around the site (it is also far cheaper to ray-trace than a
# whole-protein transparent surface), element colours on the ligand, and light enough to see.
cmd.hide("everything")
cmd.show("cartoon", "receptor")
cmd.color("gray70", "receptor")
cmd.set("cartoon_fancy_helices", 1)
cmd.set("cartoon_smooth_loops", 1)
cmd.spectrum("count", "slate skyblue palecyan", "receptor and name CA")
if has_lig:
    cmd.show("sticks", "ligand")
    cmd.show("spheres", "ligand")
    cmd.set("stick_radius", 0.17, "ligand")
    cmd.set("sphere_scale", 0.26, "ligand")
    cmd.color("yellow", "ligand and elem C")
    cmd.color("red", "ligand and elem O")
    cmd.color("marine", "ligand and elem N")
    cmd.color("orange", "ligand and elem S")
    cmd.color("salmon", "ligand and elem P")
    cmd.color("palegreen", "ligand and elem F+Cl+Br+I")

cmd.bg_color("white")
cmd.set("ray_opaque_background", 1)
cmd.set("antialias", 2)
cmd.set("ray_trace_mode", 0)
cmd.set("ambient", 0.32)
cmd.set("direct", 0.62)
cmd.set("reflect", 0.35)
cmd.set("specular", 0.25)
cmd.set("shininess", 40)
# Shadows cost most of the ray trace and, on a translucent surface, mostly make mud. Measured at 8.02 s a frame
# with them on over the whole receptor; this is what pays for a film somebody will actually wait for.
cmd.set("ray_shadows", "off")
cmd.set("transparency_mode", 2)
cmd.set("depth_cue", 0)
# A surface cut open by the clipping plane shows its INSIDE, and PyMOL paints that black by default: the first
# render of the pocket had black patches across it that read as holes in the protein. These three are what make a
# cut-open pocket look like a pocket.
cmd.set("ray_interior_color", "gray80")
cmd.set("two_sided_lighting", "on")
cmd.set("surface_quality", 1)


def centre(sel):
    """Centre of mass, falling back to the box centre: an SDF with no element guesses has no mass, and a film
    that refuses to render because of that would be worse than one centred on the bounding box."""
    try:
        c = cmd.centerofmass(sel)
        if c and len(c) == 3:
            return list(c)
    except Exception:
        pass
    mn, mx = cmd.get_extent(sel)
    return [(a + b) / 2.0 for a, b in zip(mn, mx)]


# --- is that one ligand, or scattered heteroatoms?
#
# A PDB's HETATM records are often waters and ions spread over the whole protein. Taking their centroid gives a
# point inside the protein and an approach axis that means nothing — measured on a real complex (43 HETATM over a
# 4699-atom receptor): the "ligand" spanned the entire structure and the film showed red dots everywhere. A real
# small molecule is compact, so the span is the test, and when it fails the film is an orbit of the complex
# instead of an approach. An approach that was never computed must not be animated as though it was.
SCATTER_A = 24.0
if has_lig:
    mn, mx = cmd.get_extent("ligand")
    span = max(mx[i] - mn[i] for i in range(3))
    if span > SCATTER_A:
        print("FILMNOTE ligand spans %.1f A (> %.0f): not one molecule, filming an orbit" % (span, SCATTER_A))
        approach_ok = False
    else:
        approach_ok = True
else:
    approach_ok = False

site = centre("ligand") if has_lig else centre("receptor")
ax = [1.0, 0.0, 0.0]
if approach_ok:
    base = centre("receptor")
    v = [site[i] - base[i] for i in range(3)]
    n = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    # A ligand sitting almost on the receptor's centre gives no direction; coming in along the view is the
    # honest default, not a random axis.
    ax = [a / n for a in v] if n > 1e-6 else [0.0, 0.0, 1.0]

# The surface goes ONLY around the site: it is what shows the pocket, and a whole-protein translucent surface
# both hides the fold and is where the ray tracer spends its time.
if has_lig and approach_ok:
    cmd.select("site", "byres (receptor within 7.5 of ligand)")
    if cmd.count_atoms("site"):
        cmd.show("surface", "site")
        cmd.set("transparency", 0.45, "site")
        cmd.color("wheat", "site and elem C")
    cmd.delete("site")

# The frame is set ONCE, on the receptor, so the camera does not jump when the ligand moves. Zooming per frame is
# what makes a hand-made approach look like a slideshow.
cmd.orient("receptor")
cmd.zoom("receptor", 3)

n_frames = int(cfg["n_frames"])
w, h = int(cfg["width"]), int(cfg["height"])
orbit = float(cfg["orbit_deg"]) / max(n_frames - 1, 1)
approach = float(cfg["approach_a"])

placed = 0.0
for i in range(n_frames):
    t = i / max(n_frames - 1, 1)
    eased = t * t * (3.0 - 2.0 * t)          # smoothstep: it arrives, it does not skid
    want = approach * (1.0 - eased)
    if approach_ok:
        d = want - placed
        cmd.translate([ax[0] * d, ax[1] * d, ax[2] * d], object="ligand", camera=0)
        placed = want
    if i:
        cmd.turn("y", orbit)
    out = os.path.join(cfg["out_dir"], "frame_%03d.png" % i)
    cmd.ray(w, h)
    cmd.png(out, dpi=144)
    # The parent counts THESE lines, not the files it hoped for.
    print("FILMFRAME %d %s" % (i, "ok" if os.path.isfile(out) and os.path.getsize(out) else "empty"))

print("FILMDONE %d" % n_frames)
'''


def _write(work: Path, name: str, data: str, fmt: str) -> tuple[Path | None, Resolution | None]:
    """One structure onto disk, with the extension PyMOL reads its format from."""
    ext = EXT.get(str(fmt).strip().lower())
    if ext is None:
        return None, absent("pymol", f"{fmt!r} is not a format this renderer writes "
                                     f"({', '.join(sorted(EXT))})")
    if len(data) > MAX_BYTES:
        return None, absent("pymol", f"{name} is {len(data)} bytes and the cap is {MAX_BYTES}; it is not cut "
                                     f"down, because a cut structure is a different molecule")
    p = work / f"{name}.{ext}"
    p.write_text(data, encoding="utf-8")
    return p, None


def film(receptor: str, receptor_fmt: str, ligand: str = "", ligand_fmt: str = "",
         n_frames: int = FRAMES_DEFAULT, width: int = 900, height: int = 640) -> Resolution:
    """A ray-traced approach, frame by frame.

    RESOLVED carries `{frames: [png_base64, ...], n_frames, seconds, per_frame, pymol, width, height, has_ligand}`.

    ABSENT is about this Mac or this request — no PyMOL installed, no structure text, a format PyMOL does not read.
    UNRESOLVABLE is PyMOL there and not delivering: a crash, a hang, or fewer frames than were asked for. The last
    one matters most: a film with holes in it is not a shorter film, it is a wrong one, and BioQL's own route
    answered exactly that way.
    """
    if not isinstance(receptor, str) or not receptor.strip():
        return absent("pymol", "there is no structure to film")
    n = max(2, min(FRAMES_MAX, int(n_frames)))
    w, h = max(200, min(MAX_PX, int(width))), max(200, min(MAX_PX, int(height)))
    exe = binary()
    if not exe:
        return absent("pymol", "PyMOL is not on this Mac (`brew install pymol` puts it in /opt/homebrew/bin)")

    work = Path(tempfile.mkdtemp(prefix="bioql-studio-film-"))
    try:
        rec, why = _write(work, "receptor", receptor, receptor_fmt)
        if why is not None:
            return why
        lig = None
        if isinstance(ligand, str) and ligand.strip():
            lig, why = _write(work, "ligand", ligand, ligand_fmt)
            if why is not None:
                return why
        out_dir = work / "frames"
        out_dir.mkdir(mode=0o700)
        (work / "film.json").write_text(json.dumps({
            "receptor": str(rec), "ligand": str(lig) if lig else "", "out_dir": str(out_dir),
            "n_frames": n, "width": w, "height": h, "orbit_deg": ORBIT_DEG, "approach_a": APPROACH_A,
        }), encoding="utf-8")
        script = work / "film.py"
        script.write_text(SCRIPT, encoding="utf-8")

        started = time.monotonic()
        try:
            r = subprocess.run([exe, "-cq", str(script)], capture_output=True, text=True, timeout=TIMEOUT,
                               cwd=str(work), env={"PATH": "/usr/bin:/bin", "HOME": str(work)})
        except subprocess.TimeoutExpired:
            return unresolvable("pymol", f"PyMOL did not finish {n} frames in {TIMEOUT:.0f}s")
        except OSError as err:
            return unresolvable("pymol", f"{type(err).__name__}: {str(err)[:160]}")
        seconds = round(time.monotonic() - started, 2)

        said = [ln.split() for ln in (r.stdout or "").splitlines() if ln.startswith("FILMFRAME ")]
        ok = {int(p[1]) for p in said if len(p) > 2 and p[2] == "ok"}
        files = sorted(out_dir.glob("frame_*.png"))
        if not files:
            tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["no output"])[-1][:200]
            return unresolvable("pymol", f"PyMOL rendered no frame (exit {r.returncode}): {tail}")
        if len(ok) != n or len(files) != n:
            # Named plainly, with both counts, because this is the exact failure BioQL's route hid behind
            # "No frames rendered".
            tail = ((r.stderr or "").strip().splitlines() or [""])[-1][:160]
            return unresolvable("pymol", f"{n} frames asked for, {len(ok)} rendered and {len(files)} written "
                                         f"(exit {r.returncode}){': ' + tail if tail else ''}")
        frames = [base64.b64encode(f.read_bytes()).decode("ascii") for f in files]
        ver = version(exe)
        return resolved({"frames": frames, "n_frames": len(frames), "seconds": seconds,
                         "per_frame": round(seconds / len(frames), 2), "width": w, "height": h,
                         "has_ligand": bool(lig), "pymol": ver.value if ver.resolved else None},
                        "pymol", f"{exe} · {len(frames)} frames ray {w}x{h} · {seconds}s "
                                 f"({round(seconds / len(frames), 2)}s/frame)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
