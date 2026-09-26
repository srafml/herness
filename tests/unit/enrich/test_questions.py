"""Tests for herness.enrich.questions (U03-14 ... U03-20; T03-03).

Deviations (see the T03-03 report):
- UT03-12 names `build_inputs` (U03-84, a later card); here it covers the `PAIR_QUESTIONS`
  declaration and the pair-shape checks of `load_question_set` only.
- UT03-02 / UT03-03: `DecisionsConfig` already applies the `Question` rules to every
  `QuestionConfig`, so a bad question from YAML is rejected there (ValidationError). The
  loader-level cases build the config with `model_construct` (bypassing that validation) and
  assert `load_question_set` raises `ConfigError` naming the question.
- `redact_text` (T10-10) is not in the tree: `resolve_dynamic_options` takes a required
  `redact` callable; the tests pass a stub and assert it was applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from structlog.testing import capture_logs

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json
from herness.core.types import Question, QuestionSet, decisions
from herness.enrich import questions as questions_module
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import (
    PAIR_QUESTIONS,
    acceptance_for,
    check_fingerprint_registry,
    load_question_set,
    question_fingerprint,
    resolve_dynamic_options,
    shortlist_options,
)
from herness.enrich.settings import AcceptanceCriteria, DecisionsConfig, QuestionConfig

pytestmark = pytest.mark.unit

_QSV = "qs-2026-10-01.1"
_DECISIONS_YAML = """
question_set_version: qs-2026-10-01.1
primary_decider: laya
escalation_chain: [openjev, llm]
questions:
  - id: root_cause
    type: choice
    applies_to: [incident, problem]
    instructions: "Most likely root cause category of this IT incident, based on the description."
    options:
      software_defect: "application bug, code error, regression"
      config_change: "misconfiguration, bad parameter, config drift"
      unknown: "not enough information to tell"
    threshold: 0.70
    acceptance: {min_accuracy: 0.9}
  - id: change_caused
    type: bool
    applies_to: [incident]
    instructions: "The description states the issue started after a change."
    threshold: 0.80
  - id: business_impact
    type: score
    applies_to: [incident]
    instructions: "Business impact described in the text."
    levels: ["none or single user", "team or workaround", "department", "customer outage"]
    threshold: 0.60
  - id: owning_team
    type: choice
    options_source: core.team
    applies_to: [incident]
    instructions: "Which support team should own this issue?"
    threshold: 0.60
    scoring_use: false
  - id: change_caused_pair
    type: bool
    applies_to: [incident]
    instructions: "The change described caused the incident described."
    threshold: 0.70
    scoring_use: false
"""


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(_DECISIONS_YAML)
    return raw


def _cfg(raw: dict[str, Any] | None = None) -> DecisionsConfig:
    return DecisionsConfig.model_validate(raw if raw is not None else _raw())


def _question(**overrides: object) -> Question:
    fields: dict[str, object] = {
        "id": "root_cause",
        "type": "choice",
        "instructions": "Most likely root cause of the incident.",
        "options": {"defect": "A software defect.", "other": "Anything else."},
        "applies_to": ("incident",),
        "threshold": 0.7,
    }
    fields.update(overrides)
    return Question.model_validate(fields)


def _constructed(**overrides: object) -> DecisionsConfig:
    """A config whose single question bypasses `QuestionConfig` validation."""
    fields: dict[str, object] = {
        "id": "bad_question",
        "type": "choice",
        "instructions": "A question with a deliberately bad shape.",
        "options": {"a": "one", "b": "two"},
        "options_source": "static",
        "levels": None,
        "applies_to": ("incident",),
        "threshold": 0.7,
        "scoring_use": True,
        "acceptance": None,
        "primary_decider": None,
    }
    fields.update(overrides)
    question = QuestionConfig.model_construct(**fields)  # type: ignore[arg-type]
    base = _cfg()
    return base.model_copy(update={"questions": (question,)})


def _paths(root: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=root.resolve(),
        embedding_path="data/models/e",
        laya_current_file="data/models/laya/CURRENT",
    )


# --- U03-14 / U03-16 ------------------------------------------------------------------


def test_ut03_12_pair_questions_declaration_and_shape() -> None:
    """UT03-12 PAIR_QUESTIONS holds only `change_caused_pair`; a well-shaped pair loads."""
    assert frozenset({"change_caused_pair"}) == PAIR_QUESTIONS
    qs = load_question_set(_cfg())
    pair = qs.get("change_caused_pair")
    assert (pair.type, pair.scoring_use, pair.applies_to) == ("bool", False, ("incident",))


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "choice", "options": {"yes_it": "caused", "no_it": "not caused"}},
        {"applies_to": ["incident", "change"]},
    ],
    ids=["choice", "applies-to-change"],
)
def test_ut03_12_pair_question_wrong_shape_rejected(overrides: dict[str, object]) -> None:
    """UT03-12 a pair question that is not bool or not incident-only raises ConfigError."""
    raw = _raw()
    raw["questions"][-1].update(overrides)
    with pytest.raises(ConfigError, match="change_caused_pair"):
        load_question_set(_cfg(raw))


def test_ut03_14_pair_question_scoring_use_rejected() -> None:
    """UT03-14 pair question with `scoring_use: true` raises ConfigError on load."""
    raw = _raw()
    raw["questions"][-1]["scoring_use"] = True
    with pytest.raises(ConfigError, match="change_caused_pair"):
        load_question_set(_cfg(raw))


def test_ut03_14_load_sets_fingerprints_in_order() -> None:
    """UT03-14 (supporting U03-16) every fingerprint set; order and version preserved."""
    cfg = _cfg()
    qs = load_question_set(cfg)
    assert qs.version == _QSV
    assert [q.id for q in qs.questions] == [q.id for q in cfg.questions]
    for loaded, source in zip(qs.questions, cfg.questions, strict=True):
        assert loaded.fingerprint == question_fingerprint(source)
    assert qs.get("owning_team").options is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": {"a": "one"}},
        {"options": {f"o{i}": "description" for i in range(256)}},
        {"type": "score", "options": None, "levels": ("a", "b", "c")},
        {"type": "bool", "options": None, "levels": ("a", "b", "c", "d")},
    ],
    ids=["one-option", "256-options", "three-levels", "levels-on-bool"],
)
def test_ut03_02_loader_rejects_bad_shapes(overrides: dict[str, object]) -> None:
    """UT03-02 1 option, 256 static options, 3 levels, `levels` on bool: DecisionsConfig
    rejects them from YAML, and `load_question_set` raises ConfigError naming the question."""
    raw = _raw()
    yaml_overrides = {k: list(v) if isinstance(v, tuple) else v for k, v in overrides.items()}
    raw["questions"][0].update(yaml_overrides)
    with pytest.raises(ValidationError):
        _cfg(raw)
    with pytest.raises(ConfigError, match="bad_question"):
        load_question_set(_constructed(**overrides))


@pytest.mark.parametrize("word", ["true", "No", "yes", "FALSE"])
def test_ut03_03_loader_rejects_bool_word_labels(word: str) -> None:
    """UT03-03 option keys `true`, `No`, `yes`, `FALSE` raise ConfigError on load."""
    options = {word: "a description", "other": "another description"}
    raw = _raw()
    raw["questions"][0]["options"] = options
    with pytest.raises(ValidationError):
        _cfg(raw)
    with pytest.raises(ConfigError, match="bad_question") as info:
        load_question_set(_constructed(options=options))
    assert isinstance(info.value.__cause__, ValidationError)


def test_ut03_02_loader_rejects_duplicate_ids_and_bad_version() -> None:
    """UT03-02 (U03-16 errors) duplicate id and a bad version raise ConfigError."""
    cfg = _constructed(options={"a": "one", "b": "two"})
    doubled = cfg.model_copy(update={"questions": cfg.questions * 2})
    with pytest.raises(ConfigError, match="duplicate question id bad_question"):
        load_question_set(doubled)
    bad_version = cfg.model_copy(update={"question_set_version": "../../x"})
    with pytest.raises(ConfigError, match="question_set_version"):
        load_question_set(bad_version)


# --- U03-15 ---------------------------------------------------------------------------

_MUTATIONS: dict[str, object] = {
    "id": "root_cause_2",
    "type": "bool",
    "instructions": "A different instruction text for this question.",
    "options": {"defect": "A software defect.", "other": "Something else."},
    "options_source": "core.team",
    "levels": ("l0", "l1", "l2", "l3"),
    "applies_to": ("incident", "problem"),
    "threshold": 0.71,
    "scoring_use": False,
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_ut03_13_fingerprint_changes_for_every_field(name: str) -> None:
    """UT03-13 changing any fingerprinted field changes the fingerprint."""
    base = _question()
    changed = base.model_copy(update={name: _MUTATIONS[name]})
    fingerprint = question_fingerprint(base)
    assert len(fingerprint) == 16
    assert int(fingerprint, 16) >= 0
    assert question_fingerprint(changed) != fingerprint


def test_ut03_13_dynamic_options_do_not_enter_fingerprint() -> None:
    """UT03-13 a dynamic-option copy with different options keeps the fingerprint."""
    dynamic = _question(options_source="core.team", options=None)
    filled = dynamic.model_copy(update={"options": {"t1": "Team one", "t2": "Team two"}})
    assert question_fingerprint(filled) == question_fingerprint(dynamic)
    assert question_fingerprint(_question(fingerprint="0" * 16)) == question_fingerprint(
        _question()
    )


def test_ut03_13_config_and_question_agree() -> None:
    """UT03-13 a QuestionConfig and its Question give the same fingerprint; the threshold
    is compared at 6 decimals."""
    cfg = _cfg()
    qs = load_question_set(cfg)
    for source, loaded in zip(cfg.questions, qs.questions, strict=True):
        assert question_fingerprint(loaded) == question_fingerprint(source)
    assert question_fingerprint(_question(threshold=0.7000000001)) == question_fingerprint(
        _question(threshold=0.7)
    )


_OPTION_KEYS = st.from_regex(r"[a-z][a-z0-9_]{2,10}", fullmatch=True)
_DESCRIPTIONS = st.text(alphabet="abcdefgh ", min_size=1, max_size=30)


@st.composite
def _question_fields(draw: st.DrawFn) -> dict[str, object]:
    options = draw(st.dictionaries(_OPTION_KEYS, _DESCRIPTIONS, min_size=2, max_size=6))
    return {
        "id": draw(st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True)),
        "type": "choice",
        "instructions": draw(st.text(alphabet="abcdefgh ", min_size=10, max_size=60)),
        "options": options,
        "applies_to": tuple(
            draw(
                st.lists(
                    st.sampled_from(["incident", "change", "problem"]),
                    min_size=1,
                    max_size=3,
                    unique=True,
                )
            )
        ),
        "threshold": draw(st.floats(min_value=0.5, max_value=0.99)),
        "scoring_use": draw(st.booleans()),
    }


def _mutate(q: QuestionConfig, name: str) -> object:
    """A value of field ``name`` that differs from ``q``'s."""
    entities = ("incident", "change", "problem")
    others = tuple(e for e in entities if e not in q.applies_to) or ("incident",)
    return {
        "id": f"{q.id}_x",
        "type": "bool",
        "instructions": q.instructions + "!",
        "options": {**(q.options or {}), "zz_extra": "extra option"},
        "options_source": "core.service",
        "levels": ("l0", "l1", "l2", "l3"),
        "applies_to": others,
        "threshold": round(q.threshold + 0.001, 6) if q.threshold < 0.99 else 0.5,
        "scoring_use": not q.scoring_use,
    }[name]


@given(fields=_question_fields(), data=st.data())
def test_pt03_02_fingerprint_key_order_and_mutation(
    fields: dict[str, object], data: st.DataObject
) -> None:
    """PT03-02 fingerprint is invariant to dict key order and changes under any mutation
    of any of the 9 fingerprinted fields."""
    base = QuestionConfig.model_validate(fields)
    options = fields["options"]
    assert isinstance(options, dict)
    shuffled = dict(data.draw(st.permutations(list(fields.items()))))
    shuffled["options"] = dict(data.draw(st.permutations(list(options.items()))))
    assert canonical_json(shuffled) == canonical_json(fields)
    assert question_fingerprint(QuestionConfig.model_validate(shuffled)) == question_fingerprint(
        base
    )
    assert question_fingerprint(Question.model_validate(shuffled)) == question_fingerprint(base)

    name = data.draw(st.sampled_from(sorted(_MUTATIONS)))
    mutated = base.model_copy(update={name: _mutate(base, name)})
    assert question_fingerprint(mutated) != question_fingerprint(base)


# --- U03-17 ---------------------------------------------------------------------------


def test_ut03_15_drift_raises_and_logs(tmp_path: Path) -> None:
    """UT03-15 `questions.json` with an old fingerprint for `root_cause` raises ConfigError
    and logs `enrich.config.fingerprint_drift`."""
    paths = _paths(tmp_path)
    qs = load_question_set(_cfg())
    registry = paths.cache_dir(qs.version) / "questions.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"root_cause": "0123456789abcdef"}), encoding="utf-8")
    with capture_logs() as logs, pytest.raises(ConfigError, match="question root_cause changed"):
        check_fingerprint_registry(qs, paths=paths)
    drift = [e for e in logs if e["event"] == "enrich.config.fingerprint_drift"]
    assert len(drift) == 1
    assert drift[0]["log_level"] == "error"
    assert drift[0]["question"] == "root_cause"
    assert drift[0]["question_set_version"] == _QSV
    assert json.loads(registry.read_text(encoding="utf-8")) == {"root_cause": "0123456789abcdef"}


def test_ut03_15_new_ids_appended(tmp_path: Path) -> None:
    """UT03-15 without drift, new ids are appended and old ones kept; a rerun is a no-op."""
    paths = _paths(tmp_path)
    qs = load_question_set(_cfg())
    registry = paths.cache_dir(qs.version) / "questions.json"
    check_fingerprint_registry(qs, paths=paths)
    expected = {q.id: q.fingerprint for q in qs.questions}
    assert json.loads(registry.read_text(encoding="utf-8")) == expected

    kept = {"retired_question": "fedcba9876543210", "root_cause": expected["root_cause"]}
    registry.write_text(json.dumps(kept), encoding="utf-8")
    check_fingerprint_registry(qs, paths=paths)
    assert json.loads(registry.read_text(encoding="utf-8")) == {**expected, **kept}
    before = registry.stat().st_mtime_ns
    check_fingerprint_registry(qs, paths=paths)
    assert registry.stat().st_mtime_ns == before
    assert [p.name for p in registry.parent.iterdir()] == ["questions.json"]


@pytest.mark.parametrize(
    "content",
    [
        b"x" * (64 * 1024 + 1),
        b"{not json",
        b"[1, 2]",
        b'{"root_cause": 5}',
        b'{"root_cause": "0123456789ABCDEF"}',
        b'{"root_cause": "abc"}',
        b"\xff\xfe",
    ],
    ids=["oversized", "bad-json", "not-object", "bad-value", "upper-hex", "short", "bad-utf8"],
)
def test_ut03_15_unreadable_registry_rejected(tmp_path: Path, content: bytes) -> None:
    """UT03-15 (U03-17 errors) an oversized or unreadable `questions.json` raises ConfigError."""
    paths = _paths(tmp_path)
    qs = load_question_set(_cfg())
    registry = paths.cache_dir(qs.version) / "questions.json"
    registry.parent.mkdir(parents=True)
    registry.write_bytes(content)
    with pytest.raises(ConfigError, match=r"questions\.json"):
        check_fingerprint_registry(qs, paths=paths)


def test_ut03_15_registry_path_not_a_file(tmp_path: Path) -> None:
    """UT03-15 (U03-17 errors) a `questions.json` that cannot be opened raises ConfigError."""
    paths = _paths(tmp_path)
    qs = load_question_set(_cfg())
    (paths.cache_dir(qs.version) / "questions.json").mkdir(parents=True)
    with pytest.raises(ConfigError, match=r"questions\.json is unreadable"):
        check_fingerprint_registry(qs, paths=paths)


def test_ut03_15_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-15 (atomic write) a failing replace removes the temp file and propagates."""
    paths = _paths(tmp_path)
    qs = load_question_set(_cfg())

    def _fail(*_: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("herness.enrich.questions.os.replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        check_fingerprint_registry(qs, paths=paths)
    assert list(paths.cache_dir(qs.version).iterdir()) == []


# --- U03-18 ---------------------------------------------------------------------------


def _warehouse(teams: list[tuple[str, str | None, bool]]) -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect(":memory:")
    wh.execute("CREATE SCHEMA core")
    wh.execute("CREATE TABLE core.team (team_id VARCHAR, name VARCHAR, active BOOLEAN)")
    wh.execute("CREATE TABLE core.service (service_id VARCHAR, name VARCHAR)")
    if teams:
        wh.executemany("INSERT INTO core.team VALUES (?, ?, ?)", teams)
    wh.executemany(
        "INSERT INTO core.service VALUES (?, ?)",
        [("svc_b", "Billing"), ("svc_a", ""), ("svc_c", "x" * 600)],
    )
    return wh


class _Redactor:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, text: str) -> str:
        self.seen.append(text)
        return f"R({text})"


def _dynamic_set(source: str) -> QuestionSet:
    return QuestionSet(
        version=_QSV,
        questions=(
            _question(),
            _question(id="owning_team", options=None, options_source=source, fingerprint="a" * 16),
            _question(id="flag", type="bool", options=None),
        ),
    )


def test_ut03_16_team_options_resolved() -> None:
    """UT03-16 3 active teams (1 bad id, 1 NULL name) give 2 options, the skip is logged and
    the NULL name falls back to the id; names pass through the redactor."""
    wh = _warehouse(
        [
            ("team_b", "Payments Squad", True),
            ("team_a", None, True),
            ("bad id!", "Nope", True),
            ("team_c", "Inactive", False),
        ]
    )
    redact = _Redactor()
    qs = _dynamic_set("core.team")
    with capture_logs() as logs:
        resolved = resolve_dynamic_options(qs, wh=wh, redact=redact)
    owning = resolved.get("owning_team")
    assert owning.options == {"team_a": "team_a", "team_b": "R(Payments Squad)"}
    assert list(owning.options) == ["team_a", "team_b"]
    assert owning.fingerprint == "a" * 16
    assert redact.seen == ["Payments Squad"]
    assert resolved.get("root_cause") == qs.get("root_cause")
    assert resolved.get("flag") == qs.get("flag")
    skipped = [e for e in logs if e["event"] == "enrich.questions.option_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"
    assert (skipped[0]["question"], skipped[0]["count"]) == ("owning_team", 1)


def test_ut03_16_service_options_truncated_and_redacted() -> None:
    """UT03-16 `core.service` rows: empty name falls back to the id, long names are cut at
    500 chars after redaction."""
    wh = _warehouse([])
    resolved = resolve_dynamic_options(_dynamic_set("core.service"), wh=wh, redact=_Redactor())
    options = resolved.get("owning_team").options
    assert options is not None
    assert list(options) == ["svc_a", "svc_b", "svc_c"]
    assert options["svc_a"] == "svc_a"
    assert options["svc_b"] == "R(Billing)"
    assert len(options["svc_c"]) == 500
    assert options["svc_c"].startswith("R(x")


def test_ut03_16_single_team_rejected() -> None:
    """UT03-16 a warehouse with 1 team raises ConfigError("dynamic options < 2 ...")."""
    wh = _warehouse([("team_a", "Only Team", True)])
    with pytest.raises(ConfigError, match="dynamic options < 2 for owning_team"):
        resolve_dynamic_options(_dynamic_set("core.team"), wh=wh, redact=_Redactor())


def test_ut03_16_empty_redaction_falls_back_to_id() -> None:
    """UT03-16 a name the redactor reduces to "" gets the id as description."""
    wh = _warehouse([("team_a", "Alice", True), ("team_b", "Bob", True)])
    resolved = resolve_dynamic_options(_dynamic_set("core.team"), wh=wh, redact=lambda _: "")
    assert resolved.get("owning_team").options == {"team_a": "team_a", "team_b": "team_b"}


def test_ut03_16_option_key_rule_matches_owner() -> None:
    """UT03-16 the skip rule is U03-02 rule (e) exactly as herness.core.types enforces it."""
    assert questions_module._OPTION_KEY_RE.pattern == decisions._OPTION_KEY_RE.pattern
    assert questions_module._BOOL_WORDS == decisions._BOOL_WORDS


# --- U03-19 ---------------------------------------------------------------------------


def _unit(vector: np.ndarray) -> np.ndarray:
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def test_ut03_17_shortlist_top_k_by_similarity() -> None:
    """UT03-17 300 options with synthetic vectors: 64 labels, descending similarity, ties
    broken by label ascending, fingerprint unchanged."""
    rng = np.random.default_rng(7)
    labels = [f"team_{i:03d}" for i in range(300)]
    options = {label: f"Team number {label}" for label in labels}
    question = _question(
        id="owning_team", options=options, options_source="core.team", fingerprint="b" * 16
    )
    text_vec = _unit(rng.standard_normal(1024))
    option_vecs = {label: _unit(rng.standard_normal(1024)) for label in labels}
    for tied in ("team_250", "team_120", "team_010"):
        option_vecs[tied] = text_vec.copy()

    short = shortlist_options(question, text_vec, option_vecs)

    assert short.options is not None
    kept = list(short.options)
    assert len(kept) == 64
    assert kept[:3] == ["team_010", "team_120", "team_250"]
    sims = [float(option_vecs[label] @ text_vec) for label in kept]
    assert sims == sorted(sims, reverse=True)
    assert all(short.options[label] == options[label] for label in kept)
    assert short.fingerprint == question.fingerprint
    assert list(shortlist_options(question, text_vec, option_vecs, k=5).options or {}) == kept[:5]


def test_ut03_17_missing_vector_rejected() -> None:
    """UT03-17 (U03-19 errors) a label without a vector raises ConfigError naming it."""
    options = {f"t{i:03d}": "desc" for i in range(3)}
    question = _question(options=options)
    one = np.ones(1024, dtype=np.float32)
    with pytest.raises(ConfigError, match="t001"):
        shortlist_options(question, one, {"t000": one, "t002": one})


def test_ut03_17_preconditions_checked() -> None:
    """UT03-17 (U03-19 preconditions) a non-choice question or a vector that is not
    1024-d raises ConfigError."""
    one = np.ones(1024, dtype=np.float32)
    vecs = {"defect": one, "other": one}
    with pytest.raises(ConfigError, match="choice question"):
        shortlist_options(_question(type="bool", options=None), one, vecs)
    with pytest.raises(ConfigError, match="text vector"):
        shortlist_options(_question(), np.ones(4, dtype=np.float32), vecs)
    with pytest.raises(ConfigError, match="option vector other"):
        shortlist_options(_question(), one, {**vecs, "other": np.ones((2, 1024))})


# --- U03-20 ---------------------------------------------------------------------------


def test_ut03_18_override_min_accuracy_only() -> None:
    """UT03-18 overriding `min_accuracy` only keeps the other choice defaults."""
    cfg = _cfg()
    effective = acceptance_for(cfg, "root_cause")
    assert effective == AcceptanceCriteria(
        min_accuracy=0.9, min_macro_f1=0.60, max_ece=0.05, min_coverage=0.70,
        max_gap_to_teacher=0.02)  # fmt: skip
    assert acceptance_for(cfg, "change_caused") == cfg.acceptance.bool_
    assert acceptance_for(cfg, "business_impact") == cfg.acceptance.score


def test_ut03_18_unknown_question_rejected() -> None:
    """UT03-18 (U03-20 errors) an unknown question id raises ConfigError."""
    with pytest.raises(ConfigError, match="nope"):
        acceptance_for(_cfg(), "nope")
