"""Settings out of the menu — and the test that keeps the claim honest.

Half of these are the ordinary thing: the switch goes on, goes off, survives, and the window stops offering the
page by every door it has. The other half exist to make sure nobody later mistakes this for security. It HIDES.
If a future change starts refusing routes on the strength of it, `test_the_switch_guards_nothing` fails, and that
failure is the point: a lock that only the menu believes in is worse than no lock, because somebody acts on it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from studio import advanced

JS = (Path(__file__).resolve().parent.parent / "app" / "web" / "app.js").read_text()


def test_the_default_is_out_of_the_menu(tmp_path: Path):
    assert advanced.is_on(tmp_path) is False


def test_on_then_off_then_on(tmp_path: Path):
    assert advanced.set_on(True, tmp_path)["on"] is True
    assert advanced.is_on(tmp_path) is True
    assert advanced.set_on(False, tmp_path)["on"] is False
    assert advanced.is_on(tmp_path) is False
    assert advanced.set_on(True, tmp_path)["on"] is True


def test_turning_on_what_is_already_on_is_not_an_error(tmp_path: Path):
    advanced.set_on(True, tmp_path)
    out = advanced.set_on(True, tmp_path)
    assert out["state"] == "RESOLVED" and out["on"] is True


def test_turning_off_what_was_never_on_is_not_an_error(tmp_path: Path):
    out = advanced.set_on(False, tmp_path)
    assert out["state"] == "RESOLVED" and out["on"] is False


def test_presence_is_the_state_so_the_contents_never_decide(tmp_path: Path):
    """Somebody will open that file and edit it. Whatever they write, the file being there is what it means —
    a switch that could be turned off by typing the word 'off' into it would be a switch nobody can reason about."""
    advanced.set_on(True, tmp_path)
    advanced.file(tmp_path).write_text("off\nno\nfalse\n0\n")
    assert advanced.is_on(tmp_path) is True
    advanced.file(tmp_path).write_text("")
    assert advanced.is_on(tmp_path) is True


def test_a_folder_that_cannot_be_read_answers_no(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(advanced.Path, "exists", lambda self: (_ for _ in ()).throw(OSError("denied")))
    assert advanced.is_on(tmp_path) is False


def test_a_write_that_fails_is_unresolvable_and_says_so(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(advanced.Path, "write_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    out = advanced.set_on(True, tmp_path)
    assert out["state"] == "UNRESOLVABLE" and out["on"] is False and "read-only" in out["detail"]


# ------------------------------------------------------------------ what the window does with it

def test_the_window_closes_every_door_and_not_just_the_menu():
    """The menu is the obvious one. The palette iterates the same object, a tab is a door, open_ is a door, and
    two status chips carried data-go="settings". All of them go through one check."""
    assert "function shown(k)" in JS
    for door in ('Object.keys(SECTIONS).filter((k) => SECTIONS[k].group === g && shown(k))',  # the menu
                 'Object.keys(SECTIONS).filter(shown)',                                       # the palette
                 'openTabs.filter(shown)',                                                    # the tabs
                 'if (!s || !shown(k)) return;'):                                             # open_
        assert door in JS, f"this door is not behind the check: {door}"
    assert 'removeAttribute("data-go")' in JS, "the status chips stayed a way around the menu"


def test_the_way_back_is_in_the_window_and_in_a_terminal():
    """Hidden must not mean stranded. Somebody who hides it has to be able to find it again without reinstalling."""
    assert "Advanced: show Settings" in JS
    assert "studio advanced --on" in JS, "the window never names the terminal way back"
    for word in ('"advanced".includes(needle)', '"settings".includes(needle)', '"ajustes".includes(needle)'):
        assert word in JS


def test_the_window_says_it_hides_and_never_says_it_locks():
    assert "HIDES a page, it does not lock one" in JS or "HIDES a page, it does not lock" in JS
    # and the file that holds the switch says the same, where the next person will look
    src = (Path(__file__).resolve().parent.parent / "studio" / "advanced.py").read_text()
    assert "It hides. It does not lock" in src


def test_the_switch_guards_nothing(tmp_path: Path):
    """THE test. `advanced` must never appear in anything that decides whether a call is allowed.

    Studio has no users and no roles: the token is one file on a Mac whose owner can read it. A route that started
    refusing on the strength of this switch would be a lock only the menu believed in, and somebody would act on
    it. The allowlist, the approval gate and the adapter decide access; this decides a menu.
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("studio/adapter.py", "studio/allowlist.py", "studio/sources.py", "studio/mcp_server.py"):
        # Lowercased, because the first version of this test was case-sensitive and sailed straight past an
        # injected `ADVANCED_ONLY = {...}`. A guard that only catches the spelling you happened to imagine is the
        # same defect it is there to prevent.
        src = (root / name).read_text().lower()
        assert "advanced" not in src, f"{name} mentions the menu switch; access must not depend on it"


def test_the_service_reports_the_switch_rather_than_hiding_it():
    """/api/state carries it, because the window needs it and because withholding it would be the first step
    towards pretending it means more than it does."""
    src = (Path(__file__).resolve().parent.parent / "app" / "service.py").read_text()
    assert '"advanced": advanced.is_on(self.home)' in src


def test_the_update_is_visible_without_opening_settings():
    """Updates used to live only inside Settings, so hiding Settings would have hidden a channel that could not be
    reached. The check runs by itself and a staged release installs itself on quit — but an unreachable channel is
    worth seeing, and the version chip is where it is seen."""
    assert "update channel unreachable" in JS
    assert "installs on quit" in JS
    src = (Path(__file__).resolve().parent.parent / "app" / "service.py").read_text()
    assert '"update": {"state": self.update.get("state")' in src
