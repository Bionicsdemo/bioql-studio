"""The switch that keeps Settings out of the way — and what it is honestly worth.

**It hides. It does not lock.** Studio has no users, no roles and no login: one token per install, and anyone with a
session on this Mac can read it out of `HOME` and call every route directly. So this switch takes a page out of the
menu, the palette and the tabs, and that is the whole of it. `/api/state`, `/api/update` and the rest answer exactly
the same with it off as with it on, because pretending otherwise would be the worse outcome: a painted lock on an
app that decides whether real quantum hardware gets spent is more dangerous than no lock, since somebody will
believe it.

What it IS good for: a window somebody else is looking at should not open on the version numbers, the data paths,
the update channel and the MCP registration of the person who set the machine up. That is the whole job.

A real boundary is not available from inside this process and is not faked here. It would have to come from the
operating system — a separate account whose files this one cannot read — and that is outside what a desktop app
installed in `/Applications` can grant itself.

The state is one file in Studio's own HOME, present or absent, because this must be about the INSTALL and not
about a browser profile: `localStorage` is per-origin-per-profile, empties in a private window, and would put the
owner's app back into its opened-up state the first time that storage was cleared.
"""
from __future__ import annotations

from pathlib import Path

from . import home

NAME = "advanced"
# What goes in the file, for whoever opens it wondering. The CONTENT is never read: presence is the state, so a
# file somebody empties or fills with anything else still means on.
NOTE = ("on\n\nThis file being here is what puts Settings back in BioQL Studio's menu. Delete it and Settings "
        "disappears again. It hides a page; it locks nothing — see studio/advanced.py.\n")


def file(where: Path | None = None) -> Path:
    return (where or home()) / NAME


def is_on(where: Path | None = None) -> bool:
    """Is Settings shown? A folder that cannot be read answers 'no': the quiet state is the safe default here,
    and unlike a fact about the outside world this one has a correct answer when we cannot look."""
    try:
        return file(where).exists()
    except OSError:
        return False


def set_on(on: bool, where: Path | None = None) -> dict:
    """Put the switch where it is asked for, and say what happened. Turning on what is already on is not an error."""
    f = file(where)
    try:
        if on:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(NOTE, encoding="utf-8")
        elif f.exists():
            f.unlink()
    except OSError as why:
        return {"state": "UNRESOLVABLE", "on": is_on(where), "path": str(f),
                "detail": f"{type(why).__name__}: {str(why)[:120]}"}
    return {"state": "RESOLVED", "on": is_on(where), "path": str(f),
            "detail": ("Settings is in the menu." if on else
                       "Settings is out of the menu. It hides the page; every route answers the same.")}
