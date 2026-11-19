"""The three states of an external fact, as BioQL's own invariant (CLAUDE.md, 2026-11-18) defines them.

RESOLVED     the source answered and the fact is X (with its source).
ABSENT       the source answered and does not have it: that IS information.
UNRESOLVABLE we could not ask (network, timeout, permission, format): that is NOT information about the fact.

The third never degrades into the second. The type makes that degradation hard to write, not only forbidden:
`bool(res)` raises (so `if res:` cannot fold three states into two), `res.value` raises unless resolved, and
`res.or_default(d)` accepts the default only for ABSENT and raises for UNRESOLVABLE.

This is a copy of the semantics of BioQL's `external_fact.py`, written fresh: BioQL Studio never imports BioQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RESOLVED, ABSENT, UNRESOLVABLE = "RESOLVED", "ABSENT", "UNRESOLVABLE"
STATES = (RESOLVED, ABSENT, UNRESOLVABLE)


class Unresolvable(RuntimeError):
    """Raised when someone asks an UNRESOLVABLE result for a value or a default."""


@dataclass(frozen=True)
class Resolution:
    state: str
    source: str
    detail: str = ""
    _value: Any = field(default=None, repr=False)
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")
        if self.state != RESOLVED and self._value is not None:
            raise ValueError("only a RESOLVED result carries a value")

    def __bool__(self) -> bool:  # pragma: no cover - the raise is the behaviour
        raise TypeError("a Resolution has three states; test .state (or .resolved / .absent / .unresolvable), not truth")

    @property
    def resolved(self) -> bool:
        return self.state == RESOLVED

    @property
    def absent(self) -> bool:
        return self.state == ABSENT

    @property
    def unresolvable(self) -> bool:
        return self.state == UNRESOLVABLE

    @property
    def value(self) -> Any:
        if self.state != RESOLVED:
            raise Unresolvable(f"{self.state} from {self.source}: {self.detail}") if self.state == UNRESOLVABLE \
                else LookupError(f"ABSENT from {self.source}: {self.detail}")
        return self._value

    def or_default(self, default: Any) -> Any:
        if self.state == RESOLVED:
            return self._value
        if self.state == ABSENT:
            return default
        raise Unresolvable(f"UNRESOLVABLE from {self.source}: {self.detail} (a default would hide that we could not ask)")

    def as_dict(self) -> dict:
        out = {"state": self.state, "source": self.source, "detail": self.detail, **({"meta": self.meta} if self.meta else {})}
        if self.state == RESOLVED:
            out["value"] = self._value
        return out


def resolved(value: Any, source: str, detail: str = "", **meta: Any) -> Resolution:
    if value is None:
        raise TypeError("a RESOLVED fact needs a value; use absent() when the source has nothing")
    return Resolution(RESOLVED, source, detail, value, dict(meta))


def absent(source: str, detail: str = "", **meta: Any) -> Resolution:
    return Resolution(ABSENT, source, detail, None, dict(meta))


def unresolvable(source: str, detail: str = "", **meta: Any) -> Resolution:
    return Resolution(UNRESOLVABLE, source, detail, None, dict(meta))
