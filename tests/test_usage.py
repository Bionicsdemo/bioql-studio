"""What the meter must never do: invent a number, lose a call, or let an old row be counted as today's.

Agtr's meter is the ported original, and three of its behaviours are reproduced here as the thing that must NOT
happen: a missing count read as zero (meter.py:34-38), a call dropped because it reported nothing (meter.py:51),
and a folded line wearing the newest old row's time while folding is decided by file position (meter.py:90, :93).
"""
import json
import time

import pytest

from studio import usage
from studio.resolution import Unresolvable

CLAUDE = {"usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 7},
          "total_cost_usd": 0.0123, "num_turns": 3, "model": "claude-opus-5"}


@pytest.fixture
def f(tmp_path):
    return tmp_path / "usage.jsonl"


def lines(f):
    return [json.loads(x) for x in f.read_text().splitlines()]


# ------------------------------------------------------------------ nothing is estimated
def test_a_price_appears_only_when_the_engine_reported_one(f):
    usage.add("claude-code", {"usage": {"input_tokens": 10, "output_tokens": 2}}, file=f)
    row = lines(f)[0]
    assert row["cost"] is None and row["cost_basis"] is None
    assert row["in"] == 10 and row["out"] == 2


def test_a_reported_price_on_a_plan_is_a_measure_not_a_charge(f):
    row = usage.add("claude-code", CLAUDE, file=f)
    assert row["billed"] == "plan"
    assert row["cost"] == 0.0123
    assert row["cost_basis"] == usage.MEASURE == "measure_at_api_rates"
    assert "not a charge" in usage.COST_NOTE and "estimated" in usage.COST_NOTE
    assert "measure" in usage.summary(file=f)["cost_note"]


def test_the_same_number_with_an_api_key_is_a_charge(f):
    row = usage.add("claude-api", CLAUDE, file=f)
    assert row["billed"] == "key" and row["cost_basis"] == usage.CHARGE


def test_nothing_in_the_module_prices_tokens(f):
    """There is no rate card here on purpose: a rate would turn a missing number into an invented one."""
    src = (__import__("pathlib").Path(usage.__file__)).read_text()
    assert "per_million" not in src and "price_per" not in src
    # and a call with tokens but no reported price totals to no cost at all
    usage.add("claude-code", {"usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}}, file=f)
    assert usage.summary(file=f)["all"]["cost"] is None


def test_an_unknown_engine_is_not_billed_to_a_key():
    """Agtr's billed() (meter.py:24) answers "key" for anything it does not know, inventing a bill."""
    assert usage.billed("some-new-engine") is None
    assert usage.billed("claude-code") == "plan"
    assert usage.billed("some-new-engine", "plan") == "plan"
    with pytest.raises(ValueError):
        usage.billed("x", "invented")


# ------------------------------------------------------------------ three states, not two
def test_a_missing_token_count_is_not_zero(f):
    usage.add("claude-code", {"usage": {"output_tokens": 5}}, file=f)
    row = lines(f)[0]
    assert row["in"] is None and row["cache"] is None and row["turns"] is None
    assert "in" in row["not_reported"] and "cache" in row["not_reported"]
    tot = usage.summary(file=f)["all"]
    assert tot["out"] == 5 and tot["out_not_reported"] == 0
    assert tot["in"] is None and tot["in_not_reported"] == 1


def test_zero_is_a_measurement_and_stays_one(f):
    usage.add("claude-code", {"usage": {"input_tokens": 0, "output_tokens": 0}}, file=f)
    row = lines(f)[0]
    assert row["in"] == 0 and row["not_reported"] == ["cache", "turns", "cost"]
    tot = usage.summary(file=f)["all"]
    assert tot["in"] == 0 and tot["in_not_reported"] == 0          # reported zero != not reported


def test_a_total_mixes_reported_and_not_reported_without_hiding_either(f):
    usage.add("claude-code", {"usage": {"input_tokens": 10, "output_tokens": 1}}, file=f)
    usage.add("claude-code", {"usage": {"output_tokens": 2}}, file=f)
    tot = usage.summary(file=f)["all"]
    assert tot["in"] == 10 and tot["in_not_reported"] == 1 and tot["calls"] == 2


def test_a_malformed_count_is_not_reported_rather_than_zero(f):
    for used in ({"usage": {"input_tokens": True}}, {"usage": {"input_tokens": -4}},
                 {"usage": {"input_tokens": "lots"}}, {"usage": {"input_tokens": 1.5}},
                 {"usage": {"input_tokens": None}}):
        assert usage.tokens(used)["in"] is None, used
    assert usage.tokens(42)["in"] is None and usage.tokens(42)["not_reported"] == ["in", "out", "cache", "turns", "cost"]


def test_cache_sums_only_the_names_that_were_reported():
    got = usage.tokens({"usage": {"cache_read_input_tokens": 3, "cached_input_tokens": 4}})
    assert got["cache"] == 7
    assert usage.tokens({"usage": {"input_tokens": 1}})["cache"] is None


def test_a_call_that_reported_nothing_is_still_recorded(f):
    """Agtr returns early when nothing was reported (meter.py:51), so the call reads back as never having happened."""
    usage.add("claude-code", {}, kind="ask", tool="t", file=f)
    rows = lines(f)
    assert len(rows) == 1 and rows[0]["calls"] == 1
    assert all(rows[0][k] is None for k in ("in", "out", "cache", "turns", "cost"))
    assert usage.summary(file=f)["all"]["calls"] == 1


def test_a_write_that_fails_is_not_swallowed(tmp_path):
    """Agtr wraps the write in `except OSError: pass` (meter.py:64-65): the meter can stop recording in silence."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")
    with pytest.raises(OSError):
        usage.add("claude-code", CLAUDE, file=blocked / "usage.jsonl")


# ------------------------------------------------------------------ the row, and reading it back
def test_the_row_has_the_agreed_shape_and_the_file_is_private(f):
    row = usage.add("claude-code", CLAUDE, kind="campaign", campaign="c-7", tool="admet_standalone", file=f)
    for k in ("t", "engine", "billed", "kind", "campaign", "tool", "in", "out", "cache", "cost", "turns", "model"):
        assert k in row, k
    assert row["model"] == "claude-opus-5" and row["tool"] == "admet_standalone"
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_rows_are_readable_per_campaign(f):
    usage.add("claude-code", CLAUDE, campaign="c-1", file=f)
    usage.add("claude-code", CLAUDE, campaign="c-2", file=f)
    usage.add("claude-code", CLAUDE, campaign="c-1", file=f)
    got = usage.for_campaign("c-1", file=f)
    assert got["calls"] == 2 and got["totals"]["in"] == 200 and got["totals"]["cost"] == 0.0246
    assert usage.campaigns(file=f) == ["c-1", "c-2"]
    assert set(usage.summary(file=f)["all"]["campaigns"]) == {"c-1", "c-2"}


def test_no_meter_yet_is_absent_and_an_unreadable_one_raises(f, tmp_path):
    assert usage.read_rows(f).absent is True
    assert usage.rows(f) == []
    d = tmp_path / "a-directory.jsonl"
    d.mkdir()
    assert usage.read_rows(d).unresolvable is True
    with pytest.raises(Unresolvable):
        usage.rows(d)


def test_a_torn_line_is_counted_not_dropped_in_silence(f):
    usage.add("claude-code", CLAUDE, file=f)
    with f.open("a") as fh:
        fh.write('{"t": 1, "in": 5')                 # a crash during an append
    res = usage.read_rows(f)
    assert res.resolved and res.meta["skipped"] == 1
    assert usage.summary(file=f)["source"]["skipped"] == 1


# ------------------------------------------------------------------ the fold: Agtr's defect, not ported
def old_row(t, **kw):
    base = {"t": t, "engine": "claude-code", "billed": "plan", "kind": "ask", "campaign": "", "tool": "",
            "in": 10, "out": 2, "cache": None, "cost": 0.001, "turns": 1, "model": "m", "calls": 1,
            "cost_basis": usage.MEASURE, "not_reported": ["cache"]}
    return {**base, **kw}


def write_rows(f, rows):
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_an_old_row_can_never_count_as_todays(f):
    """Agtr stamps the folded line with the LAST old row's time (meter.py:93): months of usage land in today."""
    now = time.time()
    ancient = now - 200 * 86400
    write_rows(f, [old_row(ancient + i, campaign="old") for i in range(3)] + [old_row(now - 60, campaign="new")])
    before = usage.summary(now=now, file=f)
    assert before["today"]["calls"] == 1 and before["all"]["calls"] == 4

    usage.fold(now=now, file=f)
    after = usage.summary(now=now, file=f)
    folded = [r for r in lines(f) if r.get("folded")]
    assert folded and folded[0]["t"] == min(ancient + i for i in range(3))     # the OLDEST, never the newest
    assert folded[0]["t_from"] <= folded[0]["t_to"] < now - usage.WIDEST
    assert after["today"]["calls"] == 1 and after["today"]["in"] == 10
    assert after["week"]["calls"] == 1 and after["month"]["calls"] == 1
    assert after["all"]["calls"] == 4


def test_the_fold_leaves_every_total_and_breakdown_unchanged(f):
    now = time.time()
    ancient = now - 90 * 86400
    rows = []
    for i in range(6):
        rows.append(old_row(ancient + i * 3600, campaign=f"c-{i % 2}", kind=["ask", "campaign"][i % 2],
                            cost=0.002 if i % 2 else None, cache=5 if i % 3 else None))
    rows += [old_row(now - 3600, campaign="c-0"), old_row(now - 10 * 86400, campaign="c-1")]
    write_rows(f, rows)
    before = usage.summary(now=now, file=f)
    before_campaigns = {c: usage.for_campaign(c, file=f)["totals"] for c in ("c-0", "c-1")}

    out = usage.fold(now=now, file=f)
    assert out["folded"] == 6 and out["into"] >= 2
    after = usage.summary(now=now, file=f)
    for window in ("today", "week", "month", "all"):
        assert after[window] == before[window], window
    for c in ("c-0", "c-1"):
        assert usage.for_campaign(c, file=f)["totals"] == before_campaigns[c], c


def test_the_fold_keeps_the_breakdowns_apart(f):
    now = time.time()
    ancient = now - 100 * 86400
    write_rows(f, [old_row(ancient, engine="claude-code", kind="ask"),
                   old_row(ancient + 1, engine="ollama", billed="local", kind="ask", cost=None),
                   old_row(ancient + 2, engine="claude-code", kind="campaign")])
    usage.fold(now=now, file=f)
    folded = [r for r in lines(f) if r.get("folded")]
    assert len(folded) == 3                                   # engine and kind are never merged away
    tot = usage.summary(now=now, file=f)["all"]
    assert set(tot["engines"]) == {"claude-code", "ollama"} and set(tot["kinds"]) == {"ask", "campaign"}
    assert tot["engines"]["claude-code"]["calls"] == 2


def test_the_fold_never_sums_a_not_reported_count_into_a_zero(f):
    now = time.time()
    ancient = now - 100 * 86400
    write_rows(f, [old_row(ancient, cache=None, cost=None), old_row(ancient + 1, cache=None, cost=None)])
    usage.fold(now=now, file=f)
    folded = [r for r in lines(f) if r.get("folded")][0]
    assert folded["cache"] is None and folded["missing"]["cache"] == 2
    tot = usage.summary(now=now, file=f)["all"]
    assert tot["cache"] is None and tot["cache_not_reported"] == 2
    assert tot["cost"] is None and tot["cost_not_reported"] == 2


def test_folding_is_decided_by_time_not_by_position(f):
    """Agtr folds `rows[:-KEEP // 2]` (meter.py:90) while `earlier()` appends OLD rows at the end (meter.py:84), so
    the oldest rows survive and the newest are summed away. Here position in the file means nothing."""
    now = time.time()
    write_rows(f, [old_row(now - 120, campaign="new")] + [old_row(now - 300 * 86400, campaign="old")])
    usage.fold(now=now, file=f)
    rows = lines(f)
    assert [r.get("folded", False) for r in sorted(rows, key=lambda r: r["t"])] == [True, False]
    assert usage.for_campaign("new", file=f)["calls"] == 1     # the recent row is untouched and unfolded


def test_a_recent_row_is_never_folded_to_make_the_file_smaller(f):
    now = time.time()
    write_rows(f, [old_row(now - i) for i in range(50)])
    out = usage.fold(now=now, file=f)
    assert out["folded"] == 0 and "older" in out["why"]
    assert len(lines(f)) == 50


def test_the_fold_refuses_to_rewrite_a_file_with_damage_in_it(f):
    now = time.time()
    write_rows(f, [old_row(now - 100 * 86400)])
    with f.open("a") as fh:
        fh.write("{not json\n")
    out = usage.fold(now=now, file=f)
    assert out["folded"] == 0 and "do not parse" in out["why"]
    assert "{not json" in f.read_text()                        # nothing was thrown away


def test_the_fold_does_not_read_a_meter_it_cannot_read(tmp_path):
    d = tmp_path / "dir.jsonl"
    d.mkdir()
    with pytest.raises(Unresolvable):
        usage.fold(file=d)


def test_a_folded_line_that_reaches_into_a_window_is_not_folded_again(f):
    now = time.time()
    write_rows(f, [{**old_row(now - 100 * 86400), "folded": True, "rows": 2, "calls": 2,
                    "t_from": now - 100 * 86400, "t_to": now - 60, "missing": {"cache": 2}}])
    out = usage.fold(now=now, file=f)
    assert out["folded"] == 0
    # and a straddling line — which only an older writer could leave — is counted in no window but "all": a span
    # that starts 100 days ago is not this week's spending, whatever its newest moment is.
    s = usage.summary(now=now, file=f)
    assert s["today"]["calls"] == 0 and s["week"]["calls"] == 0 and s["month"]["calls"] == 0
    assert s["all"]["calls"] == 2


# ------------------------------------------------------------------ the summary's own shape
def test_the_windows_are_today_week_month_all_with_breakdowns_and_recent(f):
    for i in range(45):
        usage.add("claude-code", CLAUDE, campaign="c", kind="ask", file=f)
    s = usage.summary(file=f)
    assert set(s) == {"today", "week", "month", "all", "recent", "folded", "cost_note", "source"}
    assert len(s["recent"]) == 40 and s["recent"][0]["t"] >= s["recent"][-1]["t"]
    for w in ("today", "week", "month", "all"):
        assert set(s[w]) >= {"calls", "in", "in_not_reported", "cost", "engines", "kinds", "campaigns", "billed"}
    assert s["all"]["calls"] == 45 and s["all"]["in"] == 4500
