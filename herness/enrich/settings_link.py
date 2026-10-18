"""``change_link`` section of ``config/decisions.yaml`` (U03-09; T03-41 cap and markers).

Re-exported by ``herness.enrich.settings``. Imports only the standard library, pydantic and the
sibling ``settings_base`` (R-03).
"""

from __future__ import annotations

import re
from typing import Annotated, Final

from pydantic import Field

from herness.enrich.settings_base import Rules, _Fraction, _Model, _Positive, _Tuple

__all__ = ["DEFAULT_CORROBORATION_MARKERS", "ChangeLinkSettings"]

# T03-41: temporal cue + change noun (the change_caused question's own definition), EN and ES
_CHANGE_NOUN: Final = r"(release|deployment|deploy|rollout|patch|hotfix|upgrade|change|version)"
_CAMBIO_NOUN: Final = r"(despliegue|cambio|parche|actualizaci[oó]n|versi[oó]n)"
DEFAULT_CORROBORATION_MARKERS: Final = (
    rf"(?i)\b(after|following|since|post-)\s*(the\s+|a\s+|this\s+)?{_CHANGE_NOUN}\b",
    rf"(?i)\b(tras|despu[eé]s\s+(de|del)|desde)\s+(el\s+|la\s+)?{_CAMBIO_NOUN}\b",
)


def _valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


class ChangeLinkSettings(_Model):
    before_h: _Positive = 72.0
    after_h: _Positive = 1.0
    tau_h: float = Field(default=12.0, gt=0, le=8760)  # 1 year; keeps SQL slack float-safe (T03-40)
    ci_weight: _Fraction = 1.0
    service_weight: _Fraction = 0.6
    min_score: _Fraction = 0.30
    top_n: int = Field(default=3, ge=1, le=20)
    use_decider: bool = True
    decider_band: Annotated[tuple[_Fraction, _Fraction], _Tuple] = (0.30, 0.70)
    decider_max_pairs: int = Field(default=30_000, ge=0, le=1_000_000)
    window_only_cap: float = Field(default=0.45, ge=0, lt=1)  # T03-41
    corroboration_markers: Annotated[tuple[str, ...], _Tuple] = DEFAULT_CORROBORATION_MARKERS

    def _rules(self) -> Rules:
        ascending = self.decider_band[0] < self.decider_band[1]
        yield ascending, "change_link.decider_band must be ascending"
        yield self.min_score <= self.window_only_cap, "change_link.window_only_cap < min_score"
        yield all(_valid_regex(p) for p in self.corroboration_markers), "change_link.markers: regex"
