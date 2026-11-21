"""The refusal that outlived its own mechanism.

On 2026-11-20 the approval gate was taken out of the code, and the sentence that announced it stayed in the tool
DESCRIPTIONS: "Needs a person's approval; Claude cannot run it." — on 335 of the 502 tools, `peptide_discovery` and
`admet_standalone` among them. A description is the only thing a model obeys with no code behind it, so the gate kept
working perfectly in the one direction that hurt: the owner's own pipelines would not start, and nothing in Studio
would have refused them had the model tried. A lock that exists only in the text is worse than no lock, because it
stops the person holding the key and stops nobody else.

This file holds the two halves together: what the descriptions SAY and what `check` DOES. Re-add a refusal to the
prose and `test_no_description_tells_claude_it_may_not_run` fails; re-add the gate to the code and
`test_the_code_refuses_nothing_for_being_expensive` fails, so the prose has to come back with it. Neither half moves
alone.

It runs over a MATRIX of entries rather than the owner's allowlist, because `_describe` is the single source of all
502 descriptions and the owner's file is not on a fresh machine — a guard that passes because the list was empty is
the same defect as a guard that never fails. The real file is checked too, in the one test that skips without it,
and that test asserts the list was big before it believes its own result.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from studio import allowlist as al
from studio.adapter import Adapter, Refused
from studio.mcp_server import _describe, tool_list

SRC = Path(__file__).resolve().parent.parent / "studio" / "mcp_server.py"

# A model reads any of these as "do not call this". Phrases, not single words: "approval" alone is legitimate in a
# cost label, and a guard that fired on the word would be switched off within a week.
REFUSALS = [
    r"cannot run", r"can'?t run", r"claude (?:may|must|can) ?not", r"may not (?:run|call)",
    r"needs? (?:a )?(?:person'?s |human |owner'?s )?approval",
    r"requires? (?:a )?(?:person'?s |human |owner'?s )?approval",
    r"not allowed to (?:run|call)", r"do not (?:run|call) (?:this|it)", r"will be refused",
    r"ask (?:the )?(?:person|owner|user) (?:first|before)", r"only (?:a|the) (?:person|owner) can run",
]
BANNED = re.compile("|".join(REFUSALS), re.I)

KINDS = ("status", "local", "compute", "hardware")
REGISTRY_META = {"name": "Peptide Discovery", "modality": "peptide", "cost_tier": "high",
                 "intent_terms": ["design a peptide"], "forbidden_inference": ["sequence"],
                 "mutually_exclusive": ["quantum_peptide_design"]}


def matrix() -> dict[str, dict]:
    """Every kind x approval x with/without BioQL registry metadata: 16 entries, one of which is what 335 real
    tools looked like."""
    out = {}
    for kind in KINDS:
        for approval in (False, True):
            for meta in (None, REGISTRY_META):
                name = f"e_{kind}_{'appr' if approval else 'free'}_{'meta' if meta else 'bare'}"
                e = {"method": "POST", "path": f"/api/{name}", "kind": kind, "approval": approval,
                     "why": "What this does, in BioQL's own words.", "required": [], "optional": []}
                e = al._norm(name, e)
                e["from_registry"] = meta
                out[name] = e
    return out


def described() -> dict[str, str]:
    return {n: _describe(e) for n, e in matrix().items()}


# ------------------------------------------------------------------ what the descriptions say

def test_no_description_tells_claude_it_may_not_run():
    """THE test."""
    bad = {n: BANNED.search(d).group(0) for n, d in described().items() if BANNED.search(d)}
    assert not bad, f"{len(bad)} description(s) refuse in prose while the code refuses nothing: {bad}"


def test_the_extra_tools_do_not_refuse_either(fake_bioql):
    """`run_many` and the seven studio_* tools are described by hand, not by `_describe`. The batch tool's text is
    read before every fan-out, and the fan-out is the exact path the owner's own call failed on."""
    bad = {t["name"]: BANNED.search(t["description"]).group(0)
           for t in tool_list(Adapter(fake_bioql, matrix()))
           if BANNED.search(t["description"] or "")}
    assert not bad, f"hand-written tool descriptions still refuse: {bad}"


def test_the_cost_is_still_named_because_it_is_still_real():
    """Removing the refusal must not remove the warning: a compute entry costs money whether or not anything asks
    first, and a description that stopped saying so would trade one dishonesty for another."""
    d = described()
    for n, e in matrix().items():
        says = "costs real compute" in d[n].lower()
        should = e["kind"] in ("compute", "hardware") or e["approval"]
        assert says is should, f"{n} (kind={e['kind']}, approval={e['approval']}): cost named={says}"
        if e["kind"] == "hardware":
            assert "real quantum hardware" in d[n].lower(), f"{n} does not warn about the chip"


def test_the_batch_tool_accepts_the_name_the_client_shows():
    """The owner's `run_many` came back REFUSED for `mcp__bioql__backend_health` — the only name their client ever
    showed them. The adapter now strips that prefix; the description has to say so, or the model will keep
    apologising for a name that works."""
    one = [t for t in tool_list(Adapter("http://127.0.0.1:9", matrix())) if t["name"] == "run_many"][0]
    assert "mcp__bioql__" in one["description"]


# ------------------------------------------------------------------ what the code does

def test_the_code_refuses_nothing_for_being_expensive(fake_bioql):
    """The other half. If this starts failing, a gate came back — and the descriptions must say so again."""
    es = matrix()
    a = Adapter(fake_bioql, es)
    expensive = [n for n, e in es.items() if e["kind"] in ("compute", "hardware") or e["approval"]]
    assert len(expensive) == 12, f"the matrix changed shape: {len(expensive)}"
    refused = []
    for n in expensive:
        try:
            a.check(n, {}, approved_by=None)
        except Refused as why:
            if re.search(r"approv|hardware|qpu", str(why), re.I):
                refused.append((n, str(why)))
    assert not refused, f"the cost gate is back in the code for {len(refused)} entries: {refused[:3]}"


def test_the_sentence_is_not_in_the_source_either():
    """Docstrings are stripped first, and comments never reach the AST. The file explains the removed sentence by
    QUOTING it — that is what makes the explanation readable — and a guard that read its own explanation is a
    defect this project has already shipped twice (CLAUDE.md, 2026-09-18)."""
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]
    hit = BANNED.search(ast.unparse(ast.fix_missing_locations(tree)))
    assert not hit, f"a refusal is built in live code: {hit.group(0)!r}"


# ------------------------------------------------------------------ the owner's real list, when it is there

def test_the_real_allowlist_on_this_machine(monkeypatch):
    """The matrix proves the function; this proves the file the owner's clients are actually served. It skips where
    there is no such file, and refuses to pass on a list too small to mean anything."""
    monkeypatch.delenv("BIOQL_STUDIO_HOME", raising=False)
    f = al.path_file()
    if not f.exists():
        pytest.skip(f"no owner allowlist at {f}")
    es = al.load(f)
    assert len(es) > 100, f"only {len(es)} entries: too few to conclude anything from"
    bad = {n: BANNED.search(_describe(e)).group(0) for n, e in es.items() if BANNED.search(_describe(e))}
    assert not bad, f"{len(bad)} of {len(es)} real tools still refuse in prose: {dict(list(bad.items())[:5])}"


# ------------------------------------------------------------------ the guard must fail when it should

@pytest.mark.parametrize("why", [
    "Needs a person's approval; Claude cannot run it.",
    "REQUIRES APPROVAL before use.",
    "Ask the owner first.",
    "Claude may not call this.",
])
def test_the_guard_catches_an_injected_refusal(why):
    """A guard that never fails is the same defect it exists to prevent. Four spellings, because the first version
    of a sibling guard in this suite was case-sensitive and sailed straight past an injected `ADVANCED_ONLY`."""
    es = matrix()
    name = "e_compute_appr_bare"
    es[name] = dict(es[name], why=why)
    hit = {n: d for n, d in ((k, _describe(v)) for k, v in es.items()) if BANNED.search(d)}
    assert name in hit, f"the guard missed {why!r} — it would ship a text-only refusal again"
