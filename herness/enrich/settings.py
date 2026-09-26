"""Config models of impl 03 (U03-09 … U03-11, U03-150, U03-151); keys and rules: impl 03 §9.

``DecisionsConfig`` is the root of ``config/decisions.yaml``; ``DecidersSettings`` is the
``deciders`` section of ``config/models.yaml`` (R-76). Imports only stdlib, pydantic,
``herness.core.types`` and ``herness.core.errors`` (R-03). Secrets are ``secret:`` references
only (R-72); inputs are hidden in errors (TH03-14); messages name keys, never values.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from herness.core.types import Entity, Question, QuestionSet, QuestionType

# T10-12: the composition root registers owner validator ``enrich.deciders`` with
# herness.core.config_validate.register_owner_validator as
# ``lambda cfg, *, offline: check_decider_refs(cfg.decisions, cfg.models.deciders)`` (R-71).

DeciderName = Literal["laya", "openjev", "jev", "llm"]
EscalationName = Literal["openjev", "jev", "llm"]
Rules = Iterator[tuple[bool, str]]

_MODEL_CONFIG: Final = ConfigDict(
    extra="forbid", strict=True, frozen=True, allow_inf_nan=False, hide_input_in_errors=True
)
# `SecretRefStr` pattern text of impl 10, declared locally (settings never import secrets, R-72)
_SECRET_REF_RE: Final = r"^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$"  # noqa: S105 - a pattern
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
_PAIR_QUESTION: Final = "change_caused_pair"
_WEIGHT_TOL: Final = 1e-6


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def _as_tuple(value: object) -> object:
    """YAML sequences arrive as lists; strict tuple fields accept them as tuples."""
    return tuple(value) if isinstance(value, list) else value


_Tuple = BeforeValidator(_as_tuple)
_Fraction = Annotated[float, Field(ge=0, le=1)]
_Positive = Annotated[float, Field(gt=0)]
_Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_Role = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
_SecretRef = Annotated[str, StringConstraints(pattern=_SECRET_REF_RE)]
_Abbreviation = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{1,16}$")]
_Samples = Annotated[int, Field(ge=1, le=32)] | None


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


class AcceptanceCriteria(_Model):
    """Gate thresholds for a question type or one question (U03-11)."""

    min_accuracy: _Fraction | None = None
    min_macro_f1: _Fraction | None = None
    max_ece: _Fraction | None = None
    min_coverage: _Fraction | None = None
    max_gap_to_teacher: _Fraction | None = None
    min_within_one: _Fraction | None = None
    max_mae: Annotated[float, Field(ge=0, le=3)] | None = None

    def _rules(self) -> Rules:
        is_set = any(value is not None for value in self.model_dump().values())
        yield is_set, "acceptance: at least one criterion must be set"


class AcceptanceDefaults(_Model):
    """``acceptance.choice|bool|score`` per-type defaults."""

    choice: AcceptanceCriteria = AcceptanceCriteria(
        min_accuracy=0.80, min_macro_f1=0.60, max_ece=0.05, min_coverage=0.70,
        max_gap_to_teacher=0.02)  # fmt: skip
    bool_: AcceptanceCriteria = Field(alias="bool", default=AcceptanceCriteria(
        min_accuracy=0.88, max_ece=0.05, min_coverage=0.75, max_gap_to_teacher=0.02))  # fmt: skip
    score: AcceptanceCriteria = AcceptanceCriteria(
        max_mae=0.45, min_within_one=0.92, max_ece=0.06, min_coverage=0.65)  # fmt: skip


class QuestionConfig(_Model):
    """One ``questions:`` entry: the ``Question`` fields but ``fingerprint``, plus overrides."""

    id: str
    type: QuestionType
    instructions: str
    options: dict[str, str] | None = None
    options_source: Literal["static", "core.team", "core.service"] = "static"
    levels: Annotated[tuple[str, str, str, str] | None, _Tuple] = None
    applies_to: Annotated[tuple[Entity, ...], _Tuple] = ("incident",)
    threshold: float
    scoring_use: bool = True
    acceptance: AcceptanceCriteria | None = None
    primary_decider: DeciderName | None = None

    def _rules(self) -> Rules:
        return _check_question_fields(self)


def _check_question_fields(question: QuestionConfig) -> Rules:
    """Apply the ``Question`` rules (U03-02) by building one from the shared fields."""
    try:
        Question.model_validate(question.model_dump(exclude={"acceptance", "primary_decider"}))
    except ValidationError as exc:
        yield False, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


class EmbeddingSettings(_Model):
    model: _Name = "BAAI/bge-m3"
    path: _Name = "data/models/bge-m3/<rev>/"
    dtype: Literal["fp16", "fp32"] = "fp16"
    batch_size: int = Field(default=128, ge=8, le=512)
    max_seq_length: int = Field(default=512, ge=64, le=8192)


class EscalationSettings(_Model):
    max_rows_per_night: int = Field(default=150_000, ge=0, le=10_000_000)
    llm_max_rows_per_night: int = Field(default=20_000, ge=0, le=1_000_000)
    bootstrap_window_days: int = Field(default=90, ge=1, le=3650)


class SpotCheckSettings(_Model):
    nightly_rate: _Fraction = 0.001
    nightly_max_per_question: int = Field(default=50, ge=0, le=10_000)
    open_cap_per_question: int = Field(default=300, ge=0, le=100_000)


class EnsembleSettings(_Model):
    band: float = Field(default=0.90, ge=0.5, le=1)
    max_rows: int = Field(default=300_000, ge=0, le=10_000_000)
    llm_max_rows: int = Field(default=20_000, ge=0, le=1_000_000)
    disagreement_review_cap: int = Field(default=500, ge=0, le=100_000)


class ActiveLearningSettings(_Model):
    pool: int = Field(default=500_000, gt=0)
    candidates: int = Field(default=20_000, gt=0)
    per_round: int = Field(default=5000, gt=0)
    per_round_llm_teacher: int = Field(default=2000, gt=0)
    per_prototype: int = Field(default=5, gt=0)
    min_gain_pp: float = Field(default=0.5, ge=0)
    patience: int = Field(default=2, ge=1)
    max_rounds: int = Field(default=5, ge=1)

    def _rules(self) -> Rules:
        ordered = self.per_round <= self.candidates <= self.pool
        yield ordered, "distill.active: per_round <= candidates <= pool must hold"


class DistillSettings(_Model):
    sample_size: int = Field(default=30_000, ge=20_000, le=50_000)
    sample_size_llm_teacher: int = Field(default=20_000, ge=1000, le=50_000)
    gold_size: int = Field(default=1500, ge=100, le=10_000)
    init_from: Literal["base", "previous"] = "base"
    spot_check_min: int = Field(default=200, ge=0)
    spot_check_max: int = Field(default=500, ge=0)
    block_disagreement: _Fraction = 0.15
    active: ActiveLearningSettings = Field(default_factory=ActiveLearningSettings)

    def _rules(self) -> Rules:
        ordered = self.spot_check_min <= self.spot_check_max
        yield ordered, "distill.spot_check_min must be <= distill.spot_check_max"


class NamingSettings(_Model):
    role: _Role = "cluster_namer"
    max_llm_calls: int = Field(default=500, ge=0, le=10_000)
    examples: int = Field(default=20, ge=1, le=50)
    example_chars: int = Field(default=600, ge=50, le=4000)


class ClusteringSettings(_Model):
    window_days: int = Field(default=1095, ge=30, le=3650)
    pca_dims: int = Field(default=64, ge=8, le=256)
    pca_sample: int = Field(default=200_000, ge=1000, le=1_000_000)
    proto_per: int = Field(default=250, ge=1)
    k_min: int = Field(default=1000, ge=1)
    k_max: int = Field(default=20_000, ge=1)
    iters: int = Field(default=15, ge=1, le=100)
    min_cluster_size: int = Field(default=5, ge=2)
    min_samples: int = Field(default=3, ge=1)
    assign_min_sim: _Fraction = 0.60
    full_sim: _Fraction = 0.85
    min_incidents: int = Field(default=25, ge=1)
    match_cos: _Fraction = 0.85
    revive_cos: _Fraction = 0.90
    rename_cos: _Fraction = 0.95
    revive_days: int = Field(default=90, ge=1)
    full_every_days: int = Field(default=7, ge=1)
    drift_share: _Fraction = 0.10
    naming: NamingSettings = Field(default_factory=NamingSettings)

    def _rules(self) -> Rules:
        yield self.k_min <= self.k_max, "clustering.k_min must be <= clustering.k_max"
        ordered = self.assign_min_sim < self.full_sim
        yield ordered, "clustering.assign_min_sim must be < clustering.full_sim"


class ChangeLinkSettings(_Model):
    before_h: _Positive = 72.0
    after_h: _Positive = 1.0
    tau_h: _Positive = 12.0
    ci_weight: _Fraction = 1.0
    service_weight: _Fraction = 0.6
    min_score: _Fraction = 0.30
    top_n: int = Field(default=3, ge=1, le=20)
    use_decider: bool = True
    decider_band: Annotated[tuple[_Fraction, _Fraction], _Tuple] = (0.30, 0.70)
    decider_max_pairs: int = Field(default=30_000, ge=0, le=1_000_000)

    def _rules(self) -> Rules:
        ascending = self.decider_band[0] < self.decider_band[1]
        yield ascending, "change_link.decider_band must be ascending"


class MappingWeights(_Model):
    fuzzy: _Fraction = 0.35
    semantic: _Fraction = 0.45
    cooccurrence: _Fraction = 0.20


class MappingSuggestSettings(_Model):
    min_score: _Fraction = 0.60
    top_n: int = Field(default=3, ge=1, le=20)
    weights: MappingWeights = Field(default_factory=MappingWeights)
    abbreviations: dict[_Abbreviation, _Name] = Field(
        default_factory=lambda: {"pmt": "payment", "auth": "authentication"}
    )

    def _rules(self) -> Rules:
        total = self.weights.fuzzy + self.weights.semantic + self.weights.cooccurrence
        yield abs(total - 1.0) <= _WEIGHT_TOL, "mapping_suggest.weights must sum to 1"
        clash = any(value in self.abbreviations for value in self.abbreviations.values())
        yield not clash, "mapping_suggest.abbreviations: an expansion equals a key"


class DecisionsConfig(_Model):
    """Root of ``config/decisions.yaml`` (U03-09). No ``deciders`` key (R-76)."""

    question_set_version: str
    primary_decider: DeciderName = "laya"
    escalation_chain: Annotated[tuple[EscalationName, ...], _Tuple] = ("openjev", "llm")
    questions: Annotated[tuple[QuestionConfig, ...], _Tuple, Field(max_length=64)]
    acceptance: AcceptanceDefaults = Field(default_factory=AcceptanceDefaults)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    escalation: EscalationSettings = Field(default_factory=EscalationSettings)
    spot_check: SpotCheckSettings = Field(default_factory=SpotCheckSettings)
    ensemble: EnsembleSettings = Field(default_factory=EnsembleSettings)
    distill: DistillSettings = Field(default_factory=DistillSettings)
    clustering: ClusteringSettings = Field(default_factory=ClusteringSettings)
    change_link: ChangeLinkSettings = Field(default_factory=ChangeLinkSettings)
    mapping_suggest: MappingSuggestSettings = Field(default_factory=MappingSuggestSettings)

    def _rules(self) -> Rules:
        chain, ids = self.escalation_chain, [question.id for question in self.questions]
        try:
            QuestionSet(version=self.question_set_version, questions=())
        except ValidationError:
            yield False, "question_set_version must match the QuestionSet.version pattern"
        yield len(set(chain)) == len(chain), "escalation_chain must not contain duplicates"
        yield self.primary_decider not in chain, "escalation_chain contains primary_decider"
        yield len(set(ids)) == len(ids), "questions: duplicate question id"
        paired = not self.change_link.use_decider or _PAIR_QUESTION in ids
        yield paired, f"change_link.use_decider requires question {_PAIR_QUESTION}"


class DepthSamples(_Model):  # null means the OpenJev server default
    fast: _Samples = 1
    standard: _Samples = None
    deep: _Samples = 5


class DepthVotes(_Model):
    fast: int = Field(default=1, ge=1, le=9)
    standard: int = Field(default=3, ge=1, le=9)
    deep: int = Field(default=5, ge=1, le=9)


def _url_host(value: str, key: str, schemes: frozenset[str]) -> str:
    clean = all(ch.isprintable() and not ch.isspace() for ch in value)
    _require(clean, f"{key} must not contain whitespace or control characters")
    parts = urlsplit(value)
    try:
        _ = parts.port
    except ValueError:
        msg = f"{key} has a malformed port"
        raise ValueError(msg) from None
    ok = parts.scheme in schemes and bool(parts.hostname)
    _require(ok, f"{key} needs scheme {' or '.join(sorted(schemes))} and a host")
    no_userinfo = parts.username is None and parts.password is None
    _require(no_userinfo, f"{key} must not carry credentials")
    return str(parts.hostname)


class LayaSettings(_Model):
    current_file: _Name = "data/models/laya/CURRENT"
    device: Literal["cuda", "cpu"] = "cuda"
    dtype: Literal["bf16", "fp32"] = "bf16"
    fast: bool = False
    call_batch: int = Field(default=256, ge=1, le=4096)
    batch_size: int = Field(default=64, ge=1, le=512)


class OpenJevSettings(_Model):
    """``deciders.openjev`` (loopback only, TH03-15)."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8100"
    model: _Name = "openjev-latest"
    concurrency: int = Field(default=64, ge=1, le=256)
    timeout_s: float = Field(default=30.0, ge=1, le=300)
    samples: DepthSamples = Field(default_factory=DepthSamples)
    api_key: _SecretRef = "secret:OPENJEV_API_KEY"

    def _rules(self) -> Rules:
        host = _url_host(self.base_url, "openjev.base_url", frozenset({"http", "https"}))
        yield host in _LOOPBACK_HOSTS, "openjev.base_url host must be 127.0.0.1 or localhost"


class JevSettings(_Model):
    """``deciders.jev`` (hosted, https only)."""

    enabled: bool = False
    base_url: str = "https://api.typesafe.ai"
    model: _Name = "jev-latest"
    concurrency: int = Field(default=16, ge=1, le=64)
    api_key: _SecretRef = "secret:TYPESAFE_API_KEY"

    def _rules(self) -> Rules:
        _url_host(self.base_url, "jev.base_url", frozenset({"https"}))
        return iter(())


class LlmDeciderSettings(_Model):
    role: _Role = "enrich_decider"
    votes: DepthVotes = Field(default_factory=DepthVotes)
    temperature: float = Field(default=0.7, ge=0, le=2)


class DecidersSettings(_Model):
    """The top-level ``deciders`` section of ``config/models.yaml`` (U03-150, R-76)."""

    laya: LayaSettings = Field(default_factory=LayaSettings)
    openjev: OpenJevSettings = Field(default_factory=OpenJevSettings)
    jev: JevSettings = Field(default_factory=JevSettings)
    llm: LlmDeciderSettings = Field(default_factory=LlmDeciderSettings)


def check_decider_refs(
    decisions: DecisionsConfig, deciders: DecidersSettings, /
) -> list[dict[str, str]]:
    """Issues for disabled deciders named in ``decisions`` (U03-151): jev error, openjev warn."""
    disabled = {} if deciders.jev.enabled else {"jev": "error"}
    if not deciders.openjev.enabled:
        disabled["openjev"] = "warn"  # skipped at run time, degraded note openjev_unavailable
    positions: list[tuple[str, str]] = [
        ("decisions.escalation_chain", name) for name in decisions.escalation_chain
    ]
    positions.append(("decisions.primary_decider", decisions.primary_decider))
    positions += [
        (f"decisions.questions[{index}].primary_decider", question.primary_decider)
        for index, question in enumerate(decisions.questions)
        if question.primary_decider is not None
    ]
    return [
        {
            "severity": disabled[name],
            "path": path,
            "message": f"{path} requires deciders.{name}.enabled",
        }
        for path, name in positions
        if name in disabled
    ]
