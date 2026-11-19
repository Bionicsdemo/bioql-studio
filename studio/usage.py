"""The usage meter: what the model cost, counted as it happens — one JSON line per model call.

Studio has one brain, Claude Code, and this is the only place that says what it spent. Two rules decide everything
here, and both are the invariant of CLAUDE.md (2026-11-18) applied to a number instead of to a fact:

**Nothing is estimated.** A price appears on a line only when the engine itself reported one. Claude Code reports
what the tokens WOULD cost at API rates: on a subscription plan that is a MEASURE, not a charge, and the line says
so in `cost_basis` (`measure_at_api_rates`) instead of leaving a reader to assume a bill. With an API key the same
number IS the bill (`charged_to_api_key`). No price, no number: `cost` is null and `cost_basis` is null. There is no
price table in this module, by design — a rate card here would turn a missing number into an invented one.

**A count the engine did not report is not zero.** Zero is a measurement: it says the engine answered and charged
nothing. Not-reported is the third state, and it stays distinguishable all the way through the totals: every total
carries `<field>_not_reported`, the number of calls that did not report it, and a total no call reported at all is
null rather than 0.

    usage.add("claude-code", used, kind="campaign", campaign="c-7", tool="admet_standalone")
    usage.summary()   -> {"today": {...}, "week": {...}, "month": {...}, "all": {...}, "recent": [...], ...}
    usage.for_campaign("c-7")   -> what that campaign cost, which is the question this meter exists to answer

Ported from Agtr's `agtros/meter.py`, with its folding defect deliberately left behind. There, the folded "earlier"
line is stamped with the LAST old row's time (meter.py:93), so rows from months ago can be counted in today, this
week and this month; which rows get folded is decided by POSITION in the file (`rows[:-KEEP // 2]`, meter.py:90)
while `earlier()` appends older rows at the END (meter.py:84), so the oldest rows survive and the newest are folded
away. Here folding is decided by TIME and nothing newer than the widest window summary reports can ever be folded;
the folded line is stamped with the OLDEST row it covers and carries `t_from`/`t_to`; and a folded line is counted
in a window only when its whole span is inside it. The size cap is what loses: a file of nothing but recent rows
grows rather than fold a row that today's total needs.
"""
from __future__ import annotations

import errno
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import home, write_private
from .resolution import Resolution, absent, resolved, unresolvable

try:
    import fcntl
except ImportError:                      # not POSIX: the in-process lock is all there is
    fcntl = None                         # type: ignore[assignment]

SOURCE = "usage.jsonl"
FIELDS = ("in", "out", "cache", "turns")          # counts; `cost` is handled apart because it is a price
KEEP = 20000                                      # rows kept unfolded, when there are old enough rows to fold
WIDEST = 30 * 86400                               # the widest window `summary` reports: nothing newer is ever folded

# How a call is paid for. Unknown stays unknown: Agtr's `billed()` (meter.py:24) returns "key" for any engine it does
# not recognise, which invents a bill for an engine nobody classified.
ON_PLAN = {"claude-code": "Claude plan"}
ON_KEY = {"claude-api": "Anthropic API key", "anthropic": "Anthropic API key"}
ON_LOCAL = {"ollama": "on this Mac", "vmlx": "on this Mac"}
BILLED = ("plan", "key", "local")

MEASURE = "measure_at_api_rates"                  # a plan: the price is what the tokens would cost, not a charge
CHARGE = "charged_to_api_key"
BASIS_UNKNOWN = "basis_unknown"                   # a price arrived and nobody said how this engine is paid for
COST_NOTE = ("a price is recorded only when the engine reported one. On a subscription plan Claude Code reports what "
             "the tokens would cost at API rates: that is a measure, not a charge (cost_basis=measure_at_api_rates). "
             "With an API key the same number is the bill (charged_to_api_key), and a price from an engine nobody "
             "classified is basis_unknown rather than either. Nothing here is estimated, and a call that reported "
             "no price has cost null, never 0.")

_LOCK = threading.RLock()


def path_file() -> Path:
    return home() / "usage.jsonl"


def _lock_file(f: Path) -> Path:
    """A lock beside the ledger, not the ledger itself: `fold` replaces the file, and an flock held on the old inode
    would not keep a concurrent append out of the rewritten one."""
    return f.with_name(f.name + ".lock")


@contextmanager
def _locked(f: Path):
    f.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd = os.open(_lock_file(f), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as why:               # a filesystem without locks: the in-process lock still holds
                    if why.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL):
                        raise
            yield
        finally:
            os.close(fd)


def billed(engine: str, given: str | None = None) -> str | None:
    """How this call is paid for, or None when nobody said. An explicit `given` from the caller wins: who is on a
    plan and who is on a key is a fact about the account, not about the engine's name."""
    if given:
        if given not in BILLED:
            raise ValueError(f"billed must be one of {BILLED} (or None when unknown), not {given!r}")
        return given
    e = (engine or "").strip().lower()
    return "plan" if e in ON_PLAN else "key" if e in ON_KEY else "local" if e in ON_LOCAL else None


def _count(d: dict, *keys: str) -> int | None:
    """A whole, non-negative count, or None when the engine did not report one.

    Agtr reads these as `int(u.get("input_tokens") or 0)` (meter.py:34-38), so a missing key, a null, a 0, a False
    and a malformed value all become the same 0 — the third state folded into a measurement. Here presence is
    tested, and a value that is not a whole count (a bool, a float with a fraction, a negative, a string) is
    not-reported rather than a number nobody reported.
    """
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int) or (isinstance(v, float) and float(v).is_integer()):
            if v >= 0:
                return int(v)
    return None


def _sum_present(d: dict, *keys: str) -> int | None:
    """The sum of the keys that are there, or None when none of them is. Cache is reported under several names and a
    call may report one and not another; summing only what was reported keeps the zero honest."""
    parts = [_count(d, k) for k in keys if k in d]
    got = [p for p in parts if p is not None]
    return sum(got) if got else None


def _price(d: dict, *keys: str) -> float | None:
    for k in keys:
        if k not in d or isinstance(d[k], bool) or d[k] is None:
            continue
        try:
            v = float(d[k])
        except (TypeError, ValueError):
            continue
        if v >= 0:
            return round(v, 6)
    return None


def tokens(used: Any) -> dict:
    """What the engine said it used: {in, out, cache, turns, cost, model, not_reported}. Each count is an int or
    None; never a 0 that no one measured."""
    if not isinstance(used, dict):
        # Not a shape we can read is not a call that used nothing: every field is not-reported.
        out = {f: None for f in FIELDS}
        return {**out, "cost": None, "model": None, "not_reported": list(FIELDS) + ["cost"]}
    u = used.get("usage") if isinstance(used.get("usage"), dict) else used
    got = {
        "in": _count(u, "input_tokens", "prompt_tokens"),
        "out": _count(u, "output_tokens", "completion_tokens"),
        "cache": _sum_present(u, "cache_read_input_tokens", "cache_creation_input_tokens", "cached_input_tokens"),
        "turns": _count(used, "num_turns", "turns"),
        "cost": _price(used, "total_cost_usd", "cost_usd"),
    }
    model = used.get("model") or (u.get("model") if isinstance(u, dict) else None)
    return {**got, "model": str(model)[:120] if isinstance(model, str) and model else None,
            "not_reported": [k for k, v in got.items() if v is None]}


def add(engine: str, used: Any, kind: str = "ask", campaign: str = "", tool: str = "", model: str = "",
        billed_as: str | None = None, calls: int = 1, file: Path | None = None) -> dict:
    """One line for one model call. Returns the line as written.

    A call whose counts the engine did not report is still RECORDED, as not-reported. Agtr drops it
    (`if not (i or o or c or cost): return`, meter.py:51), so a call that reported nothing reads back as a call that
    never happened — and a write that fails is swallowed there too (meter.py:64-65), so the ledger can stop
    recording in silence. Here an OSError is raised: a meter that cannot write must say so.
    """
    got = tokens(used)
    row = {"t": round(time.time(), 3), "engine": (engine or "").strip() or None,
           "billed": billed(engine, billed_as), "kind": (kind or "ask").strip(),
           "campaign": (campaign or "").strip(), "tool": (tool or "").strip(),
           "in": got["in"], "out": got["out"], "cache": got["cache"], "cost": got["cost"], "turns": got["turns"],
           "model": (model or "").strip() or got["model"], "calls": max(1, int(calls)),
           "not_reported": got["not_reported"]}
    # Three states here too: no price, a price whose basis is known, and a price from an engine nobody has
    # classified. The third is not a measure — calling it one would state on the record that this number is not a
    # charge, which is exactly what nobody knows.
    row["cost_basis"] = None if row["cost"] is None else \
        {"key": CHARGE, "plan": MEASURE, "local": MEASURE}.get(row["billed"] or "", BASIS_UNKNOWN)
    f = file or path_file()
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with _locked(f):
        # A write cut short leaves a line with no newline. Appending straight after it would glue this call to the
        # fragment and lose BOTH: the damaged bytes stay (and stay counted as skipped), this row starts its own line.
        torn = f.exists() and f.stat().st_size > 0 and f.open("rb").read()[-1:] != b"\n"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(("\n" if torn else "") + line)
        big = f.stat().st_size > KEEP * 400
    if big:
        fold(file=f)                     # folds only what is older than every window; often that is nothing
    return row


# ---------------------------------------------------------------- reading


def read_rows(file: Path | None = None) -> Resolution:
    """RESOLVED the rows, ABSENT when nothing has been metered yet, UNRESOLVABLE when the file cannot be read.

    A line that does not parse is counted in `meta["skipped"]` rather than dropped in silence: a torn last line is
    what a crash during an append leaves, and a summary that quietly loses rows understates a cost.
    """
    f = file or path_file()
    try:
        raw = f.read_bytes()
    except FileNotFoundError:
        return absent(SOURCE, f"no {f.name} yet: no model call has been metered")
    except OSError as e:
        return unresolvable(SOURCE, f"cannot read {f.name}: {e}")
    rows, skipped = [], 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(r, dict):
            rows.append(r)
        else:
            skipped += 1
    if not rows and not skipped:
        return absent(SOURCE, f"{f.name} is empty: no model call has been metered")
    return resolved(rows, SOURCE, f"{len(rows)} row(s)", skipped=skipped)


def rows(file: Path | None = None) -> list[dict]:
    """The rows, or [] when there are none. Raises on UNRESOLVABLE: a meter that cannot be read is not a meter that
    counted nothing."""
    return read_rows(file).or_default([])


def _miss(r: dict, field: str) -> int:
    """How many calls in this row did not report `field`. A plain row is one call; a folded row carries its count."""
    got = (r.get("missing") or {}).get(field)
    if isinstance(got, int) and got >= 0:
        return got
    v = r.get(field)
    reported = isinstance(v, (int, float)) and not isinstance(v, bool)
    return 0 if reported else max(1, int(r.get("calls") or 1))


def _field(rows_: list[dict], field: str) -> tuple[Any, int]:
    """(the total of what was reported — None when nothing was — and how many calls did not report it)."""
    total, seen, miss = 0.0, False, 0
    for r in rows_:
        v = r.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total, seen = total + v, True
        miss += _miss(r, field)
    if not seen:
        return None, miss
    return (round(total, 6) if field == "cost" else int(total)), miss


def _priced(rows_: list[dict]) -> int:
    """How many calls came with a price above zero. A folded line carries its own count."""
    n = 0
    for r in rows_:
        if isinstance(r.get("priced"), int):
            n += int(r["priced"])
            continue
        c = r.get("cost")
        if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
            n += max(1, int(r.get("calls") or 1))
    return n


def add_up(rows_: list[dict]) -> dict:
    """Totals and breakdowns for a set of rows. A field no call reported is null, not 0, and every field says how
    many calls did not report it."""
    out: dict = add_up_flat(rows_)

    def by(key: str) -> dict:
        keys = sorted({(r.get(key) or "unknown") for r in rows_})
        return {k: add_up_flat([r for r in rows_ if (r.get(key) or "unknown") == k]) for k in keys}

    out["engines"] = by("engine")
    out["kinds"] = by("kind")
    out["billed"] = by("billed")
    out["campaigns"] = {k: v for k, v in by("campaign").items() if k != "unknown"}
    return out


def add_up_flat(rows_: list[dict]) -> dict:
    """The totals without the breakdowns, which is also what goes inside one."""
    out: dict = {"calls": sum(max(1, int(r.get("calls") or 1)) for r in rows_)}
    for field in (*FIELDS, "cost"):
        total, miss = _field(rows_, field)
        out[field] = total
        out[f"{field}_not_reported"] = miss
    out["priced"] = _priced(rows_)
    return out


def _since(now: float) -> dict:
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return {"today": midnight, "week": now - 7 * 86400, "month": now - WIDEST, "all": 0.0}


def _in_window(r: dict, since: float) -> bool:
    """A row belongs to a window when its whole span does. A folded row carries `t_from`/`t_to`; its `t` IS its
    oldest moment, which is what keeps Agtr's defect out: there the folded line wore the NEWEST old row's time
    (meter.py:93), so months of usage could be counted as spent today."""
    t = r.get("t_from", r.get("t"))
    try:
        return float(t or 0) >= since
    except (TypeError, ValueError):
        return False


def summary(now: float | None = None, file: Path | None = None) -> dict:
    """today / week / month / all, each with totals and breakdowns by engine, kind and campaign, plus the last 40
    rows. `cost_note` travels with it: a number here is never an estimate."""
    res = read_rows(file)
    all_rows = res.or_default([])
    now = time.time() if now is None else now
    out: dict = {name: add_up([r for r in all_rows if _in_window(r, since)]) for name, since in _since(now).items()}
    out["recent"] = sorted((r for r in all_rows if not r.get("folded")),
                           key=lambda r: -float(r.get("t") or 0))[:40]
    out["folded"] = [r for r in all_rows if r.get("folded")]
    out["cost_note"] = COST_NOTE
    out["source"] = {"state": res.state, "detail": res.detail, "skipped": res.meta.get("skipped", 0)}
    return out


def for_campaign(campaign: str, file: Path | None = None) -> dict:
    """What one campaign cost — the question this meter exists to answer.

    `calls` is the number of model calls, not the number of lines: a folded line stands for many, and a count of
    lines would make a campaign look cheaper after a fold than before it."""
    want = (campaign or "").strip()
    res = read_rows(file)
    sel = [r for r in res.or_default([]) if (r.get("campaign") or "") == want]
    totals = add_up(sel)
    return {"campaign": want, "calls": totals["calls"], "lines": len(sel), "totals": totals, "rows": sel,
            "cost_note": COST_NOTE,
            "source": {"state": res.state, "detail": res.detail, "skipped": res.meta.get("skipped", 0)}}


def campaigns(file: Path | None = None) -> list[str]:
    return sorted({(r.get("campaign") or "") for r in rows(file)} - {""})


# ---------------------------------------------------------------- folding


def _group(r: dict) -> tuple:
    """What a folded line may merge. Folding across engine, billed, kind, campaign or model would shrink the
    breakdowns `summary` reports, so each stays its own line."""
    return (r.get("engine"), r.get("billed"), r.get("kind"), r.get("campaign") or "", r.get("model"),
            r.get("cost_basis"))


def fold(now: float | None = None, file: Path | None = None) -> dict:
    """Sum rows older than the widest window `summary` reports into one line per (engine, billed, kind, campaign,
    model), and leave everything newer alone.

    What it guarantees, and what Agtr's `_fold` did not: a folded line can never be counted in today, this week or
    this month. Only rows strictly older than `now - WIDEST` are eligible; the line is stamped with the OLDEST
    moment it covers and carries `t_from`/`t_to` and the number of calls and of not-reported counts behind each
    total. So `summary` and `for_campaign` give the same answer before and after a fold, in every window and in
    every breakdown.

    The size cap loses on purpose: when the file is over KEEP lines but every row is recent, nothing is folded. A
    row today's total needs is never summed away to make a file smaller.
    """
    f = file or path_file()
    now = time.time() if now is None else now
    cutoff = now - WIDEST
    with _locked(f):
        res = read_rows(f)
        if res.absent:
            return {"folded": 0, "into": 0, "cutoff": cutoff, "why": res.detail}
        all_rows = res.value                     # UNRESOLVABLE raises: a fold never rewrites a file it cannot read
        if res.meta.get("skipped"):
            # Unparsable lines cannot be summed, and a rewrite would delete them. Folding stops instead.
            return {"folded": 0, "into": 0, "cutoff": cutoff,
                    "why": f"{res.meta['skipped']} line(s) do not parse: not rewriting a file with damage in it"}
        old, keep = [], []
        for r in all_rows:
            # A row is eligible only when its WHOLE span is older than the cutoff. A folded line that already
            # reaches into a window is left as it is rather than merged further.
            try:
                newest = max(float(r.get("t") or 0), float(r.get("t_to") or 0))
            except (TypeError, ValueError):
                newest = 0.0
            (old if 0 < newest < cutoff else keep).append(r)
        if not old:
            return {"folded": 0, "into": 0, "cutoff": cutoff, "why": "nothing is older than the widest window"}
        groups: dict[tuple, list[dict]] = {}
        for r in old:
            groups.setdefault(_group(r), []).append(r)
        lines = []
        for key, sel in sorted(groups.items(), key=lambda kv: min(float(r.get("t") or 0) for r in kv[1])):
            engine, billed_as, kind, campaign, model, basis = key
            stamps = [float(r.get("t_from", r.get("t")) or 0) for r in sel] + \
                     [float(r.get("t_to", r.get("t")) or 0) for r in sel]
            line = {"t": min(stamps), "t_from": min(stamps), "t_to": max(stamps), "folded": True,
                    "rows": len(sel), "engine": engine, "billed": billed_as, "kind": kind, "campaign": campaign,
                    "tool": "", "model": model, "cost_basis": basis,
                    "calls": sum(max(1, int(r.get("calls") or 1)) for r in sel), "missing": {}}
            for field in (*FIELDS, "cost"):
                total, miss = _field(sel, field)
                line[field] = total
                line["missing"][field] = miss
            line["priced"] = _priced(sel)
            lines.append(line)
        kept = sorted(lines + keep, key=lambda r: float(r.get("t") or 0))
        write_private(f, "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in kept))
    return {"folded": len(old), "into": len(lines), "cutoff": cutoff, "why": None}
