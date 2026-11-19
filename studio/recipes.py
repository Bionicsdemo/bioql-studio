"""Recipes and scheduled work: the request that keeps being put off, in precise words, one call away — and on a clock.

A recipe is a named REQUEST (`ask`, written precisely enough that two people would run the same thing) plus what it
will reach: `uses`, the allowlist entries it calls by name, and `needs`, the kinds of entry it will touch. A schedule
runs one on a clock. Both stores live in `home()` at 0600 and are read with the three states, like any other external
fact.

Ported from Agtr's `agtrdesktop/recipes.py` and `schedule.py`, keeping what makes them safe and dropping the one
thing that would be unsafe here:

**Kept.** A run that was due while the Mac slept happens ONCE on waking, not once per missed day (schedule.py:123:
`next_time(..., after=now)` walks the clock forward past every missed slot in one step). The off switch
(`AGTR_NO_SCHEDULES`, read in agtrdesktop/service.py:85) becomes `BIOQL_STUDIO_NO_SCHEDULES`, read at CALL time
rather than once at startup, so turning schedules off does not need a restart to take effect.

**Dropped.** In Agtr a scheduled recipe is planned and STARTED without the person seeing the plan first —
`recipe_run` plans and builds in one step for the schedule (agtrdesktop/dev.py:1571-1583; recipes.py:3 says it
plainly: "he sees the plan (unless it was scheduled)"). Nothing merges without him, but the work runs. Here a
schedule approves NOTHING: `tick` passes `approved_by=None` on every path, and a recipe that would touch anything
needing a person is not called at all — it becomes a PENDING record carrying the reasons, on the signed notebook.
A person approves by hand, with `run_now(id, approved_by=...)`.

Approval is decided fail-closed, from BioQL's own allowlist rather than from a recipe's own say-so: an entry marked
for approval, a `compute` or `hardware` kind, a name the allowlist does not have, an allowlist that cannot be read,
free text added to the request, or a recipe that declares nothing — each of those is a reason a person is needed.
Declared `uses` is only the first layer: the model still chooses tools at run time, and the adapter refuses them
without an approval of their own. This is the first gate, not the only one.

    recipes.listed()                    the built-ins and the owner's own
    Schedules(run=...).add("admet_sweep", every="weekday", at="07:30")
    Schedules(run=...).tick()           {"fired": [...], "pending": [...], "off": False, "error": None}
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import allowlist, home, ledger, notebook, write_private
from .resolution import Resolution, absent, resolved, unresolvable

try:
    import fcntl
except ImportError:
    fcntl = None                         # type: ignore[assignment]

EVERY = ("day", "weekday", "week")
AT = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
TICK = 30                                # seconds between looks, as in Agtr
OFF = "BIOQL_STUDIO_NO_SCHEDULES"
PENDING = "PENDING_APPROVAL"
KEEP_PENDING = 200
RECIPES_SOURCE = "recipes.json"
SCHEDULES_SOURCE = "schedules.json"

# The kinds of allowlist entry a recipe may reach without a person: reading state, and computing on this Mac.
# Anything heavier asks, because `compute` and `hardware` are what spend GPU minutes, Modal credit or the ~153 s of
# IBM hardware time that is left.
FREE_KINDS = ("status", "local")


# ---------------------------------------------------------------- the built-in recipes, for THIS domain

BUILTIN: list[dict] = [
    {"id": "due_diligence", "title": "Due diligence report on a candidate",
     "ask": "Write a due-diligence report on the candidate I name. Take every claim about it from the signed "
            "notebook only: for each claim give the ledger line that produced it, the BioQL entry and arguments "
            "that were called, the verifier's verdict, and the provenance BioQL itself reported — is_real_hardware "
            "and the IBM job id, real or null, never inferred. State plainly what is NOT in the record: a claim "
            "with no line behind it goes in a section called 'not evidenced', not in the body. Do not call BioQL "
            "for anything new; do not estimate a number that is missing.",
     "uses": ["pipeline_job", "pipeline_job_result"], "needs": ["status"],
     "why": "The report a reader can check: every number traced to the run that produced it."},

    {"id": "admet_sweep", "title": "ADMET sweep of a candidate set",
     "ask": "Run the ADMET predictions over the candidate set I give you (one SMILES per line), in ONE batch call "
            "rather than one call per molecule, and report the per-molecule values with the model and version that "
            "produced them. Flag every molecule RDKit cannot sanitize and leave it out of the ranking rather than "
            "scoring it. Report the median and the top-N separately: they can point at different arms.",
     "uses": ["admet_standalone", "smiles_to_3d"], "needs": ["compute"],
     "why": "The sweep that decides which candidates go on. It spends compute, so a person starts it."},

    {"id": "rerun_compare", "title": "Re-run this campaign and compare",
     "ask": "Re-run the campaign I name with the SAME arguments, and compare the new result with the recorded one "
            "field by field. Match every free parameter before comparing — shots, exhaustiveness, basis, window — "
            "and say which parameters you matched. Report a difference as a difference; a re-run is never used as "
            "the baseline for itself. Say whether each arm's backend was the same (is_real_hardware and job id), "
            "because a simulator-versus-hardware difference is not a scientific one.",
     "uses": [], "needs": ["compute"],
     "why": "Reproduction, with the free parameters pinned first. It re-spends what the campaign spent."},

    {"id": "engine_changed", "title": "What changed in the engine since yesterday",
     "ask": "Using Studio's own no-change guard, compare the current fingerprint of BioQL's trees with the last "
            "snapshot and report every file added, removed or changed, with the time and the sha256. Say for each "
            "whether it is a file Studio could not have touched. State the guard's own blind spots in the report: "
            "it sees that a file changed and not who changed it, it skips runtime folders, and it fingerprints "
            "source extensions only, so a changed .pdb or .sdf is not seen. Do not change anything under BioQL.",
     "uses": [], "needs": [],
     "why": "The guard: proof that a Studio session left BioQL as it found it — and what moved if it did not."},

    {"id": "weekly_digest", "title": "Weekly findings digest",
     "ask": "Summarise the week from the signed notebook and the usage meter: every campaign run, its verdict, the "
            "findings the verifier raised, what each campaign cost (saying for each price whether it is a charge or "
            "a measure at API rates), and what is still pending a person's approval. Report a token or cost the "
            "engine did not report as not reported, never as zero. End with what did not happen: runs refused, "
            "schedules that stayed pending, and why.",
     "uses": [], "needs": [],
     "why": "What the week actually produced and cost, from the record rather than from memory."},
]


def _norm(r: dict, own: bool = False) -> dict:
    out = {"id": str(r.get("id") or "").strip(), "title": str(r.get("title") or "").strip(),
           "ask": str(r.get("ask") or "").strip(), "uses": [str(x) for x in (r.get("uses") or [])],
           "needs": [str(x) for x in (r.get("needs") or [])], "why": str(r.get("why") or "").strip(),
           "own": bool(own), "declared": "uses" in r or "needs" in r}
    if not ID.match(out["id"]):
        raise ValueError(f"a recipe id is lowercase letters, digits and underscores: {out['id']!r}")
    if not out["ask"]:
        raise ValueError(f"recipe {out['id']!r} has no request: a recipe IS the request, in precise words")
    bad = [k for k in out["needs"] if k not in allowlist.KINDS]
    if bad:
        raise ValueError(f"recipe {out['id']!r} names unknown kinds {bad}; they are {allowlist.KINDS}")
    return out


# ---------------------------------------------------------------- the owner's own recipes


def recipes_path() -> Path:
    return home() / RECIPES_SOURCE


def read_store(file: Path | None = None) -> Resolution:
    """RESOLVED the owner's recipes, ABSENT when there is no file (that IS information: none have been written),
    UNRESOLVABLE when a file exists and cannot be used. A corrupt file never reads as "no recipes"."""
    f = file or recipes_path()
    try:
        raw = f.read_bytes()
    except FileNotFoundError:
        return absent(RECIPES_SOURCE, f"no {f.name}: the owner has written no recipes of their own")
    except OSError as e:
        return unresolvable(RECIPES_SOURCE, f"cannot read {f.name}: {e}")
    try:
        store = json.loads(raw)
        if not isinstance(store, dict):
            raise ValueError("the recipes file is not an object")
        if not isinstance(store.setdefault("recipes", {}), dict):
            raise ValueError("'recipes' is not an object")
        for rid, r in store["recipes"].items():
            if not isinstance(r, dict):
                raise ValueError(f"recipe {rid!r} is not an object")
            got = _norm({**r, "id": rid}, own=True)
            store["recipes"][rid] = got
    except (ValueError, TypeError) as e:
        return unresolvable(RECIPES_SOURCE, f"{f.name} exists but cannot be trusted: {e}")
    store.setdefault("version", 1)
    return resolved(store, RECIPES_SOURCE, f"{len(store['recipes'])} recipe(s) of the owner's own")


def _store(file: Path | None = None) -> dict:
    """The store, or an empty one when there is none. Raises on UNRESOLVABLE."""
    return read_store(file).or_default({"version": 1, "recipes": {}})


def builtin() -> list[dict]:
    return [_norm(r) for r in BUILTIN]


def listed(file: Path | None = None) -> list[dict]:
    """The built-ins, then the owner's own (which may replace a built-in by using its id)."""
    out = {r["id"]: r for r in builtin()}
    out.update(_store(file)["recipes"])
    return [out[k] for k in sorted(out)]


def find(rid: str, file: Path | None = None) -> dict:
    hit = next((r for r in listed(file) if r["id"] == rid), None)
    if hit is None:
        raise ValueError(f"there is no recipe {rid!r}")
    return hit


def save_recipe(rid: str, title: str, ask: str, uses=(), needs=(), why: str = "",
                file: Path | None = None) -> dict:
    r = _norm({"id": rid, "title": title, "ask": ask, "uses": list(uses), "needs": list(needs), "why": why}, own=True)
    store = _store(file)                     # read first: a store that cannot be read is never overwritten
    store["recipes"][r["id"]] = r
    write_private(file or recipes_path(), json.dumps(store, indent=1, sort_keys=True))
    return r


def remove_recipe(rid: str, file: Path | None = None) -> bool:
    store = _store(file)
    if rid not in store["recipes"]:
        return False
    store["recipes"].pop(rid)
    write_private(file or recipes_path(), json.dumps(store, indent=1, sort_keys=True))
    return True


# ---------------------------------------------------------------- what needs a person


def approval_needed(recipe: dict, more: str = "", entries: dict | None = None) -> list[str]:
    """Why a person is needed before this recipe runs — an empty list means nothing here asks for one.

    Fail closed, every branch: an unknown entry name, an allowlist that cannot be read, a kind heavier than a local
    computation, free text added to the request, and a recipe that declares neither `uses` nor `needs`.
    """
    why: list[str] = []
    if (more or "").strip():
        why.append("the request carries free text a person added; nobody can say in advance what it will reach")
    if not recipe.get("declared", True) and not (recipe.get("uses") or recipe.get("needs")):
        why.append(f"recipe {recipe.get('id')!r} declares neither the entries it uses nor the kinds it needs")
    for kind in recipe.get("needs") or []:
        if kind not in FREE_KINDS:
            why.append(f"it needs a {kind!r} entry, which can spend GPU, cloud or real quantum hardware")
    names = list(recipe.get("uses") or [])
    if names:
        try:
            got = allowlist.load() if entries is None else entries
        except Exception as e:               # noqa: BLE001 - an allowlist nobody can read is not an empty one
            return [*why, f"the allowlist could not be read ({type(e).__name__}: {e}); nothing is assumed allowed"]
        for name in names:
            e = got.get(name)
            if e is None:
                why.append(f"{name!r} is not in the allowlist: an entry nobody approved is never assumed cheap")
                continue
            if not e.get("enabled", True):
                why.append(f"{name!r} is in the allowlist but switched off")
            if e.get("approval") or e.get("kind") not in FREE_KINDS:
                why.append(f"{name!r} is a {e.get('kind')} entry marked for approval")
    return why


def request_of(recipe: dict, more: str = "", by: str = "owner", approved_by: str | None = None,
               schedule: str | None = None) -> dict:
    """What the runner is handed. `approved_by` is carried, never invented: a schedule always passes None."""
    ask = recipe["ask"] + (("\n\n" + more.strip()) if (more or "").strip() else "")
    return {"recipe": recipe["id"], "title": recipe["title"], "ask": ask, "uses": list(recipe.get("uses") or []),
            "needs": list(recipe.get("needs") or []), "more": (more or "").strip(), "by": by,
            "approved_by": approved_by, "schedule": schedule, "t": round(time.time(), 3)}


# ---------------------------------------------------------------- schedules


def off() -> bool:
    """Read at call time, not once at startup as Agtr does (agtrdesktop/service.py:85): turning schedules off takes
    effect on the next tick instead of on the next restart."""
    return (os.environ.get(OFF) or "").strip().lower() not in ("", "0", "false", "no", "off")


def next_time(every: str, at: str, day: int = 0, after: float | None = None) -> float:
    """The next moment (local time) this schedule is due, strictly after `after`.

    This is what makes a missed run fire ONCE: when the Mac wakes after three missed days, `tick` asks for the next
    slot after NOW, which walks past all three in one step (Agtr's schedule.py:123).
    """
    after = time.time() if after is None else after
    h, m = (int(x) for x in at.split(":"))
    cand = datetime.fromtimestamp(after).replace(hour=h, minute=m, second=0, microsecond=0)
    for _ in range(9):
        if cand.timestamp() > after and (every == "day"
                                         or (every == "weekday" and cand.weekday() < 5)
                                         or (every == "week" and cand.weekday() == day)):
            return cand.timestamp()
        cand = (cand + timedelta(days=1)).replace(hour=h, minute=m)
    return cand.timestamp()


class Schedules:
    """A recipe at a time, daily / weekdays / weekly. One thread, a 30 s tick, and a runner that is INJECTED: this
    class never reaches a model or BioQL itself, which is also why it is testable without either.

    The store is re-read on every operation (Agtr caches it in `__init__`, schedule.py:43, so a second process's
    change is invisible and the next `_save` overwrites it), and it is never written unless it was read: a store
    that cannot be parsed stops the tick with a reason, and does not read as "no schedules" — that would silently
    stop all scheduled work, which is the degradation the three-state invariant exists to prevent.
    """

    def __init__(self, run: Callable[[dict], Any], file: Path | None = None,
                 recipes_file: Path | None = None) -> None:
        self.run = run
        self.file = file or (home() / SCHEDULES_SOURCE)
        self.recipes_file = recipes_file
        self.last_error: str | None = None
        self.last_tick: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- the store

    def read(self) -> Resolution:
        """RESOLVED the store, ABSENT when there is no file, UNRESOLVABLE when one exists and cannot be used."""
        f = self.file
        try:
            raw = f.read_bytes()
        except FileNotFoundError:
            return absent(SCHEDULES_SOURCE, f"no {f.name}: nothing is scheduled")
        except OSError as e:
            return unresolvable(SCHEDULES_SOURCE, f"cannot read {f.name}: {e}")
        try:
            store = json.loads(raw)
            if not isinstance(store, dict):
                raise ValueError("the schedules file is not an object")
            if not isinstance(store.setdefault("items", []), list):
                raise ValueError("'items' is not a list")
            if not isinstance(store.setdefault("pending", []), list):
                raise ValueError("'pending' is not a list")
            for x in store["items"]:
                if not isinstance(x, dict) or not x.get("id"):
                    raise ValueError("a scheduled task is not an object with an id")
                if x.get("every") not in EVERY or not AT.match(str(x.get("at", ""))):
                    raise ValueError(f"scheduled task {x.get('id')!r} has no usable clock")
        except (ValueError, TypeError) as e:
            return unresolvable(SCHEDULES_SOURCE, f"{f.name} exists but cannot be trusted: {e}")
        store.setdefault("version", 1)
        return resolved(store, SCHEDULES_SOURCE, f"{len(store['items'])} scheduled task(s)")

    def _read(self) -> dict:
        return self.read().or_default({"version": 1, "items": [], "pending": []})

    def _save(self, store: dict) -> None:
        write_private(self.file, json.dumps(store, indent=1, ensure_ascii=False))

    @contextmanager
    def _locked(self):
        """An flock on a lock file beside the store, so two PROCESSES cannot both decide the same slot is due and
        fire it twice. The lock is a sidecar because `_save` replaces the store's inode."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(self.file.with_name(self.file.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    except OSError as why:
                        if why.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL):
                            raise
                yield
            finally:
                os.close(fd)

    # -- the schedules themselves

    def list(self) -> list[dict]:
        """The scheduled tasks. Raises on an UNRESOLVABLE store: never an empty list, which would read as "nothing
        is scheduled" and quietly stop every scheduled run."""
        return [dict(x) for x in self._read()["items"]]

    def get(self, sid: str) -> dict:
        x = next((x for x in self._read()["items"] if x["id"] == sid), None)
        if x is None:
            raise ValueError(f"there is no scheduled task {sid!r}")
        return dict(x)

    def pending(self) -> list[dict]:
        """What is waiting for a person, newest first."""
        return sorted(self._read()["pending"], key=lambda p: -float(p.get("t") or 0))

    def add(self, recipe: str, more: str = "", every: str = "week", at: str = "09:00", day: int = 0,
            by: str = "", on: bool = True) -> dict:
        r = find(recipe, self.recipes_file)              # a schedule for a recipe nobody has is refused here
        if every not in EVERY:
            raise ValueError(f"every: one of {EVERY}")
        if not AT.match(str(at)):
            raise ValueError("at: HH:MM, 24-hour")
        day = int(day or 0) % 7
        item = {"id": secrets.token_hex(4), "recipe": r["id"], "title": r["title"], "more": (more or "").strip()[:4000],
                "every": every, "at": at, "day": day, "on": bool(on), "by": by,
                "created": round(time.time(), 3), "last": None, "result": None,
                "next": next_time(every, at, day)}
        with self._locked():
            store = self._read()
            store["items"].append(item)
            self._save(store)
        return dict(item)

    def set(self, sid: str, on: bool) -> dict:
        with self._locked():
            store = self._read()
            x = next((x for x in store["items"] if x["id"] == sid), None)
            if x is None:
                raise ValueError(f"there is no scheduled task {sid!r}")
            x["on"] = bool(on)
            if x["on"]:
                x["next"] = next_time(x["every"], x["at"], x.get("day", 0))
            self._save(store)
            return dict(x)

    def remove(self, sid: str) -> bool:
        with self._locked():
            store = self._read()
            if not any(x["id"] == sid for x in store["items"]):
                return False
            store["items"] = [x for x in store["items"] if x["id"] != sid]
            self._save(store)
        return True

    # -- running

    def tick(self, now: float | None = None) -> dict:
        """Fire what is due. {fired, pending, off, error}.

        Nothing here approves anything: `approved_by` is None on every path out of this method. A task whose recipe
        would touch something needing a person becomes a pending record instead of a call.

        `next` is advanced and SAVED before anything runs, inside the cross-process lock, so a second process that
        ticks at the same moment finds the slot already taken.
        """
        out: dict = {"fired": [], "pending": [], "off": False, "error": None}
        if off():
            return {**out, "off": True, "error": None}
        now = time.time() if now is None else now
        self.last_tick = now
        try:
            with self._locked():
                store = self._read()
                due = []
                for x in store["items"]:
                    nxt = x.get("next")
                    if x.get("on") and isinstance(nxt, (int, float)) and nxt <= now:
                        # Once, however many slots were missed while the Mac slept (Agtr's rule, schedule.py:123).
                        x["next"] = next_time(x["every"], x["at"], x.get("day", 0), after=now)
                        x["last"] = now
                        due.append(dict(x))
                if due:
                    self._save(store)
        except Exception as e:                # noqa: BLE001 - reported, never swallowed as "nothing to do"
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            return {**out, "error": self.last_error}
        self.last_error = None
        for x in due:
            got = self._fire(x, approved_by=None, by="schedule")
            (out["pending"] if got.get("state") == PENDING else out["fired"]).append(x["id"])
        return out

    def run_now(self, sid: str, approved_by: str | None = None) -> dict:
        """By hand. A person may approve here — that is the only way an approval ever reaches the runner."""
        x = self.get(sid)
        return self._fire(x, approved_by=(approved_by or "").strip() or None, by="person")

    def _fire(self, x: dict, approved_by: str | None, by: str) -> dict:
        try:
            r = find(x["recipe"], self.recipes_file)
            why = approval_needed(r, x.get("more") or "")
        except Exception as e:                # noqa: BLE001 - an unreadable recipe or allowlist asks a person too
            r, why = None, [f"the recipe could not be prepared ({type(e).__name__}: {e})"[:300]]
        if why and not approved_by:
            return self._pend(x, why, r)
        req = request_of(r, x.get("more") or "", by=by, approved_by=approved_by, schedule=x["id"]) if r else {}
        try:
            said = self.run(req)
        except Exception as e:                # noqa: BLE001 - a runner that throws does not stop the next slot
            return self._result(x, {"state": "FAILED", "why": f"{type(e).__name__}: {e}"[:300]})
        if _asks_for_approval(said):
            # The runner itself says a person is needed. That is pending, not failed, and it is never retried
            # automatically: a retry is how a refusal turns into a run nobody approved.
            return self._pend(x, ["the runner reported that this needs a person's approval"], r, said=said)
        return self._result(x, {"state": "RAN", "by": by, "approved_by": approved_by, "said": _small(said)})

    def _pend(self, x: dict, why: list[str], r: dict | None, said: Any = None) -> dict:
        rec = {"id": secrets.token_hex(4), "t": round(time.time(), 3), "state": PENDING, "schedule": x.get("id"),
               "recipe": x.get("recipe"), "title": x.get("title") or (r or {}).get("title", ""),
               "more": x.get("more") or "", "why": why, "approved_by": None,
               "ask": (r or {}).get("ask", "")[:4000], "said": _small(said)}
        rec["record"] = _record({"tool": "schedule_pending", "state": PENDING, "detail": "; ".join(why)[:300],
                                 "recipe": rec["recipe"], "schedule": rec["schedule"], "pending": rec["id"],
                                 "by": "schedule", "approved_by": None})
        with self._locked():
            store = self._read()
            store["pending"] = ([rec] + list(store["pending"]))[:KEEP_PENDING]
            y = next((y for y in store["items"] if y["id"] == x.get("id")), None)
            if y is not None:
                y["result"] = {"state": PENDING, "pending": rec["id"], "why": why}
            self._save(store)
        return rec

    def _result(self, x: dict, result: dict) -> dict:
        with self._locked():
            store = self._read()
            y = next((y for y in store["items"] if y["id"] == x.get("id")), None)
            if y is not None:
                y["result"] = result
                self._save(store)
        return result

    # -- the thread

    def status(self) -> dict:
        """What a person (or a UI) asks when nothing seems to be running. The store's state is part of the answer:
        an UNRESOLVABLE store is why nothing fired, and it says so instead of looking idle."""
        res = self.read()
        return {"off": off(), "running": bool(self._thread and self._thread.is_alive()),
                "tick_seconds": TICK, "last_tick": self.last_tick, "last_error": self.last_error,
                "store": {"state": res.state, "detail": res.detail},
                # 0 when there is no store (ABSENT: nothing is scheduled, which IS the answer); None only when the
                # store could not be read, so "none scheduled" and "cannot tell" never look alike.
                "items": len(res.value["items"]) if res.resolved else (0 if res.absent else None)}

    def start(self) -> bool:
        """One thread, looking every 30 s. The runs happen IN it, one after another, rather than in a thread each
        (Agtr starts one per due task, schedule.py:128): one clock, one runner, no two runs racing each other."""
        if off() or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:        # noqa: BLE001 - the loop survives, and says why it stumbled
                    self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._stop.wait(TICK)

        self._thread = threading.Thread(target=loop, daemon=True, name="studio-schedules")
        self._thread.start()
        return True

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=wait)


def _asks_for_approval(said: Any) -> bool:
    if not isinstance(said, dict):
        return False
    for key in ("needs_approval", "approval_required", "pending_approval"):
        if said.get(key) is True:
            return True
    return str(said.get("state") or "").upper() in (PENDING, "REFUSED") and "approv" in \
        f"{said.get('why', '')} {said.get('detail', '')}".lower()


def _small(said: Any) -> Any:
    """What of the runner's answer goes in the store: a shape, not a payload."""
    if said is None:
        return None
    if isinstance(said, dict):
        return {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300])
                for k, v in list(said.items())[:20]}
    return str(said)[:300]


def _record(row: dict) -> dict:
    """On the signed notebook when it can sign, on the hash-chained ledger with the reason when it cannot — the same
    fallback `adapter._record` uses. A pending record that could not be written is said, never dropped."""
    try:
        return {"state": "RECORDED", "hash": notebook.record(row).get("hash")}
    except Exception as e:                    # noqa: BLE001 - no crypto, no key, a broken key: all land here
        try:
            got = ledger.append({**row, "unsigned_because": f"{type(e).__name__}: {e}"[:160]})
            return {"state": "UNSIGNED", "hash": got.get("hash"), "why": f"{type(e).__name__}: {e}"[:160]}
        except Exception as e2:               # noqa: BLE001 - the record is kept in the store either way
            return {"state": "UNRESOLVABLE", "hash": None, "why": f"{type(e2).__name__}: {e2}"[:160]}
