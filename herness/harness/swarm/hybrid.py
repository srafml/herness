"""Hybrid evidence pack and run-local pseudonyms (impl 06 U06-123, U06-124; design 06 §5.12).

Off-network Skeptic and Writer tasks get an evidence pack instead of the local task input: no
``evidence.result_sample``, ticket text, ``enrich.text_redacted``, ``prior_context`` or
``inputs.notes`` (TH06-06, LLM02). Every outbound string is pseudonymized with the run-local
``Pseudonymizer`` and then passed through the shared redactor ``redact_text`` (fail closed). The
reverse map (``Pseudonymizer.mapping``) stays with the in-process caller and is never packed.
This module builds no LLM or HTTP client; ``count_tokens`` (R-17) is its only outside call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from herness.core.errors import BudgetExceeded, NotFound
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.redact import RedactionFailed, redact_text
from herness.core.types import Challenge, Finding, Message, TaskSpec, TextPart
from herness.harness.llm.tokens import count_tokens

if TYPE_CHECKING:
    from herness.harness.llm.settings import ClientConfig

__all__ = ["PACK_HEADROOM_TOKENS", "Pseudonymizer", "build_evidence_pack"]

PACK_HEADROOM_TOKENS: Final = 8_000  # U06-123 step 2: room for the role prompt and the output
_MAX_PART_CHARS: Final = 200_000  # TextPart.text max_length: a longer pack cannot be sent
_ID_KEYS: Final = frozenset({"target_id", "entity_id", "row_key"})  # U06-124 step 3
_PORTFOLIO_LISTS: Final = ("rows", "selected", "query_ids")

_log = get_logger("harness.swarm")


def _whole_token_re(strings: Sequence[str]) -> re.Pattern[str] | None:
    """One alternation, longest string first, matching only whole tokens; None when empty."""
    if not strings:
        return None
    ordered = sorted(strings, key=lambda s: (-len(s), s))
    body = "|".join(re.escape(s) for s in ordered)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)")


class Pseudonymizer:
    """Run-local pseudonyms ``<entity_type>_<nnn>`` for entity ids and names (U06-124)."""

    def __init__(self, entities: Sequence[tuple[str, str, str | None]]) -> None:
        self._tokens: dict[tuple[str, str], str] = {}
        self._entries: dict[str, tuple[str, str, str | None]] = {}
        self._forward: dict[str, str] = {}  # raw id or name -> token; the first entity wins
        counts: dict[str, int] = {}
        for entity_type, entity_id, name in entities:
            if (entity_type, entity_id) in self._tokens:
                continue
            counts[entity_type] = counts.get(entity_type, 0) + 1
            token = f"{entity_type}_{counts[entity_type]:03d}"
            self._tokens[entity_type, entity_id] = token
            self._entries[token] = (entity_type, entity_id, name)
            for raw in (entity_id, name):
                if raw:
                    self._forward.setdefault(raw, token)
        self._forward_re = _whole_token_re(list(self._forward))
        self._reverse_re = _whole_token_re(list(self._entries))

    def token(self, entity_type: str, entity_id: str) -> str:
        """The pseudonym of one entity; ``NotFound`` when the entity was not given."""
        try:
            return self._tokens[entity_type, entity_id]
        except KeyError:
            msg = "entity has no pseudonym"
            raise NotFound(msg, entity_type=entity_type) from None

    def pseudonymize(self, text: str) -> str:
        """``text`` with every id and name replaced by its token (longest first, whole tokens)."""
        if self._forward_re is None:
            return text
        return self._forward_re.sub(lambda m: self._forward[m.group(0)], text)

    def restore(self, text: str) -> str:
        """Prose: each token back to the entity name, or to its id when it has none."""
        return self._restore(text, ids=False)

    def restore_obj(self, obj: object) -> object:
        """Walk dicts and lists: id-valued fields (``_ID_KEYS``) restore to ids, prose to names."""
        return self._walk(obj, ids=False)

    def mapping(self) -> dict[str, dict[str, str]]:
        """``{token: {"entity_type", "entity_id"[, "name"]}}`` for the task checkpoint (U06-140)."""
        out: dict[str, dict[str, str]] = {}
        for token, (entity_type, entity_id, name) in self._entries.items():
            entry = {"entity_type": entity_type, "entity_id": entity_id}
            if name is not None:
                entry["name"] = name
            out[token] = entry
        return out

    def _restore(self, text: str, *, ids: bool) -> str:
        if self._reverse_re is None:
            return text

        def back(match: re.Match[str]) -> str:
            _type, entity_id, name = self._entries[match.group(0)]
            return entity_id if ids or not name else name

        return self._reverse_re.sub(back, text)

    def _walk(self, obj: object, *, ids: bool) -> object:
        if isinstance(obj, str):
            return self._restore(obj, ids=ids)
        if isinstance(obj, Mapping):
            return {k: self._walk(v, ids=ids or k in _ID_KEYS) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self._walk(item, ids=ids) for item in obj]
        return obj


# --- U06-123 evidence pack ----------------------------------------------------------------------


class _Unredactable(Exception):  # noqa: N818 - internal control flow, never escapes this module
    """A string the shared redactor refused; its item is left out of the pack."""


def _clean(pseudo: Pseudonymizer, text: str) -> str:
    """Pseudonymize, then redact through the shared path; refusal raises ``_Unredactable``."""
    out = redact_text(pseudo.pseudonymize(text))
    if out is None:
        raise _Unredactable
    return out


def _scrub(pseudo: Pseudonymizer, value: object) -> object:
    """``value`` with every string cleaned; dicts and lists walked, other scalars kept."""
    if isinstance(value, str):
        return _clean(pseudo, value)
    if isinstance(value, Mapping):
        return {str(k): _scrub(pseudo, v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(pseudo, item) for item in value]
    return value


def _kept(pseudo: Pseudonymizer, values: object) -> list[object]:
    """The scrubbed items of a list; an item that cannot be redacted is dropped."""
    out: list[object] = []
    for value in values if isinstance(values, list | tuple) else ():
        try:
            out.append(_scrub(pseudo, value))
        except _Unredactable:
            continue
    return out


def _challenges(pseudo: Pseudonymizer, challenges: Sequence[Challenge]) -> list[object]:
    """Per challenge: round, verdict and the checks that carry a note (U06-123 step 1)."""
    return [
        {
            "round": ch.round,
            "verdict": ch.verdict,
            "checks": [
                {"check": c.check, "result": c.result, "note": _clean(pseudo, c.note)}
                for c in ch.checks
                if c.note
            ],
        }
        for ch in challenges
    ]


def _finding_item(pseudo: Pseudonymizer, f: Finding) -> dict[str, object] | None:
    """One pack finding; None (dropped, fail closed) for an unknown entity or unredactable text."""
    try:
        numbers = [
            n.model_dump(mode="json") | {"row_key": _scrub(pseudo, n.row_key)} for n in f.numbers
        ]
        return {
            "finding_id": f.finding_id,
            "entity_type": f.entity_type,
            "entity_id": pseudo.token(f.entity_type, f.entity_id),
            "claim": _clean(pseudo, f.claim),
            "numbers": numbers,
            "confidence": f.confidence,
            "challenges": _challenges(pseudo, f.challenge),
        }
    except (NotFound, _Unredactable) as exc:
        _log.warning(
            "harness.hybrid.finding_dropped", finding_id=f.finding_id, reason=type(exc).__name__
        )
        return None


def _portfolio(pseudo: Pseudonymizer, value: object) -> dict[str, object]:
    """Scenario, rows, selected ids and query ids; ``custom`` results are not packed."""
    src = value if isinstance(value, Mapping) else {}
    scenario = _kept(pseudo, [src.get("scenario") or ""])
    return {"scenario": scenario[0] if scenario else ""} | {
        key: _kept(pseudo, src.get(key)) for key in _PORTFOLIO_LISTS
    }


def _dq_names(pseudo: Pseudonymizer, rows: object) -> list[object]:
    """The ``check_name`` of each DQ warning row."""
    names = [
        row["check_name"]
        for row in (rows if isinstance(rows, list | tuple) else ())
        if isinstance(row, Mapping) and "check_name" in row
    ]
    return _kept(pseudo, names)


def _over(cfg: ClientConfig, pack: dict[str, object], limit: int) -> bool:
    """Whether the pack's canonical JSON, as one user text part, counts above ``limit``."""
    text = canonical_json(pack)
    if len(text) > _MAX_PART_CHARS:
        return True
    message = Message(role="user", parts=[TextPart(text=text)])
    return count_tokens(cfg, [message], [], [])[0] > limit


def build_evidence_pack(
    spec: TaskSpec,
    *,
    findings: Sequence[Finding],
    inp: Mapping[str, object],
    pseudo: Pseudonymizer,
    max_tokens: int,
    cfg: ClientConfig,
) -> dict[str, object]:
    """Input of an off-network Skeptic or Writer task (U06-123).

    Only the items listed in U06-123 step 1 are read from ``inp``; ``spec.inputs`` is not read.
    Findings are dropped from the end while the pack counts above ``max_tokens -
    PACK_HEADROOM_TOKENS``; the count is taken on the final pseudonymized and redacted pack
    with ``cfg``'s tokenizer. A pack over the cap with no finding left raises ``BudgetExceeded``;
    an objective the redactor refuses raises ``RedactionFailed``. No text is in either message.
    """
    try:
        objective = _clean(pseudo, spec.objective)
    except _Unredactable:
        msg = "objective redaction failed"
        raise RedactionFailed(msg, task_id=spec.task_id) from None
    items = [item for f in findings if (item := _finding_item(pseudo, f)) is not None]
    pack: dict[str, object] = {
        "objective": objective,
        "findings": items,
        "portfolio": _portfolio(pseudo, inp.get("portfolio")),
        "levers": _kept(pseudo, inp.get("levers")),
        "dq_checks": _dq_names(pseudo, inp.get("dq_warnings")),
        "unconfirmed_weights": _kept(pseudo, inp.get("unconfirmed_weights")),
    }
    limit = max_tokens - PACK_HEADROOM_TOKENS
    while _over(cfg, pack, limit):
        if not items:
            msg = "evidence pack exceeds max_input_tokens_per_call"
            raise BudgetExceeded(msg, task_id=spec.task_id, max_tokens=max_tokens)
        items.pop()
    return pack
