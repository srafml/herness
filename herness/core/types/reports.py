"""09-owned shared data type: `ReportManifest` (design 09 §4; U09-01; R-01, R-02)."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_RUN_ID_RE: Final = r"^run_[0-9A-HJKMNP-TV-Z]{26}$"
_HASH_RE: Final = r"^[0-9a-f]{64}$"
type _FileName = Literal["report.html", "report.md", "report.pdf"]


class ReportManifest(BaseModel):
    """Metadata record of one rendered report (design 09 §4; U09-01)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=_RUN_ID_RE)
    kind: Literal["funding_review", "org_review"]
    build_id: str
    template_version: str
    rendered_at: AwareDatetime
    publishable: bool
    files: dict[_FileName, Annotated[str, Field(pattern=_HASH_RE)]]
    numbers_total: int = Field(ge=0)
    numbers_linked: int = Field(ge=0)
    uncited: list[dict[str, str]] = Field(default_factory=list)
    evidence_entries: int = Field(ge=0)
    warnings: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.numbers_linked > self.numbers_total:
            msg = "numbers_linked > numbers_total"
            raise ValueError(msg)
        if any(set(d) != {"where", "text"} for d in self.uncited):
            msg = "uncited needs where, text keys"
            raise ValueError(msg)
        return self
