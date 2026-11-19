"""What recipes and schedules must never do: run something a person has to approve, fire a missed run N times,
ignore the off switch, or read a corrupt store as "nothing is scheduled".

The runner is injected everywhere here, so no test reaches a model or BioQL. A `run` that is never called is the
point of half of these tests, so every stub counts its calls and records the approval it was handed.
"""
import json
import os
import time

import pytest

from studio import recipes
from studio.recipes import Schedules
from studio.resolution import Unresolvable


class Runner:
    """A stand-in for Claude Code. It records what it was asked and what approval came with it."""

    def __init__(self, answer=None, boom=None):
        self.calls = []
        self.answer = answer if answer is not None else {"state": "RAN", "ok": True}
        self.boom = boom

    def __call__(self, req):
        self.calls.append(req)
        if self.boom:
            raise self.boom
        return self.answer


@pytest.fixture
def store(tmp_path):
    return tmp_path / "schedules.json"


@pytest.fixture
def own(tmp_path):
    return tmp_path / "recipes.json"


@pytest.fixture(autouse=True)
def schedules_on(monkeypatch):
    monkeypatch.delenv(recipes.OFF, raising=False)


def sched(run, store, own=None, **kw):
    return Schedules(run=run, file=store, recipes_file=own)


# ------------------------------------------------------------------ the recipes themselves
def test_the_built_ins_are_this_domains_requests_in_precise_words():
    ids = {r["id"] for r in recipes.builtin()}
    assert ids == {"due_diligence", "admet_sweep", "rerun_compare", "engine_changed", "weekly_digest"}
    for r in recipes.builtin():
        assert len(r["ask"]) > 200 and r["title"] and r["why"], r["id"]
        assert r["declared"] is True                      # each says what it reaches
    # the guard recipe is the one that asks about the engine, and it reaches nothing of BioQL's
    guard = next(r for r in recipes.builtin() if r["id"] == "engine_changed")
    assert guard["uses"] == [] and guard["needs"] == [] and "no-change guard" in guard["ask"]


def test_the_owners_own_recipes_live_beside_the_built_ins(own):
    recipes.save_recipe("my_sweep", "My sweep", "Do the thing I always ask for, over the set in notes.md.",
                        uses=["smiles_to_3d"], needs=["local"], file=own)
    got = {r["id"]: r for r in recipes.listed(own)}
    assert got["my_sweep"]["own"] is True and len(got) == 6
    assert oct(own.stat().st_mode)[-3:] == "600"
    assert recipes.find("my_sweep", own)["title"] == "My sweep"
    assert recipes.remove_recipe("my_sweep", own) is True
    assert recipes.remove_recipe("my_sweep", own) is False
    with pytest.raises(ValueError):
        recipes.find("my_sweep", own)


def test_a_recipe_without_a_request_is_refused(own):
    with pytest.raises(ValueError):
        recipes.save_recipe("empty", "Empty", "   ", file=own)
    with pytest.raises(ValueError):
        recipes.save_recipe("Bad Id", "x", "a request", file=own)
    with pytest.raises(ValueError):
        recipes.save_recipe("k", "x", "a request", needs=["invented_kind"], file=own)


def test_a_corrupt_recipes_file_is_unresolvable_and_is_never_overwritten(own):
    own.write_text("{this is not json")
    assert recipes.read_store(own).unresolvable is True
    with pytest.raises(Unresolvable):
        recipes.listed(own)
    with pytest.raises(Unresolvable):
        recipes.save_recipe("x", "x", "a request", file=own)
    assert own.read_text() == "{this is not json"
    assert recipes.read_store(own.parent / "nothing-here.json").absent is True


# ------------------------------------------------------------------ what needs a person
def test_approval_is_decided_from_the_allowlist_not_from_the_recipes_say_so():
    entries = {"smiles_to_3d": {"kind": "local", "approval": False, "enabled": True},
               "admet_standalone": {"kind": "compute", "approval": True, "enabled": True}}
    cheap = {"id": "r", "ask": "x", "uses": ["smiles_to_3d"], "needs": ["local"], "declared": True}
    assert recipes.approval_needed(cheap, entries=entries) == []
    dear = {"id": "r", "ask": "x", "uses": ["admet_standalone"], "needs": ["compute"], "declared": True}
    assert len(recipes.approval_needed(dear, entries=entries)) == 2


def test_approval_fails_closed_on_everything_it_cannot_settle(monkeypatch):
    entries = {"smiles_to_3d": {"kind": "local", "approval": False, "enabled": True}}
    r = {"id": "r", "ask": "x", "uses": ["smiles_to_3d"], "needs": ["local"], "declared": True}
    assert recipes.approval_needed(r, entries=entries) == []
    # free text a person added
    assert recipes.approval_needed(r, more="and also drop the table", entries=entries)
    # an entry the allowlist does not have
    assert recipes.approval_needed({**r, "uses": ["nobody_allowed_this"]}, entries=entries)
    # an entry switched off
    assert recipes.approval_needed(r, entries={"smiles_to_3d": {"kind": "local", "approval": False,
                                                                "enabled": False}})
    # a recipe that declares nothing
    assert recipes.approval_needed({"id": "q", "ask": "x", "declared": False})
    # an allowlist that cannot be read is not an empty allowlist
    monkeypatch.setattr(recipes.allowlist, "load", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    why = recipes.approval_needed(r)
    assert why and "allowlist could not be read" in why[0]


def test_the_built_in_sweeps_ask_a_person_and_the_read_only_ones_do_not():
    needs = {r["id"]: recipes.approval_needed(r) for r in recipes.builtin()}
    assert needs["admet_sweep"] and needs["rerun_compare"]
    assert needs["engine_changed"] == [] and needs["weekly_digest"] == [] and needs["due_diligence"] == []


# ------------------------------------------------------------------ a schedule approves nothing
def test_a_scheduled_recipe_that_needs_approval_produces_a_pending_record_and_no_call(store, tmp_path):
    run = Runner()
    s = sched(run, store)
    item = s.add("admet_sweep", every="day", at="09:00")
    s.set(item["id"], True)
    got = s.tick(now=item["next"] + 1)
    assert got["pending"] == [item["id"]] and got["fired"] == []
    assert run.calls == []                                     # nothing was run
    pend = s.pending()
    assert len(pend) == 1 and pend[0]["state"] == "PENDING_APPROVAL"
    assert pend[0]["recipe"] == "admet_sweep" and pend[0]["approved_by"] is None
    assert any("compute" in w or "approval" in w for w in pend[0]["why"])
    assert pend[0]["record"]["state"] in ("RECORDED", "UNSIGNED") and pend[0]["record"]["hash"]
    assert s.get(item["id"])["result"]["state"] == "PENDING_APPROVAL"


def test_no_path_out_of_tick_ever_passes_an_approval(store):
    run = Runner()
    s = sched(run, store)
    for rid in ("engine_changed", "weekly_digest", "due_diligence"):
        s.add(rid, every="day", at="09:00")
    now = max(x["next"] for x in s.list()) + 1
    got = s.tick(now=now)
    assert len(got["fired"]) == 3 and got["pending"] == []
    assert len(run.calls) == 3
    assert {c["approved_by"] for c in run.calls} == {None}
    assert {c["by"] for c in run.calls} == {"schedule"}


def test_only_a_person_can_approve_and_then_the_runner_gets_the_name(store):
    run = Runner()
    s = sched(run, store)
    item = s.add("admet_sweep", every="day", at="09:00")
    assert s.run_now(item["id"])["state"] == "PENDING_APPROVAL" and run.calls == []
    out = s.run_now(item["id"], approved_by="heinz")
    assert out["state"] == "RAN" and len(run.calls) == 1
    assert run.calls[0]["approved_by"] == "heinz" and run.calls[0]["by"] == "person"


def test_a_runner_that_says_approval_is_needed_is_pending_not_failed_and_is_not_retried(store):
    run = Runner(answer={"state": "REFUSED", "why": "a person must approve this call"})
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    got = s.tick(now=item["next"] + 1)
    assert got["pending"] == [item["id"]]
    assert len(run.calls) == 1                                  # called once, never retried
    assert s.pending()[0]["why"] == ["the runner reported that this needs a person's approval"]
    assert s.get(item["id"])["result"]["state"] == "PENDING_APPROVAL"


def test_free_text_added_to_a_schedule_makes_it_wait_for_a_person(store):
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", more="and push the fix", every="day", at="09:00")
    assert s.tick(now=item["next"] + 1)["pending"] == [item["id"]]
    assert run.calls == []


def test_a_runner_that_throws_is_recorded_and_does_not_stop_the_next_slot(store):
    run = Runner(boom=RuntimeError("the model is away"))
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    s.tick(now=item["next"] + 1)
    got = s.get(item["id"])
    assert got["result"]["state"] == "FAILED" and "away" in got["result"]["why"]
    assert got["next"] > item["next"]                           # the clock went on


# ------------------------------------------------------------------ a missed run fires once
def test_a_missed_run_fires_once_not_once_per_missed_day(store):
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    woke = item["next"] + 4 * 86400 + 600                       # the Mac slept through four slots
    got = s.tick(now=woke)
    assert got["fired"] == [item["id"]] and len(run.calls) == 1
    assert s.tick(now=woke)["fired"] == []                      # and the same moment does not fire it again
    assert len(run.calls) == 1
    assert s.get(item["id"])["next"] > woke


def test_two_ticks_at_the_same_moment_fire_once(store):
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    when = item["next"] + 1
    assert s.tick(now=when)["fired"] == [item["id"]]
    assert sched(run, store).tick(now=when)["fired"] == []      # a second reader of the same store
    assert len(run.calls) == 1


def test_the_next_slot_is_saved_before_anything_runs(store):
    """A runner that looks at the store while it runs must already see the slot taken, or a second process fires it."""
    seen = {}
    s = sched(lambda req: seen.setdefault("next", json.loads(store.read_text())["items"][0]["next"]), store)
    item = s.add("engine_changed", every="day", at="09:00")
    s.tick(now=item["next"] + 1)
    assert seen["next"] > item["next"]


def test_the_clock_honours_day_weekday_and_week():
    t = recipes.next_time("day", "09:00", after=time.mktime((2026, 11, 19, 10, 0, 0, 0, 0, -1)))
    assert time.localtime(t).tm_hour == 9 and time.localtime(t).tm_mday == 20
    wd = recipes.next_time("weekday", "09:00", after=time.mktime((2026, 11, 20, 10, 0, 0, 0, 0, -1)))
    assert time.localtime(wd).tm_wday < 5                        # Friday 10:00 -> Monday, not Saturday
    wk = recipes.next_time("week", "09:00", day=2, after=time.mktime((2026, 11, 19, 10, 0, 0, 0, 0, -1)))
    assert time.localtime(wk).tm_wday == 2


def test_a_schedule_is_refused_without_a_usable_clock_or_recipe(store):
    s = sched(Runner(), store)
    with pytest.raises(ValueError):
        s.add("engine_changed", every="fortnight")
    with pytest.raises(ValueError):
        s.add("engine_changed", at="25:00")
    with pytest.raises(ValueError):
        s.add("no_such_recipe")


def test_a_schedule_switched_off_does_not_fire(store):
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00", on=False)
    assert s.tick(now=item["next"] + 1)["fired"] == [] and run.calls == []
    s.set(item["id"], True)
    assert s.get(item["id"])["on"] is True


# ------------------------------------------------------------------ the off switch
def test_the_off_switch_holds(store, monkeypatch):
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    monkeypatch.setenv(recipes.OFF, "1")
    got = s.tick(now=item["next"] + 1)
    assert got == {"fired": [], "pending": [], "off": True, "error": None}
    assert run.calls == []
    assert s.start() is False and s.status()["off"] is True
    assert s.get(item["id"])["next"] == item["next"]             # the clock did not even move


def test_the_off_switch_is_read_at_call_time(store, monkeypatch):
    """Agtr reads AGTR_NO_SCHEDULES once at startup (agtrdesktop/service.py:85), so turning it off needs a restart."""
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    monkeypatch.setenv(recipes.OFF, "yes")
    assert s.tick(now=item["next"] + 1)["off"] is True
    monkeypatch.setenv(recipes.OFF, "0")                        # an explicit no is not "on"... it is off-the-off
    assert recipes.off() is False
    assert s.tick(now=item["next"] + 1)["fired"] == [item["id"]]


# ------------------------------------------------------------------ a corrupt store never reads as "no schedules"
def test_a_corrupt_store_is_unresolvable_and_never_reads_as_no_schedules(store):
    store.write_text('{"items": "not a list"}')
    s = sched(Runner(), store)
    assert s.read().unresolvable is True
    with pytest.raises(Unresolvable):
        s.list()
    with pytest.raises(Unresolvable):
        s.pending()


def test_a_corrupt_store_stops_the_tick_with_a_reason_instead_of_looking_idle(store):
    run = Runner()
    s = sched(run, store)
    store.write_text("{ broken")
    got = s.tick(now=time.time() + 10)
    assert got["fired"] == [] and got["pending"] == []
    assert got["error"] and "cannot be trusted" in got["error"]
    assert s.last_error == got["error"]
    st = s.status()
    assert st["store"]["state"] == "UNRESOLVABLE" and st["items"] is None and st["last_error"]
    assert run.calls == []
    assert store.read_text() == "{ broken"                      # and it was not overwritten


def test_a_task_with_an_unusable_clock_makes_the_store_unresolvable(store):
    store.write_text(json.dumps({"items": [{"id": "a", "recipe": "engine_changed", "every": "day", "at": "nine"}]}))
    s = sched(Runner(), store)
    assert s.read().unresolvable is True
    assert "clock" in s.read().detail


def test_no_store_yet_is_absent_not_an_error(store):
    s = sched(Runner(), store)
    assert s.read().absent is True and s.list() == [] and s.pending() == []
    assert s.status()["items"] == 0 and s.status()["last_error"] is None
    assert s.tick(now=time.time())["error"] is None


# ------------------------------------------------------------------ the thread
def test_one_thread_and_it_stops(store, monkeypatch):
    monkeypatch.setattr(recipes, "TICK", 0.05)
    run = Runner()
    s = sched(run, store)
    item = s.add("engine_changed", every="day", at="09:00")
    try:
        assert s.start() is True
        assert s.start() is False                               # one thread, not two
        assert s.status()["running"] is True
        for _ in range(100):
            if run.calls:
                break
            time.sleep(0.02)
    finally:
        s.stop()
    assert s.status()["running"] is False
    assert item["next"] > time.time() or run.calls              # it either had nothing due yet, or it ran it once
