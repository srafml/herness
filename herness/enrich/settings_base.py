"""Shared pydantic primitives of the impl 03 config models (``settings.py``, ``settings_link.py``).

Imports only the standard library and pydantic (R-03). ``_Model`` is strict, frozen, forbids
extra keys and hides inputs in errors (TH03-14); cross-field rules are ``(holds, message)`` pairs.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Final, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

Rules = Iterator[tuple[bool, str]]

_MODEL_CONFIG: Final = ConfigDict(
    extra="forbid", strict=True, frozen=True, allow_inf_nan=False, hide_input_in_errors=True
)


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def _as_tuple(value: object) -> object:
    """YAML sequences arrive as lists; strict tuple fields accept them as tuples."""
    return tuple(value) if isinstance(value, list) else value


_Tuple = BeforeValidator(_as_tuple)
_Fraction = Annotated[float, Field(ge=0, le=1)]
_Positive = Annotated[float, Field(gt=0)]


class _Model(BaseModel):
    model_config = _MODEL_CONFIG

    def _rules(self) -> Rules:
        """Cross-field rules as ``(holds, message)`` pairs; the first broken one is raised."""
        return iter(())

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        for holds, msg in self._rules():
            _require(holds, msg)
        return self
