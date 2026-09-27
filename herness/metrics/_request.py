"""Request checks behind `herness.metrics.compute.validate_metric_request` (impl 04 U04-51).

Private split of `herness.metrics.compute` (module budget). Messages name keys and rules,
never caller values (TH04-11); every accepted value is later bound as a typed parameter.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from herness.core.errors import ToolInputError
from herness.metrics.settings import FilterKey, MetricDef

__all__ = ["MAX_ENTITY_IDS", "MAX_FILTER_VALUES", "normalize_entity_ids", "normalize_filters"]

MAX_ENTITY_IDS: Final = 500
MAX_FILTER_VALUES: Final = 500
MAX_ID_CHARS: Final = 256
_PRIORITIES: Final = range(1, 6)
_ID_FILTERS: Final[frozenset[str]] = frozenset({"service_id", "team_id", "org_id", "cluster_id"})
_ENUM_FILTERS: Final[Mapping[str, tuple[str, ...]]] = {
    "severity": ("critical", "major", "minor", "warning", "info"),
    "change_type": ("standard", "normal", "emergency"),
    "work_item_type": ("initiative", "epic", "feature", "story", "bug", "task", "subtask"),
}


def _is_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_ID_CHARS and value.isprintable()


def normalize_entity_ids(entity_ids: Sequence[str] | None) -> list[str] | None:
    """U04-51 step 4: None, or the sorted unique IDs (1-500 printable IDs of 1-256 chars)."""
    if entity_ids is None:
        return None
    if isinstance(entity_ids, str | bytes) or not isinstance(entity_ids, Sequence):
        msg = "entity_ids must be null or a list of IDs"
        raise ToolInputError(msg)
    if not entity_ids:
        msg = "entity_ids must be null or non-empty"
        raise ToolInputError(msg)
    if len(entity_ids) > MAX_ENTITY_IDS:
        msg = f"at most {MAX_ENTITY_IDS} entity_ids"
        raise ToolInputError(msg)
    if not all(_is_id(i) for i in entity_ids):
        msg = f"entity_ids items must be 1-{MAX_ID_CHARS} printable characters"
        raise ToolInputError(msg)
    return sorted(set(entity_ids))


def _item_ok(key: str, item: object) -> bool:
    if key == "priority":
        return isinstance(item, int) and not isinstance(item, bool) and item in _PRIORITIES
    if key in _ID_FILTERS:
        return _is_id(item)
    return isinstance(item, str) and item in _ENUM_FILTERS[key]


def _item_rule(key: str) -> str:
    if key == "priority":
        return "integers 1-5"
    if key in _ID_FILTERS:
        return f"1-{MAX_ID_CHARS} printable characters"
    return "one of " + ", ".join(_ENUM_FILTERS[key])


def _filter_values(key: str, value: object) -> list[object]:
    """One filter's values as a list; messages name the key and the rule, never the values."""
    items: list[object] = list(value) if isinstance(value, list | tuple) else [value]
    if not 1 <= len(items) <= MAX_FILTER_VALUES:
        msg = f"filter {key} needs 1-{MAX_FILTER_VALUES} values"
        raise ToolInputError(msg)
    if not all(_item_ok(key, item) for item in items):
        msg = f"filter {key} items must be {_item_rule(key)}"
        raise ToolInputError(msg)
    return items


def normalize_filters(metric: MetricDef, filters: object) -> dict[FilterKey, list[object]]:
    """U04-51 step 5: allowlisted keys, values as 1-500 item lists checked per U04-28."""
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        msg = "filters must be null or a mapping"
        raise ToolInputError(msg)
    allowed: dict[str, FilterKey] = {str(k): k for k in metric.filters}
    out: dict[FilterKey, list[object]] = {}
    for key, value in filters.items():
        known = allowed.get(key) if isinstance(key, str) else None
        if known is None:
            shown = key[:64] if isinstance(key, str) else type(key).__name__
            names = ", ".join(metric.filters)
            msg = f"filter {shown} not allowed for {metric.name}; allowed: {names}"
            raise ToolInputError(msg)
        out[known] = _filter_values(known, value)
    return out
