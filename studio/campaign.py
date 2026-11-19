"""The campaign: a question with success criteria, decomposed into BioQL runs, each verified, then audited, then
reported. The unit of scientific work, as a library — no app, no thread that cannot be stopped.

The planner and the auditor are injected callables, so neither this module nor its tests contain a language model.
What this module owns is the order and the brakes:

- a plan is validated against the adapter BEFORE it is accepted (every tool in the allowlist, every argument through
  `Adapter.check`). A plan naming an unknown tool or smuggling an argument is REJECTED with the reason and recorded —
  never trimmed into something callable;
- a person confirms the plan; nothing runs before `confirm()`;
- each run goes to its OWN results folder, so no run can write into another's (the moral equivalent of a worktree);
- approval is not laundered: the approver `execute` is given is what each run carries, and a run whose entry demands
  approval without one is refused like any other call and recorded as refused;
- a REFUSED or UNRESOLVABLE run is recorded as such and never becomes a result (the three states, CLAUDE.md
  2026-11-18);
- the auditor did not run the work and is given only the criteria, the raw numbers and the provenance — never a
  summary to agree with. An auditor that cannot decide yields UNKNOWN, never "unsupported": a failure to check is not
  evidence against;
- `stop()` ends the campaign and `execute` returns with no thread still running; a stopped campaign is never `done`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import home, write_private
from . import ledger
from . import verify
from .adapter import Adapter, Refused, wants_hardware
from .resolution import RESOLVED, UNRESOLVABLE

PLANNING, AWAITING, RUNNING, VERIFYING, AUDITING, DONE, FAILED, STOPPED = (
    "planning", "awaiting_confirmation", "running", "verifying", "auditing", "done", "failed", "stopped")
STATES = (PLANNING, AWAITING, RUNNING, VERIFYING, AUDITING, DONE, FAILED, STOPPED)

PLANNED, REFUSED, STOPPED_RUN = "PLANNED", "REFUSED", "STOPPED"
MAX_WORKERS = 8                 # the plan says start small: two by default, eight at the very most
MAX_NUMBERS = 400               # how many raw numbers the auditor is handed

# At plan time the route and the arguments are checked; WHO approves is a question for `execute`, which passes the real
# approver to every call. So the approval gate is satisfied here by a sentinel and nowhere else: a plan can never make
# a call, and `execute` asks the gate again with the person's own name (or refuses).
_PLAN_CHECK = "plan validation only, no call is made"

_STOPS: dict[str, threading.Event] = {}
# One lock for every state transition AND for `save`: a `stop()` arriving while `execute` or `audit` is writing its
# final state must not lose the race and leave `campaign.json` saying `verifying` or `done`. Reentrant because the
# transition holds it across the save.
_LOCK = threading.RLock()


class Rejected(ValueError):
    """Studio will not accept this plan. The reason is the message; `campaign_id` points at the recorded plan."""

    def __init__(self, why: str, campaign_id: str | None = None) -> None:
        super().__init__(why)
        self.campaign_id = campaign_id


@dataclass
class Run:
    n: int
    tool: str
    args: dict = field(default_factory=dict)
    why: str = ""
    state: str = PLANNED
    detail: str = ""
    needs_approval: bool = False
    approved_by: str | None = None
    result_sha256: str | None = None
    provenance: dict | None = None
    verdict: str | None = None
    findings: list = field(default_factory=list)
    folder: str | None = None
    ledger_position: int | None = None
    ledger_hash: str | None = None
    seconds: float | None = None

    @property
    def is_result(self) -> bool:
        """Only a RESOLVED run is a result. A refusal and an UNRESOLVABLE are records, not science."""
        return self.state == RESOLVED


@dataclass
class Campaign:
    id: str
    question: str
    title: str = ""
    criteria: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    state: str = PLANNING
    created: float = 0.0
    confirmed_by: str | None = None
    confirmed_at: float | None = None
    approved_by: str | None = None
    plan_digest: str = ""
    audited: list = field(default_factory=list)
    reason: str = ""
    stopped_at: float | None = None

    @property
    def folder(self) -> Path:
        return folder_of(self.id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["runs"] = [asdict(r) if isinstance(r, Run) else dict(r) for r in self.runs]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Campaign":
        d = dict(d)
        d["runs"] = [Run(**r) for r in d.get("runs") or []]
        return cls(**d)


# ---------------------------------------------------------------------------- where a campaign lives
def folder_of(campaign_id: str) -> Path:
    if not campaign_id or any(c in campaign_id for c in "/\\.") or len(campaign_id) > 64:
        raise ValueError(f"bad campaign id {campaign_id!r}")
    return home() / "campaigns" / campaign_id


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def save(c: Campaign) -> Campaign:
    with _LOCK:                 # two savers would otherwise collide on the one campaign.json.tmp
        d = c.folder
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(d / "campaign.json", json.dumps(c.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n")
    return c


def load(campaign_id: str) -> Campaign:
    f = folder_of(campaign_id) / "campaign.json"
    return Campaign.from_dict(json.loads(f.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------- 1. plan
def plan(question: str, propose: Callable[[str], dict], adapter: Adapter | None = None) -> Campaign:
    """Ask the injected planner for a plan, validate it against the adapter, record it, and return it awaiting a
    person's confirmation. A plan that does not validate is recorded and then raised: never trimmed."""
    if not str(question or "").strip():
        raise ValueError("a campaign needs a question")
    a = adapter or Adapter()
    c = Campaign(id=_new_id(), question=str(question), created=time.time(), state=PLANNING)
    try:
        raw = propose(question)
    except Exception as err:                        # the planner's failure is the campaign's, and it is recorded
        c.state, c.reason = FAILED, f"the planner raised {type(err).__name__}: {err}"
        save(c)
        raise Rejected(c.reason, c.id) from err
    try:
        c.title, c.criteria, c.runs = _validate(raw, a)
    except Rejected as why:
        c.state, c.reason = FAILED, str(why)
        save(c)
        raise Rejected(str(why), c.id) from None
    c.state = AWAITING
    return save(c)


def _validate(raw: Any, a: Adapter) -> tuple[str, list, list]:
    if not isinstance(raw, dict):
        raise Rejected(f"the plan must be an object, not {type(raw).__name__}")
    extra = sorted(set(raw) - {"title", "criteria", "runs"})
    if extra:
        # Nothing in a plan is ignored: a field Studio does not read is a field the planner expected to matter.
        raise Rejected(f"the plan carries {extra}, which a plan does not get to set (only title, criteria, runs)")
    title = str(raw.get("title") or "").strip()
    crit_in = raw.get("criteria")
    runs_in = raw.get("runs")
    if not title:
        raise Rejected("the plan has no title")
    if not isinstance(crit_in, list):
        # A string would iterate into characters: that is not one criterion, it is a silent misreading.
        raise Rejected(f"criteria must be a list, not {type(crit_in).__name__}")
    criteria = [str(x).strip() for x in crit_in]
    if not criteria or not all(criteria):
        raise Rejected("the plan states no success criteria, or states an empty one: there would be nothing to audit")
    if len(set(criteria)) != len(criteria):
        raise Rejected("the plan repeats a criterion: one audited verdict could not be told from another")
    if not isinstance(runs_in, list) or not runs_in:
        raise Rejected("the plan has no runs")
    runs: list[Run] = []
    for i, r in enumerate(runs_in, 1):
        if not isinstance(r, dict):
            raise Rejected(f"run {i} is not an object")
        extra = sorted(set(r) - {"tool", "args", "why"})
        if extra:
            # `approved_by` lands here: a plan cannot carry its own approval, and no field of a plan is ignored.
            raise Rejected(f"run {i} carries {extra}, which a plan does not get to set (only tool, args, why)")
        tool = str(r.get("tool") or "")
        args = r.get("args") if r.get("args") is not None else {}
        if not isinstance(args, dict):
            raise Rejected(f"run {i} ({tool!r}): args must be an object")
        try:
            e = a.check(tool, args, approved_by=_PLAN_CHECK)
        except Refused as why:
            raise Rejected(f"run {i}: {why}") from None
        runs.append(Run(n=i, tool=tool, args=dict(args), why=str(r.get("why") or ""),
                        needs_approval=bool(e["approval"] or e["kind"] == "hardware" or wants_hardware(args))))
    return title, criteria, runs


def _plan_digest(runs: list) -> str:
    """What was confirmed, as a hash of the calls themselves. A run added after the confirmation does not inherit the
    person's approval: `execute` compares this and refuses."""
    return ledger.digest([[r.tool, r.args] for r in runs])


def confirm(c: Campaign, by: str) -> Campaign:
    """The person confirms the plan. Nothing runs without this."""
    if not str(by or "").strip():
        raise Refused("a confirmation needs a person")
    if c.state != AWAITING:
        raise Refused(f"a campaign in {c.state!r} is not awaiting confirmation")
    c.confirmed_by, c.confirmed_at = str(by).strip(), time.time()
    c.plan_digest = _plan_digest(c.runs)
    return save(c)


# ---------------------------------------------------------------------------- 2. run, 3. verify
def stop(c: Campaign, why: str = "stopped by the person") -> Campaign:
    """End the campaign. A run not yet started never starts; `execute` returns only once no thread is still running.

    What it cannot do: interrupt a call already in flight. `Adapter.call` is a blocking HTTP request, so `execute`
    waits for it to return (bounded by that entry's own timeout) and records it — that is the honest meaning of "no
    run left dangling", not that a running request is killed.
    """
    with _LOCK:
        if c.state in (DONE, FAILED):
            return c                                  # a finished campaign is not re-opened to be stopped
        _STOPS.setdefault(c.id, threading.Event()).set()
        c.state, c.reason, c.stopped_at = STOPPED, why, time.time()
        return save(c)


def _stopped(c: Campaign) -> bool:
    with _LOCK:
        ev = _STOPS.get(c.id)
        return bool(ev and ev.is_set()) or c.state == STOPPED


def workers_for(n: int) -> int:
    """The pool size actually used: at least one, never more than MAX_WORKERS (the plan says start small)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, MAX_WORKERS))


def execute(c: Campaign, approved_by: str | None = None, adapter: Adapter | None = None,
            workers: int = 2) -> Campaign:
    """Run every planned call through the adapter, each into its own results folder.

    `approved_by` is the person who approved THIS campaign, and it is what every run records. A run whose entry demands
    approval and has none is refused like any other call: a campaign is not a way to launder approval.
    """
    if c.state == STOPPED or _stopped(c):
        raise Refused("this campaign was stopped")
    if not c.confirmed_by:
        raise Refused("this campaign has not been confirmed by a person: nothing runs")
    if c.state not in (AWAITING, PLANNING):
        raise Refused(f"a campaign in {c.state!r} has already run")
    if c.plan_digest and c.plan_digest != _plan_digest(c.runs):
        raise Refused("the runs changed after the person confirmed them: confirm the plan again")
    a = adapter or Adapter()
    with _LOCK:
        _STOPS.setdefault(c.id, threading.Event())
        c.approved_by = (approved_by or "").strip() or None
        c.state = RUNNING
        save(c)
    claimed: set[int] = set()
    pool = ThreadPoolExecutor(max_workers=workers_for(workers), thread_name_prefix=f"campaign-{c.id}")
    try:
        # cancel_futures leaves nothing queued, and shutdown(wait=True) returns only when no worker is still running.
        for r in c.runs:
            pool.submit(_one, c, r, a, claimed)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    with _LOCK:
        if _stopped(c):
            c.state, c.stopped_at = STOPPED, c.stopped_at or time.time()
            for r in c.runs:
                if r.state == PLANNED:      # cancelled while queued: it never started, and the record says why
                    r.state, r.detail = STOPPED_RUN, "the campaign was stopped before this run started"
        else:
            for r in c.runs:
                if r.state == PLANNED:
                    # A planned run that is neither done nor stopped is a defect in Studio, not an empty result.
                    r.state, r.detail = UNRESOLVABLE, "this run never ran and the campaign was not stopped"
            if not any(r.is_result for r in c.runs):
                c.state = FAILED
                c.reason = c.reason or "no run produced a result"
            else:
                c.state = VERIFYING         # every result is checked; it now awaits an independent audit
        return save(c)


def _one(c: Campaign, r: Run, a: Adapter, claimed: set[int]) -> None:
    if _stopped(c):
        r.state, r.detail = STOPPED_RUN, "the campaign was stopped before this run started"
        return
    began = time.time()
    r.approved_by = c.approved_by
    d = c.folder / f"run-{r.n}"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    r.folder = str(d)
    try:
        res = a.call(r.tool, r.args, approved_by=c.approved_by, by="studio-campaign")
    except Refused as why:
        r.state, r.detail, r.seconds = REFUSED, str(why), round(time.time() - began, 3)
        _write_run(c, r, None)
        _claim(r, claimed)
        return
    except Exception as err:
        # Nothing escapes a worker: an unexpected failure is UNRESOLVABLE with its cause, never a run left `PLANNED`
        # with no record of why (and never a plausible empty result).
        r.state, r.detail = UNRESOLVABLE, f"{type(err).__name__}: {err}"
        r.seconds = round(time.time() - began, 3)
        _write_run(c, r, None)
        return
    r.state, r.detail = res.state, res.detail
    r.seconds = res.meta.get("seconds", round(time.time() - began, 3))
    r.provenance = res.meta.get("provenance")
    if res.resolved:
        # The adapter already checked this answer; re-checking here would be a second, divergent verdict.
        r.findings = res.meta.get("findings") if res.meta.get("findings") is not None else verify.check(res.value, r.provenance)
        r.verdict = res.meta.get("verdict") or verify.verdict(r.findings)
        r.result_sha256 = ledger.digest(res.value)
        _write_run(c, r, res.value)
    else:
        # UNRESOLVABLE (and ABSENT) never becomes a result: no result file, no hash, no verdict.
        _write_run(c, r, None)
    _claim(r, claimed)


def _write_run(c: Campaign, r: Run, result: Any) -> None:
    """Everything this run produced, inside this run's own folder and nowhere else."""
    d = Path(r.folder)
    write_private(d / "run.json", json.dumps({k: v for k, v in asdict(r).items()}, indent=2,
                                             ensure_ascii=False, default=str) + "\n")
    if result is not None:
        write_private(d / "result.json", json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")


def _claim(r: Run, claimed: set[int]) -> None:
    """The ledger line this run wrote, so a reader can go from a conclusion back to the raw call.

    The adapter writes the line itself and does not hand back its position, so it is found by matching the line's
    tool, state and result hash — and claimed, so two runs of the same tool cannot point at one line.
    """
    f = ledger.path_file()
    if not f.exists():
        return
    rows = f.read_bytes().splitlines()
    with _LOCK:
        for i in range(len(rows), 0, -1):
            if i in claimed:
                continue
            try:
                row = json.loads(rows[i - 1])
            except ValueError:
                continue
            if row.get("tool") == r.tool and row.get("state") == r.state \
                    and row.get("result_sha256") == r.result_sha256 and row.get("args") == r.args:
                claimed.add(i)
                r.ledger_position, r.ledger_hash = i, row.get("hash")
                return


# ---------------------------------------------------------------------------- 4. audit
def _numbers(o: Any, path: str = "$", depth: int = 0, out: dict | None = None) -> dict:
    """The raw numbers of a result, by JSON path. No prose: the auditor re-derives the claim from the data rather than
    agreeing with a summary written by whoever ran it."""
    out = {} if out is None else out
    if len(out) >= MAX_NUMBERS or depth > 7:
        return out
    if isinstance(o, bool) or isinstance(o, (int, float)):
        out[path] = o
    elif isinstance(o, dict):
        for k, v in o.items():
            _numbers(v, f"{path}.{k}", depth + 1, out)
    elif isinstance(o, list):
        for i, v in enumerate(o[:200]):
            _numbers(v, f"{path}[{i}]", depth + 1, out)
    return out


def evidence(c: Campaign) -> dict:
    """What the auditor is given: the criteria, and per run only the raw numbers and the provenance block.

    Deliberately NOT included: the question's own wording, any prose from the result, Studio's own verdict and
    findings. An auditor shown a conclusion tends to return it confirmed; it is here to re-derive each criterion from
    the data. What it keeps of the call is the tool, its parameters and the hash — identity, not interpretation.
    """
    runs = []
    for r in c.runs:
        if not r.is_result:
            continue
        result = None
        f = Path(r.folder or "") / "result.json"
        if f.exists():
            result = json.loads(f.read_text(encoding="utf-8"))
        runs.append({"run": r.n, "tool": r.tool, "args": r.args, "numbers": _numbers(result),
                     "provenance": r.provenance, "result_sha256": r.result_sha256,
                     "ledger_position": r.ledger_position})
    return {"criteria": list(c.criteria), "runs": runs}


def audit(c: Campaign, auditor: Callable[[dict], Any]) -> Campaign:
    """Have a caller that did NOT run the work re-derive each criterion from the numbers.

    Anything the auditor cannot answer is UNKNOWN (UNRESOLVABLE), never "supported: false": a failure to check is not
    evidence against the claim.
    """
    if c.state == STOPPED or _stopped(c):
        raise Refused("this campaign was stopped")
    if c.state not in (VERIFYING, FAILED, AUDITING, DONE):
        raise Refused(f"a campaign in {c.state!r} has no results to audit")
    with _LOCK:
        was, c.state = c.state, AUDITING
        save(c)
    ev = evidence(c)
    if not ev["runs"]:
        # Nothing to re-derive from: the auditor is not asked, because an answer with no data behind it is exactly
        # the plausible value this whole module exists to refuse.
        said, unknown_why = None, "no run produced a result, so no criterion could be checked"
        return _record_audit(c, was, said, unknown_why)
    try:
        said = auditor(ev)
    except Exception as err:
        said = None
        unknown_why = f"the auditor raised {type(err).__name__}: {err}"
    else:
        unknown_why = "the auditor did not answer this criterion"
    return _record_audit(c, was, said, unknown_why)


def _record_audit(c: Campaign, was: str, said: Any, unknown_why: str) -> Campaign:
    by_claim: dict[str, dict] = {}
    for item in (said if isinstance(said, list) else [said] if isinstance(said, dict) else []):
        if isinstance(item, dict) and str(item.get("claim") or "").strip():
            by_claim[str(item["claim"]).strip()] = item
    out = []
    for claim in c.criteria:
        item = by_claim.get(claim)
        sup = item.get("supported") if item else None
        why = str((item or {}).get("why") or "").strip()
        if isinstance(sup, bool):
            out.append({"claim": claim, "state": RESOLVED, "supported": sup, "why": why})
        else:
            out.append({"claim": claim, "state": UNRESOLVABLE, "supported": None,
                        "why": why or unknown_why})
    c.audited = out
    if _stopped(c):
        c.state, c.stopped_at = STOPPED, c.stopped_at or time.time()
    else:
        # A campaign whose runs produced nothing stays failed: an audit does not promote it.
        c.state = FAILED if was == FAILED else DONE
    return save(c)


# ---------------------------------------------------------------------------- 5. report
def report(c: Campaign) -> dict:
    """The campaign as a reader needs it: what is wrong first, then the criteria, then every run with its provenance
    (real-or-null) and the ledger position that leads back to the raw call."""
    highs = [(r, f) for r in c.runs for f in (r.findings or []) if f.get("severity") == verify.HIGH]
    unsupported = [v for v in c.audited if v.get("state") == RESOLVED and v.get("supported") is False]
    unknown = [v for v in c.audited if v.get("state") == UNRESOLVABLE]
    attention = []
    if c.state == STOPPED:
        attention.append(f"STOPPED: {c.reason or 'the campaign was stopped'} — this campaign is not done")
    if c.state == FAILED:
        attention.append(f"FAILED: {c.reason or 'no run produced a result'}")
    for r, f in highs:
        attention.append(f"HIGH {f['code']} in run {r.n} ({r.tool}) at {f.get('path')}: {f.get('detail')}")
    for v in unsupported:
        attention.append(f"NOT SUPPORTED: {v['claim']} — {v.get('why') or 'the auditor says the data does not show it'}")
    for v in unknown:
        attention.append(f"UNKNOWN (not checked, which is not evidence against): {v['claim']} — {v.get('why')}")
    for r in c.runs:
        if r.state == REFUSED:
            attention.append(f"REFUSED run {r.n} ({r.tool}): {r.detail}")
        elif r.state == UNRESOLVABLE:
            attention.append(f"UNRESOLVABLE run {r.n} ({r.tool}) — not a result: {r.detail}")
    return {"campaign": {"id": c.id, "question": c.question, "title": c.title, "state": c.state,
                         "created": c.created, "confirmed_by": c.confirmed_by, "approved_by": c.approved_by,
                         "reason": c.reason},
            "attention": attention,
            "clean": not attention,
            "criteria": list(c.audited) or [{"claim": x, "state": UNRESOLVABLE, "supported": None,
                                             "why": "not audited yet"} for x in c.criteria],
            "runs": [{"n": r.n, "tool": r.tool, "args": r.args, "why": r.why, "state": r.state, "detail": r.detail,
                      "needs_approval": r.needs_approval, "approved_by": r.approved_by,
                      "result_sha256": r.result_sha256, "provenance": r.provenance, "verdict": r.verdict,
                      "findings": r.findings, "folder": r.folder, "ledger_position": r.ledger_position,
                      "ledger_hash": r.ledger_hash, "seconds": r.seconds} for r in c.runs],
            "ledger": {"file": str(ledger.path_file()),
                       "positions": [r.ledger_position for r in c.runs if r.ledger_position]}}


def render(rep: dict) -> str:
    """The report as Markdown. What is wrong is at the top, never in a footnote."""
    c = rep["campaign"]
    L = [f"# {c['title'] or 'Campaign'} ({c['id']})", "", f"**Question.** {c['question']}", "",
         f"State: `{c['state']}` · confirmed by: {c['confirmed_by'] or '—'} · approved by: {c['approved_by'] or '—'}", ""]
    if rep["attention"]:
        L += ["## Read this first", ""] + [f"- {x}" for x in rep["attention"]] + [""]
    else:
        L += ["No high finding, and no criterion left unsupported or unchecked.", ""]
    L += ["## Criteria", ""]
    for v in rep["criteria"]:
        mark = {True: "supported", False: "NOT supported"}.get(v.get("supported"), "unknown (not checked)")
        L.append(f"- **{v['claim']}** — {mark}" + (f": {v['why']}" if v.get("why") else ""))
    L += ["", "## Runs", ""]
    for r in rep["runs"]:
        p = r["provenance"] or {}
        real = p.get("is_real_hardware")
        L += [f"### Run {r['n']} — `{r['tool']}` · {r['state']}",
              f"- why: {r['why'] or '—'}",
              f"- parameters: `{json.dumps(r['args'], sort_keys=True, ensure_ascii=False)}`",
              f"- approval: {'required' if r['needs_approval'] else 'not required'}"
              f" · approved by: {r['approved_by'] or '—'}",
              f"- result sha256: `{r['result_sha256'] or '— (no result)'}`",
              f"- real hardware: {'not stated by BioQL' if real is None else real}"
              f" · IBM job ids: {', '.join(p.get('ibm_job_ids') or []) or '—'}"
              f" · backends: {', '.join(p.get('quantum_backends') or []) or '—'}",
              f"- verdict: {r['verdict'] or '—'}",
              f"- ledger line: {r['ledger_position'] or '—'} (`{(r['ledger_hash'] or '')[:12] or '—'}`)",
              f"- folder: {r['folder'] or '—'}"]
        if r["detail"]:
            L.append(f"- detail: {r['detail']}")
        for f in r["findings"] or []:
            L.append(f"- [{f['severity']}] {f['code']} at {f.get('path')}: {f.get('detail')}")
        L.append("")
    L += [f"Raw calls: {rep['ledger']['file']}, lines {rep['ledger']['positions'] or '—'}.", ""]
    return "\n".join(L)
