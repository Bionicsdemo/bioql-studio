"""The signed lab notebook: Ed25519 on top of the hash-chained ledger, and the entry a reader is handed.

`ledger.py` already chains every call (each line carries the previous line's hash). Chaining proves a line was not
changed *after* the next one was written; it does not prove who wrote the file, and anyone who can rewrite the whole
file can rewrite the chain with it. The device key is what a chain alone cannot give: every line carries a signature
over its hash, so a rewritten log is a log signed by nobody.

Where the signature lives, and why beside the line rather than on it. `ledger.verify` recomputes each line's hash
over every field but `hash` itself, so a `sig` written into the line would be hashed on the next read and the
ledger's own verifier would call the honest line broken. A signature over the hash cannot be inside what it signs.
So `runs.jsonl` stays exactly the ledger's format — `python3 -m studio ledger verify` and the adapter keep working
unchanged — and the signatures go to `runs.jsonl.sig`, one line per ledger line, keyed by the line's hash rather than
by its position. Keying by hash is what makes a removed or truncated line visible: its signature is left orphaned.

Three states, not two (CLAUDE.md, 2026-11-18). A line whose `key_id` is not the current key's is **unverifiable**,
not broken: the verifier says which key signed it and how many lines, and `ok` is false because it could not be
checked — never because the content was judged bad. The same for an unsigned line and for a missing public key.
`verify` never says `ok: True` for something it could not check, and never invents a verdict it did not reach.

Honest limits, stated rather than hidden:
- truncating the ledger and its signature file by the same lines is invisible here. Only an anchor kept elsewhere
  (a printed hash, another machine) catches that; the notebook reports what it can see;
- whoever holds the private key can rebuild the whole log — chain, `seq` and signatures — and it will verify. And
  `notebook.pub` sits beside the log, so replacing both keys together also verifies clean. What this catches is a
  log altered WITHOUT the key; what makes key replacement visible is the `key_id` being pinned somewhere else (a
  report, a printout, another machine), not the files themselves;
- the key proves the device, not the person. `by` and `approved_by` in the row are claims the row makes;
- a signature says this line is the line that was written. It says nothing about whether BioQL's science was right —
  that is `verify.py`'s job, and a flagged result stays flagged in the entry.
"""
from __future__ import annotations

import binascii
import hashlib
import importlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import home, write_private
from . import ledger

KEY_NAME, PUB_NAME, SIG_SUFFIX = "notebook.key", "notebook.pub", ".sig"
KEY_ID_LEN = 16                 # 8 bytes of sha256(public key): enough to name a key, too short to be mistaken for one

# Studio's own lock, and reentrant: `record` holds it across seq + append + signature (two threads reading the same
# seq would brick an honest log), and `sign_line` re-enters it through `device_key`. `ledger._LOCK` is a plain Lock
# and is not taken here: holding it around `ledger.append`, which takes it itself, would deadlock.
_LOCK = threading.RLock()

VALID, UNSIGNED, OTHER_KEY, BAD, NO_KEY = "VALID", "UNSIGNED", "OTHER_KEY", "BAD_SIGNATURE", "NO_PUBLIC_KEY_HERE"
STATES = (VALID, UNSIGNED, OTHER_KEY, BAD, NO_KEY)


class NoCrypto(RuntimeError):
    """Ed25519 is not available. Said plainly, never worked around: a hand-rolled signature is worse than none."""


class CorruptKey(RuntimeError):
    """The key file exists and is not a key. Never regenerated: that would silently retire a key that signed lines."""


def _mod(name: str):
    """Imported here so a missing library is one clear error at one place (and testable)."""
    return importlib.import_module(name)


def _ed():
    try:
        return _mod("cryptography.hazmat.primitives.asymmetric.ed25519")
    except ImportError as why:
        raise NoCrypto(
            "the signed notebook needs Ed25519 from `cryptography` (pip install cryptography); "
            f"it could not be imported: {why}. Studio does not sign with anything else."
        ) from why


def _invalid_signature():
    return _mod("cryptography.exceptions").InvalidSignature


# ---------------------------------------------------------------- the device key
def key_path() -> Path:
    return home() / KEY_NAME


def pub_path() -> Path:
    return home() / PUB_NAME


def sig_path(file: Path | None = None) -> Path:
    f = file or ledger.path_file()
    return f.with_name(f.name + SIG_SUFFIX)


def _hex32(text: str, what: str) -> bytes:
    raw = text.strip().split()[0] if text.strip() else ""
    try:
        b = binascii.unhexlify(raw)
    except (binascii.Error, ValueError) as why:
        raise CorruptKey(f"{what} is not hex ({why}); it is not being replaced — move it aside by hand") from why
    if len(b) != 32:
        raise CorruptKey(f"{what} holds {len(b)} bytes, not the 32 an Ed25519 key has; it is not being replaced")
    return b


def device_key(create: bool = True):
    """The private key, created on first use with mode 0600. A corrupt key raises; it is never overwritten."""
    ed = _ed()
    kp, pp = key_path(), pub_path()
    with _LOCK:
        if kp.exists():
            priv = ed.Ed25519PrivateKey.from_private_bytes(_hex32(kp.read_text(), str(kp)))
            if not pp.exists():                                  # the public half can be rebuilt; the private cannot
                write_private(pp, _pub_raw(priv).hex() + "\n")
            return priv
        if not create:
            raise CorruptKey(f"no device key at {kp}")
        priv = ed.Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization as _s  # noqa: PLC0415 - only on the create path
        raw = priv.private_bytes(_s.Encoding.Raw, _s.PrivateFormat.Raw, _s.NoEncryption())
        write_private(kp, raw.hex() + "\n")                      # atomic, 0600 from the first byte
        write_private(pp, _pub_raw(priv).hex() + "\n")
        return priv


def _pub_raw(priv) -> bytes:
    from cryptography.hazmat.primitives import serialization as _s  # noqa: PLC0415
    return priv.public_key().public_bytes(_s.Encoding.Raw, _s.PublicFormat.Raw)


def key_id(pub: bytes) -> str:
    """A short fingerprint of the public key, so a log signed by a replaced key is detectable rather than silent."""
    return hashlib.sha256(pub).hexdigest()[:KEY_ID_LEN]


def public_key(pubkey=None) -> tuple[object | None, str | None]:
    """(public key, key_id) from raw bytes, hex, a file, a key object — or `notebook.pub`. (None, None) if there is
    nothing to verify with: that is reported, never treated as a verdict."""
    ed = _ed()
    raw: bytes | None = None
    if pubkey is None:
        p = pub_path()
        raw = _hex32(p.read_text(), str(p)) if p.exists() else None
    elif isinstance(pubkey, bytes):
        raw = pubkey
    elif isinstance(pubkey, Path):
        raw = _hex32(pubkey.read_text(), str(pubkey))
    elif isinstance(pubkey, str):
        raw = _hex32(pubkey, "the public key given")
    else:
        pub = pubkey                                             # already an Ed25519PublicKey
        return pub, key_id(pub.public_bytes_raw())
    if raw is None:
        return None, None
    return ed.Ed25519PublicKey.from_public_bytes(raw), key_id(raw)


# ---------------------------------------------------------------- signing
def _message(h: str, kid: str) -> bytes:
    """What is signed: the line's hash, bound to the key that signed it, in the ledger's own canonical form.

    The hash already covers the whole line and its `prev`, so signing the hash signs the content and its position in
    the chain. `key_id` is inside the message so a signature cannot be relabelled as another key's work.
    """
    return ledger.canon({"hash": h, "key_id": kid})


def sign_line(row: dict) -> dict:
    """The row plus `sig` (hex) and `key_id`. The row itself is not changed: its `hash` is what gets signed."""
    h = row.get("hash")
    if not isinstance(h, str) or len(h) != 64:
        raise ValueError(f"a line can only be signed once it is hashed; got hash={h!r}")
    priv = device_key()
    kid = key_id(_pub_raw(priv))
    return {**row, "key_id": kid, "sig": priv.sign(_message(h, kid)).hex()}


def _read_sigs(file: Path | None = None) -> tuple[dict[str, dict], list[str]]:
    """{hash: {key_id, sig}} and the complaints about lines that are not usable signatures."""
    p = sig_path(file)
    sigs: dict[str, dict] = {}
    bad: list[str] = []
    if not p.exists():
        return sigs, bad
    for n, line in enumerate(p.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            h, kid, sig = e["hash"], e["key_id"], e["sig"]
            bytes.fromhex(sig)
        except (ValueError, KeyError, TypeError):
            bad.append(f"signature line {n} of {p.name} is not a usable signature")
            continue
        sigs[h] = {"key_id": kid, "sig": sig}
    return sigs, bad


def _append_sig(signed: dict, file: Path | None = None) -> None:
    p = sig_path(file)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write(ledger.canon({k: signed[k] for k in ("hash", "key_id", "sig")}) + b"\n")


# ---------------------------------------------------------------- recording
def _next_seq(f: Path) -> int:
    if not f.exists() or f.stat().st_size == 0:
        return 1
    lines = f.read_bytes().splitlines()
    try:
        last = json.loads(lines[-1]).get("seq")
    except ValueError:
        last = None
    return (last + 1) if isinstance(last, int) else len(lines) + 1


def record(entry: dict, file: Path | None = None) -> dict:
    """What Studio calls instead of `ledger.append`: the same line, chained by the ledger, plus its signature.

    `seq` goes in before the line is hashed, so the position is covered by both the hash and the signature.
    """
    f = file or ledger.path_file()
    with _LOCK:                                          # seq, the line and its signature are one step, or none
        seq = _next_seq(f)
        # `seq` last: a caller's own `seq` must not be able to overwrite the position the notebook counted.
        row = ledger.append({**entry, "seq": seq}, f)    # the ledger's own append, untouched
        signed = sign_line(row)
        _append_sig(signed, f)
    return signed


# ---------------------------------------------------------------- verifying
def _state_of(h: str, sigs: dict, pub, kid: str | None) -> tuple[str, str | None]:
    """(state, the key that signed it) for one line: VALID / UNSIGNED / OTHER_KEY / BAD_SIGNATURE."""
    e = sigs.get(h)
    if e is None:
        return UNSIGNED, None
    if pub is None:
        # Signed, and there is nothing here to check it with. Not "another key": a different unverifiable.
        return NO_KEY, e["key_id"]
    if e["key_id"] != kid:
        return OTHER_KEY, e["key_id"]
    try:
        pub.verify(bytes.fromhex(e["sig"]), _message(h, e["key_id"]))
    except Exception as why:                                  # noqa: BLE001 - InvalidSignature or a malformed sig
        if not isinstance(why, (_invalid_signature(), ValueError)):
            raise
        return BAD, e["key_id"]
    return VALID, e["key_id"]


def verify(file: Path | None = None, pubkey=None) -> dict:
    """{ok, lines, broken_at, why, key_ids}: seq, link and hash continuity AND every signature.

    `ok` is true only for a notebook that was checked end to end. A broken line gives `broken_at`; something that
    could not be checked (unsigned, signed by another key, no public key here) leaves `broken_at` None and says so
    in `why` — the third state never degrades into "bad".
    """
    f = file or ledger.path_file()
    sigs, sig_complaints = _read_sigs(f)
    lines = f.read_bytes().splitlines() if f.exists() else []
    out: dict = {"ok": False, "lines": 0, "broken_at": None, "why": None, "key_ids": {}}

    if not lines and not sigs:
        return {**out, "ok": True, "why": "the notebook is empty"}

    try:
        pub, kid = public_key(pubkey)
    except CorruptKey as why:
        # A public key that is not a key is not a bad notebook: it is a notebook nobody here can check. Reported.
        pub, kid, sig_complaints = None, None, sig_complaints + [f"the public key cannot be read: {why}"]
    out["current_key_id"] = kid
    counts = {UNSIGNED: 0, OTHER_KEY: 0, NO_KEY: 0}
    other_keys: dict[str, int] = {}
    prev, seen = "0" * 64, set()
    n = 0

    for n, line in enumerate(lines, 1):
        out["lines"] = n
        try:
            row = json.loads(line)
        except ValueError:
            return {**out, "broken_at": n, "why": f"line {n} is not valid JSON (a torn tail, or a line rewritten by hand)"}
        if not isinstance(row, dict):
            return {**out, "broken_at": n, "why": f"line {n} is not a ledger line"}
        h = row.pop("hash", None)
        if row.get("prev") != prev:
            return {**out, "broken_at": n, "why": f"line {n} does not link to the line before it (removed or re-ordered)"}
        if ledger.digest(row) != h:
            return {**out, "broken_at": n, "why": f"line {n} does not match its hash (its content was changed)"}
        if isinstance(row.get("seq"), int) and row["seq"] != n:
            return {**out, "broken_at": n, "why": f"line {n} says it is seq {row['seq']} (a line is missing or moved)"}
        state, by = _state_of(h, sigs, pub, kid)
        if state == BAD:
            return {**out, "broken_at": n, "why": f"the signature on line {n} does not match it (key {by})"}
        if state != VALID:
            counts[state] += 1
            if by:
                other_keys[by] = other_keys.get(by, 0) + 1
        else:
            out["key_ids"][kid] = out["key_ids"].get(kid, 0) + 1
        seen.add(h)
        prev = h

    out["key_ids"] = {**out["key_ids"], **{k: v for k, v in other_keys.items() if k not in out["key_ids"]}}
    orphans = sorted(set(sigs) - seen)
    out["unsigned"], out["signed_by_other_key"] = counts[UNSIGNED], counts[OTHER_KEY]
    out["unchecked_no_key"], out["orphan_signatures"] = counts[NO_KEY], len(orphans)

    why = []
    if orphans:
        # A signature for a line that is not in the ledger: the line was removed or the tail was cut.
        why.append(f"{len(orphans)} signature(s) have no line in the ledger (lines removed, or the tail truncated): "
                   + ", ".join(x[:12] for x in orphans[:3]))
    if pub is None:
        why.append(f"there is no public key to verify with (looked for {pub_path().name}): "
                   f"{counts[NO_KEY]} signed line(s) were left unchecked")
    if counts[UNSIGNED]:
        why.append(f"{counts[UNSIGNED]} line(s) carry no signature (written through ledger.append, not notebook.record)")
    if counts[OTHER_KEY] and pub is not None:
        why.append(f"{counts[OTHER_KEY]} line(s) were signed by another key ("
                   + ", ".join(f"{k}: {v}" for k, v in sorted(other_keys.items()))
                   + f"), not by this device's {kid}: pass that key's public half to check them")
    why += sig_complaints
    out["ok"] = not why
    out["why"] = "; ".join(why) if why else None
    return out


# ---------------------------------------------------------------- the entry a reader is handed
def _rows(file: Path | None = None) -> list[dict]:
    f = file or ledger.path_file()
    if not f.exists():
        return []
    rows = []
    for n, line in enumerate(f.read_bytes().splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        row["_line"] = n
        rows.append(row)
    return rows


def _matches(row: dict, want) -> bool:
    """Exact equality only. Never a substring of `detail`: a job id that merely appears in a message is not this run."""
    if isinstance(want, int):
        return row.get("seq") == want or (row.get("seq") is None and row["_line"] == want)
    prov = row.get("provenance") or {}
    args = row.get("args") or {}
    return (row.get("hash") == want or args.get("job_id") == want
            or want in (prov.get("ibm_job_ids") or []) or (row.get("meta") or {}).get("job_id") == want)


def export(job_or_range, file: Path | None = None, pubkey=None) -> dict:
    """The notebook entry for one run: what was asked, the result's sha256, the provenance BioQL itself stated
    (real-or-null, never inferred), the verifier's findings if the row carries them, where the line sits in the chain
    and whether its signature holds.

    `job_or_range` is a seq number, a line hash, a job id — or (first, last) seq for a short range.
    """
    f = file or ledger.path_file()
    rows = _rows(f)
    total = len(rows)
    sigs, _ = _read_sigs(f)
    pub, kid = public_key(pubkey)

    if isinstance(job_or_range, (tuple, list)) and len(job_or_range) == 2:
        lo, hi = job_or_range
        picked = [r for r in rows if lo <= (r.get("seq") or r["_line"]) <= hi]
    else:
        picked = [r for r in rows if _matches(r, job_or_range)]
    if not picked:
        raise LookupError(f"no line in {f.name} matches {job_or_range!r} (searched seq, line hash and job id, exactly)")

    check = verify(f, pubkey)
    entries = [_entry(r, total, sigs, pub, kid, check) for r in picked]
    # `chain_check`, not `chain`: each entry has its own `chain` (its position), and merging the two would hide one.
    out = {"notebook": str(f), "entries": entries, "chain_check": check}
    out["text"] = "\n\n".join(e["text"] for e in entries)
    return out if len(entries) != 1 else {**out, **entries[0], "entries": entries}


def _entry(row: dict, total: int, sigs: dict, pub, kid: str | None, check: dict | None = None) -> dict:
    prov_recorded = isinstance(row.get("provenance"), dict)
    prov = row.get("provenance") if prov_recorded else {}
    state, by = _state_of(row.get("hash", ""), sigs, pub, kid)
    # The signature is over the line's hash, so a valid signature proves nothing about a line whose content no
    # longer matches that hash. Recompute it here: an entry must never render forged parameters under "valid".
    body = {k: v for k, v in row.items() if k not in ("hash", "_line")}
    content_ok = ledger.digest(body) == row.get("hash")
    if not content_ok and state in (VALID, NO_KEY, OTHER_KEY):
        state, by = BAD, by
    seq = row.get("seq") or row["_line"]
    t = row.get("t")
    e = {
        "seq": seq,
        "when": datetime.fromtimestamp(t, timezone.utc).isoformat() if isinstance(t, (int, float)) else None,
        "tool": row.get("tool"), "method": row.get("method"), "path": row.get("path"),
        "by": row.get("by"), "approved_by": row.get("approved_by"),
        "parameters": row.get("args"),
        "state": row.get("state"), "detail": row.get("detail"), "http": row.get("http"),
        "seconds": row.get("seconds"),
        "result_sha256": row.get("result_sha256"),
        # Real-or-null: copied from what BioQL said, null when it said nothing. A row written before provenance was
        # recorded says so, rather than showing three nulls as if BioQL had answered them.
        "provenance": {"recorded": prov_recorded,
                       "is_real_hardware": prov.get("is_real_hardware"),
                       "ibm_job_ids": list(prov.get("ibm_job_ids") or []),
                       "quantum_backends": list(prov.get("quantum_backends") or [])},
        "verification": ({"verdict": row.get("verdict"), "findings": row.get("findings") or []}
                         if ("verdict" in row or "findings" in row) else {"verdict": None, "findings": [],
                                                                         "note": "not recorded on this line"}),
        "chain": {"position": seq, "of": total, "prev": row.get("prev"), "hash": row.get("hash"),
                  "content_matches_hash": content_ok},
        "signature": {"state": state, "key_id": by, "sig": (sigs.get(row.get("hash", "")) or {}).get("sig"),
                      "current_key_id": kid},
        # The whole notebook's verdict travels with the entry: one page that does not need a second document to be
        # read honestly.
        "notebook_ok": None if check is None else check["ok"],
        "notebook_why": None if check is None else check["why"],
    }
    e["text"] = _render(e)
    return e


_SIG_SAYS = {
    VALID: "valid, made by this device's key",
    UNSIGNED: "none on record for this line (it was written through the plain ledger)",
    OTHER_KEY: "made by another key than the one here: it cannot be checked with this public key",
    NO_KEY: "present and UNCHECKED: there is no public key here to check it with",
    BAD: "present and does NOT match this line",
}


def _render(e: dict) -> str:
    p, v, s = e["provenance"], e["verification"], e["signature"]
    hw = {True: "REAL HARDWARE", False: "simulator (BioQL says not real hardware)", None: "not stated by BioQL",
          "mixed": "MIXED: parts of the answer claim hardware and parts deny it"}.get(p["is_real_hardware"],
                                                                                     str(p["is_real_hardware"]))
    findings = v["findings"]
    lines = [
        f"BioQL Studio notebook entry {e['seq']} of {e['chain']['of']}",
        f"  when              {e['when']}   ({e['seconds']}s)",
        f"  tool              {e['tool']}   [{e['method']} {e['path']}]",
        f"  asked by          {e['by']}" + (f", approved by {e['approved_by']}" if e["approved_by"] else ""),
        f"  parameters        {json.dumps(e['parameters'], sort_keys=True, ensure_ascii=False)}",
        f"  outcome           {e['state']}" + (f" — {e['detail']}" if e["detail"] else ""),
        f"  result sha256     {e['result_sha256'] or 'none (no result on this line)'}",
        "  provenance        " + ("as BioQL stated it" if p["recorded"]
                                  else "NOT RECORDED on this line; what follows is unknown, not false"),
        f"    hardware        {hw}",
        f"    IBM job ids     {', '.join(p['ibm_job_ids']) or 'none (real-or-null: never inferred)'}",
        f"    backends        {', '.join(p['quantum_backends']) or 'none stated'}",
        f"  verifier          {v['verdict'] or v.get('note', 'not recorded')}"
        + (f", {len(findings)} finding(s)" if findings else ""),
    ]
    lines += [f"    {f.get('severity')} {f.get('code')} at {f.get('path')}: {f.get('detail')}" for f in findings[:12]]
    lines += [
        f"  chain             line {e['chain']['position']} of {e['chain']['of']}, prev {str(e['chain']['prev'])[:12]}…,"
        f" hash {str(e['chain']['hash'])[:12]}…"
        + ("" if e["chain"]["content_matches_hash"] else "  — THIS LINE DOES NOT MATCH ITS OWN HASH: it was changed"),
        f"  signature         {_SIG_SAYS.get(s['state'], s['state'])}"
        + (f" (key {s['key_id']})" if s["key_id"] else ""),
        "  notebook          " + {True: "verified end to end", False: f"NOT VERIFIED — {e['notebook_why']}",
                                  None: "not checked"}[e["notebook_ok"]],
    ]
    return "\n".join(lines)
