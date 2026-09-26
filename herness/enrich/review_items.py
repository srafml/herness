"""Enrichment helpers over impl 02's `review_item` functions (paging, idempotent creation,
open counts) (U03-147 … U03-149, R-08, R-09, design 03 §3.11a).

The `review_item` table and its functions belong to impl 02 (`herness.store.ops.shared`);
this module defines no ops function and issues no SQL of its own. Only the exclusive
`build_pipeline` and `distill` jobs create `label_check` and `mapping_suggestion` items
(section intro, T08-26), and impl 02 runs each lookup-and-insert in one write transaction,
so `create_if_absent` stays idempotent even with a concurrent creator.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Literal

from herness.core.errors import ConfigError
from herness.store.ops import (
    ReviewItem,
    ReviewStatus,
    create_review_item_if_absent,
    list_review_items,
)

_MAX_PAGE_SIZE = 5000
_MAX_KEYS = 8

type _Kind = Literal["label_check", "mapping_suggestion"]


def iter_review_items(
    kind: _Kind,
    status: ReviewStatus,
    *,
    payload_match: Mapping[str, str] | None = None,
    page_size: int = 500,
) -> Iterator[ReviewItem]:
    """Yield every `review_item` of `kind` and `status`, by (`created_at`, `item_id`) (U03-147).

    Pages through impl 02's `list_review_items` in `page_size` chunks; items created during
    the iteration may be seen or missed (callers run inside exclusive jobs). Raises
    ConfigError when `page_size` is outside 1..5,000, or when `payload_match` is invalid
    (impl 02 U02-58); StoreBusy propagates.
    """
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        msg = f"page_size must be 1..{_MAX_PAGE_SIZE}"
        raise ConfigError(msg)
    offset = 0
    while True:
        page = list_review_items(
            kind=kind,
            status=status,
            payload_match=payload_match,
            limit=page_size,
            offset=offset,
        )
        yield from page
        if len(page) < page_size:
            return
        offset += page_size


def _check_create_if_absent(
    payloads: Sequence[Mapping[str, object]],
    match_keys: tuple[str, ...],
    blocking_statuses: tuple[ReviewStatus, ...],
    scope: Mapping[str, str],
) -> None:
    """U03-148 preconditions this module owns (impl 02 checks the rest at call time)."""
    if not match_keys:
        msg = "match_keys must be non-empty"
        raise ConfigError(msg)
    if not blocking_statuses:
        msg = "blocking_statuses must be non-empty"
        raise ConfigError(msg)
    if len(set(match_keys) | set(scope)) > _MAX_KEYS:
        msg = f"match_keys and scope together must name at most {_MAX_KEYS} keys"
        raise ConfigError(msg)
    for payload in payloads:
        if not all(k in payload for k in match_keys):
            msg = "a payload is missing a match_keys field"
            raise ConfigError(msg)
        if any(payload.get(k) != v for k, v in scope.items()):
            msg = "a payload does not equal the given scope"
            raise ConfigError(msg)


def create_if_absent(
    kind: _Kind,
    payloads: Sequence[Mapping[str, object]],
    *,
    match_keys: tuple[str, ...],
    blocking_statuses: tuple[ReviewStatus, ...],
    scope: Mapping[str, str] | None = None,
    now: datetime,
) -> tuple[int, int]:
    """Create a `review_item` per payload unless a blocking one already matches (U03-148).

    Returns `(created, suppressed)`. Within one call, payloads sharing a `match_keys` tuple
    (values compared as JSON scalars) after the first are suppressed without a store lookup.
    Raises ConfigError when `match_keys` or `blocking_statuses` is empty, when `match_keys`
    and `scope` together name more than 8 keys, when a payload is missing a `match_keys`
    field or does not equal `scope`, or from impl 02; SchemaViolation or StoreBusy from
    impl 02 propagate. Payloads carry no ticket text (TH03-03); created items are always
    `pending` (TH03-10).
    """
    scope_map = scope if scope is not None else {}
    _check_create_if_absent(payloads, match_keys, blocking_statuses, scope_map)
    combined_keys = match_keys + tuple(scope_map)
    created = 0
    suppressed = 0
    seen: set[tuple[object, ...]] = set()
    for payload in payloads:
        key = tuple(payload.get(k) for k in match_keys)
        if key in seen:
            suppressed += 1
            continue
        seen.add(key)
        _, was_created = create_review_item_if_absent(
            kind,
            payload,
            match_keys=combined_keys,
            blocking_statuses=blocking_statuses,
            now=now,
        )
        if was_created:
            created += 1
            # T08-05: herness_enrich_review_items_total{kind, purpose} += 1 per created item
        else:
            suppressed += 1
    return created, suppressed


def open_label_counts(*, qsv: str, purposes: frozenset[str]) -> dict[str, int]:
    """Pending `label_check` counts per question, for `qsv` and `purposes` (U03-149).

    Questions without a pending item are absent; callers read with a default of 0.
    Raises as `iter_review_items`.
    """
    counts: dict[str, int] = {}
    for purpose in purposes:
        match = {"question_set_version": qsv, "purpose": purpose}
        for item in iter_review_items("label_check", "pending", payload_match=match):
            question = str(item.payload["question"])
            counts[question] = counts.get(question, 0) + 1
    return counts
