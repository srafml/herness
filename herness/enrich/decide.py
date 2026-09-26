"""Decider protocol, primary rules, pure gate and resolution reference (impl 03 §2).

T03-11 adds the `Decider` protocol (U03-48); T03-17 adds `primary_decider_for`,
`chain_after`, `gate`, `resolve_pair`, `Resolution` and `CachedAnswer` (design 03 §5.7,
§5.9, §4.1).

Deviation from the literal U03-70/U03-71 signatures (recorded in the T03-17 report): after
the R-76 settings split, `deciders.*.enabled` lives in `DecidersSettings`
(`config/models.yaml`), not in `DecisionsConfig`. Both functions need to know which of
`openjev`/`jev` are enabled, so both take a keyword-only `deciders: DecidersSettings`
alongside `cfg: DecisionsConfig`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from herness.core.types import DecisionInput, DecisionOutput, Question, QuestionSet
from herness.enrich.settings import DecidersSettings, DecisionsConfig

__all__ = [
    "CachedAnswer",
    "Decider",
    "Resolution",
    "chain_after",
    "gate",
    "primary_decider_for",
    "resolve_pair",
]


@runtime_checkable
class Decider(Protocol):
    """Interchangeable classification backend (U03-48, spec 00 §6).

    `decide` returns one `DecisionOutput` per input, in input order; deciders never
    calibrate and never gate. `version` is stable for the life of the instance, except
    `JevHostedDecider`, which fixes it on the first `health()` (U03-56). `health()` raises
    `ModelUnavailable` on any failure. Implementations state their own concurrency.
    """

    name: str
    version: str

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """Classify `items` against `questions`; one output per input, in input order."""
        ...

    def health(self) -> None:
        """Check the backend is usable. Raises ModelUnavailable on any failure."""
        ...


def primary_decider_for(
    q: Question,
    *,
    cfg: DecisionsConfig,
    laya_accepted: frozenset[str] | None,
    deciders: DecidersSettings,
) -> Literal["laya", "openjev", "jev", "llm"]:
    """Name q's primary decider for this build (U03-70, design 03 §5.7 step 3).

    Only human-accepted questions use the student (TH03-04): `laya` is returned only when
    `laya_accepted` names `q.id`. Never returns a disabled backend.
    """
    if "jev" in cfg.escalation_chain and deciders.jev.enabled:
        teacher: Literal["openjev", "jev", "llm"] = "jev"
    elif deciders.openjev.enabled:
        teacher = "openjev"
    else:
        teacher = "llm"
    override = next((qc.primary_decider for qc in cfg.questions if qc.id == q.id), None)
    wanted = override if override is not None else cfg.primary_decider
    if wanted == "laya":
        return "laya" if laya_accepted is not None and q.id in laya_accepted else teacher
    if wanted in ("openjev", "jev"):
        enabled = deciders.openjev.enabled if wanted == "openjev" else deciders.jev.enabled
        return wanted if enabled else teacher
    return "llm"


def chain_after(
    primary: str, *, cfg: DecisionsConfig, deciders: DecidersSettings
) -> tuple[str, ...]:
    """Escalation members after `primary`, in order (U03-71).

    `cfg.escalation_chain` with `openjev` replaced by `jev` when `jev` is enabled (design 03
    §5.5 comment), disabled members removed, `llm` always last, and every member at or
    before `primary`'s position removed. For `primary == "laya"` the whole chain is
    returned. No duplicates; never contains `primary`.
    """

    def _swap(name: str) -> str:
        return "jev" if name == "openjev" and deciders.jev.enabled else name

    swapped = tuple(_swap(name) for name in cfg.escalation_chain)
    canonical_primary = _swap(primary)
    if primary == "laya" or canonical_primary not in swapped:
        after: tuple[str, ...] = swapped
    else:
        after = swapped[swapped.index(canonical_primary) + 1 :]
    enabled = {"openjev": deciders.openjev.enabled, "jev": deciders.jev.enabled}
    kept = [name for name in after if name != canonical_primary and enabled.get(name, True)]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in kept:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    if "llm" in ordered:
        ordered = [name for name in ordered if name != "llm"] + ["llm"]
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    """A decider's current-version cache row for one (record, question) (U03-74).

    `p_cal` is the calibrated probability of `answer`; `agreement` is set only for the
    `ensemble` row (design 03 §5.9).
    """

    answer: str
    p_cal: float
    decided_at: datetime
    version: str
    agreement: float | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of resolving one (record, question) (U03-72, design 03 §4.1).

    `status == "final"` iff `answer`, `probability`, `decider`, `decider_version` and
    `decided_at` are all set.
    """

    status: Literal["final", "queue", "out_of_scope"]
    answer: str | None
    probability: float | None
    decider: str | None
    decider_version: str | None
    escalated: bool
    decided_at: datetime | None
    agreement: float | None
    review_status: Literal["none", "pending", "confirmed", "corrected"]


def gate(p_calibrated: float, threshold: float) -> bool:
    """Calibrated-probability gate (U03-73): `p_calibrated >= threshold`."""
    return p_calibrated >= threshold


@dataclass(frozen=True, slots=True)
class _Machine:
    """Internal: the machine result `m` of `resolve_pair` step 1, before human override."""

    decider: str
    decider_version: str
    answer: str
    probability: float
    decided_at: datetime
    escalated: bool
    agreement: float | None


def _machine_result(
    rows: Mapping[str, CachedAnswer], primary: str, chain: tuple[str, ...], threshold: float
) -> _Machine | None:
    if "ensemble" in rows:
        ens = rows["ensemble"]
        laya = rows.get("laya")
        escalated = laya is None or ens.answer != laya.answer
        return _Machine("ensemble", ens.version, ens.answer, ens.p_cal, ens.decided_at,
                        escalated, ens.agreement)  # fmt: skip
    primary_row = rows.get(primary)
    if primary_row is not None and gate(primary_row.p_cal, threshold):
        return _Machine(primary, primary_row.version, primary_row.answer, primary_row.p_cal,
                        primary_row.decided_at, False, None)  # fmt: skip
    for member in chain:
        row = rows.get(member)
        if row is not None:
            return _Machine(member, row.version, row.answer, row.p_cal, row.decided_at, True, None)
    return None


def _final(
    m: _Machine, *, review_status: Literal["none", "pending", "confirmed", "corrected"]
) -> Resolution:
    return Resolution(
        status="final",
        answer=m.answer,
        probability=m.probability,
        decider=m.decider,
        decider_version=m.decider_version,
        escalated=m.escalated,
        decided_at=m.decided_at,
        agreement=m.agreement,
        review_status=review_status,
    )


def resolve_pair(  # noqa: PLR0913 - U03-74's signature is verbatim binding (7 kw-only params)
    *,
    human: tuple[str, datetime] | None,
    rows: Mapping[str, CachedAnswer],
    primary: str,
    chain: tuple[str, ...],
    threshold: float,
    in_scope: bool,
    pending_review: bool,
) -> Resolution:
    """Resolve one (record, question) from its candidate rows (U03-74).

    Reference implementation of `resolve_decisions.sql` (design 03 §5.7 step 1, §5.9
    precedence, §4.1 column rules). `rows` must hold only rows with the current
    fingerprint and each decider's current version (the ensemble row only for the current
    ensemble version); human corrections override models (LLM09 control).
    """
    m = _machine_result(rows, primary, chain, threshold)
    if human is not None:
        human_answer, human_at = human
        if m is not None and m.answer == human_answer:
            return _final(m, review_status="confirmed")
        return Resolution(
            status="final",
            answer=human_answer,
            probability=1.0,
            decider="human",
            decider_version="human",
            escalated=False,
            decided_at=human_at,
            agreement=None,
            review_status="corrected" if m is not None else "confirmed",
        )
    if m is not None:
        return _final(m, review_status="pending" if pending_review else "none")
    status: Literal["out_of_scope", "queue"] = (
        "out_of_scope" if not in_scope and primary not in rows else "queue"
    )
    return Resolution(
        status=status,
        answer=None,
        probability=None,
        decider=None,
        decider_version=None,
        escalated=False,
        decided_at=None,
        agreement=None,
        review_status="none",
    )
