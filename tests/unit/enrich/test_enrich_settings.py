"""Tests for herness.enrich.settings (U03-09 … U03-11, U03-150, U03-151).

Spec 10's loader does not exist yet (T10-03): YAML is loaded with ``yaml.safe_load`` and
validated with ``model_validate``; where the spec expects ``ConfigError`` these tests assert
pydantic ``ValidationError``, which spec 10 converts.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from herness.enrich import settings as s

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

# design 03 §5.5 and §7, without `deciders` (R-76); `change_caused_pair` added because
# `change_link.use_decider: true` requires it (§9, design 03 §5.10 step 3).
DESIGN_DECISIONS = """
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
      capacity: "resource exhaustion, disk full, CPU/memory saturation, throttling"
      infrastructure: "hardware, network, storage, datacenter failure"
      dependency: "third-party or upstream service failure"
      data_issue: "bad data, failed batch, integration payload error"
      access_identity: "login, permissions, certificates, password, MFA"
      user_error: "how-to, user mistake, training need"
      unknown: "not enough information to tell"
    threshold: 0.70
  - id: change_caused
    type: bool
    applies_to: [incident]
    instructions: "The description states or strongly implies the issue started after a change."
    threshold: 0.80
  - id: repeat_issue
    type: bool
    applies_to: [incident]
    instructions: "The description says this problem happened before or keeps recurring."
    threshold: 0.80
  - id: business_impact
    type: score
    applies_to: [incident]
    instructions: "Business impact described in the text."
    levels: ["none or single user", "team or workaround available",
             "department or degraded service", "customer-facing outage or revenue loss"]
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
    instructions: "The change described below plausibly caused the incident described above."
    threshold: 0.80
    scoring_use: false
acceptance:
  choice: {min_accuracy: 0.80, min_macro_f1: 0.60, max_ece: 0.05, min_coverage: 0.70,
           max_gap_to_teacher: 0.02}
  bool:   {min_accuracy: 0.88, max_ece: 0.05, min_coverage: 0.75, max_gap_to_teacher: 0.02}
  score:  {max_mae: 0.45, min_within_one: 0.92, max_ece: 0.06, min_coverage: 0.65}
embedding: {model: BAAI/bge-m3, path: data/models/bge-m3/<rev>/, dtype: fp16, batch_size: 128,
            max_seq_length: 512}
escalation: {max_rows_per_night: 150000, llm_max_rows_per_night: 20000, bootstrap_window_days: 90}
spot_check: {nightly_rate: 0.001, nightly_max_per_question: 50, open_cap_per_question: 300}
ensemble: {band: 0.90, max_rows: 300000, llm_max_rows: 20000, disagreement_review_cap: 500}
distill: {sample_size: 30000, sample_size_llm_teacher: 20000, gold_size: 1500, init_from: base,
          spot_check_min: 200, spot_check_max: 500, block_disagreement: 0.15,
          active: {pool: 500000, candidates: 20000, per_round: 5000, per_round_llm_teacher: 2000,
                   per_prototype: 5, min_gain_pp: 0.5, patience: 2, max_rounds: 5}}
clustering: {window_days: 1095, pca_dims: 64, pca_sample: 200000, proto_per: 250, k_min: 1000,
             k_max: 20000, iters: 15, min_cluster_size: 5, min_samples: 3, assign_min_sim: 0.60,
             full_sim: 0.85, min_incidents: 25, match_cos: 0.85, revive_cos: 0.90,
             revive_days: 90, rename_cos: 0.95, full_every_days: 7, drift_share: 0.10,
             naming: {role: cluster_namer, max_llm_calls: 500, examples: 20, example_chars: 600}}
change_link: {before_h: 72, after_h: 1, tau_h: 12, ci_weight: 1.0, service_weight: 0.6,
              min_score: 0.30, top_n: 3, use_decider: true, decider_band: [0.30, 0.70],
              decider_max_pairs: 30000}
mapping_suggest: {min_score: 0.60, top_n: 3,
                  weights: {fuzzy: 0.35, semantic: 0.45, cooccurrence: 0.20},
                  abbreviations: {pmt: payment, auth: authentication}}
"""

# design 03 §7 `deciders`, placed in models.yaml (R-76) with the impl §9 key names and values
# (`api_key: secret:…`, R-53/R-72; OpenJev host port 8100, R-51).
DESIGN_MODELS = """
deciders:
  laya:    {current_file: data/models/laya/CURRENT, device: cuda, dtype: bf16, fast: false,
            call_batch: 256, batch_size: 64}
  openjev: {enabled: true, base_url: "http://127.0.0.1:8100", model: openjev-latest,
            concurrency: 64, timeout_s: 30, samples: {fast: 1, standard: null, deep: 5},
            api_key: "secret:OPENJEV_API_KEY"}
  jev:     {enabled: false, base_url: "https://api.typesafe.ai", model: jev-latest,
            concurrency: 16, api_key: "secret:TYPESAFE_API_KEY"}
  llm:     {role: enrich_decider, votes: {fast: 1, standard: 3, deep: 5}, temperature: 0.7}
"""


def _load(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _decisions(**changes: Any) -> dict[str, Any]:
    data = _load(DESIGN_DECISIONS)
    for dotted, value in changes.items():
        *parents, leaf = dotted.split("__")
        target = data
        for key in parents:
            target = target[key]
        target[leaf] = value
    return data


def _deciders(section: str, **changes: Any) -> dict[str, Any]:
    data = _load(DESIGN_MODELS)["deciders"]
    data[section].update(changes)
    return data


def _minimal() -> dict[str, Any]:
    data = _load(DESIGN_DECISIONS)
    return {"question_set_version": data["question_set_version"], "questions": data["questions"]}


def test_ut03_08_design_yaml_parses_and_defaults_equal_section_9() -> None:
    """UT03-08 the design YAML parses; every omitted key takes its §9 default."""
    cfg = s.DecisionsConfig.model_validate(_load(DESIGN_DECISIONS))
    assert cfg == s.DecisionsConfig.model_validate(_minimal())
    assert cfg.primary_decider == "laya"
    assert cfg.escalation_chain == ("openjev", "llm")
    assert [q.id for q in cfg.questions][:2] == ["root_cause", "change_caused"]
    assert cfg.questions[3].levels is not None
    assert cfg.acceptance.bool_.min_accuracy == pytest.approx(0.88)
    assert cfg.acceptance.score.max_mae == pytest.approx(0.45)
    assert cfg.change_link.decider_band == (0.30, 0.70)
    assert cfg.distill.active.per_round == 5000
    assert cfg.clustering.naming.role == "cluster_namer"
    assert cfg.mapping_suggest.abbreviations == {"pmt": "payment", "auth": "authentication"}
    deciders = s.DecidersSettings.model_validate(_load(DESIGN_MODELS)["deciders"])
    assert deciders == s.DecidersSettings()
    assert deciders.openjev.api_key == "secret:OPENJEV_API_KEY"
    assert deciders.openjev.base_url == "http://127.0.0.1:8100"
    assert deciders.openjev.samples.standard is None
    assert deciders.jev.api_key == "secret:TYPESAFE_API_KEY"
    assert deciders.llm.votes.standard == 3
    with pytest.raises(ValidationError):
        cfg.primary_decider = "llm"  # type: ignore[misc]


def test_ut03_08_deciders_key_in_decisions_yaml_rejected() -> None:
    """UT03-08 a `deciders` key in decisions.yaml is rejected (extra="forbid", R-76)."""
    data = _decisions(deciders=_load(DESIGN_MODELS)["deciders"])
    with pytest.raises(ValidationError, match="deciders"):
        s.DecisionsConfig.model_validate(data)


def test_ut03_08_question_config_overrides_and_shared_rules() -> None:
    """UT03-08 QuestionConfig adds per-question overrides and keeps the Question rules."""
    questions = _load(DESIGN_DECISIONS)["questions"]
    questions[0]["acceptance"] = {"min_accuracy": 0.9}
    questions[0]["primary_decider"] = "llm"
    cfg = s.DecisionsConfig.model_validate(_decisions(questions=questions))
    assert cfg.questions[0].acceptance == s.AcceptanceCriteria(min_accuracy=0.9)
    assert cfg.questions[0].primary_decider == "llm"
    bad = dict(questions[3])
    del bad["levels"]
    with pytest.raises(ValidationError, match="levels"):
        s.QuestionConfig.model_validate(bad)
    with pytest.raises(ValidationError, match="fingerprint"):
        s.QuestionConfig.model_validate({**questions[1], "fingerprint": "0" * 16})


@pytest.mark.parametrize("name", ["decisions.yaml", "models.yaml"])
def test_ut03_08_shipped_config_files_validate(name: str) -> None:
    """UT03-08 the shipped config/decisions.yaml and models.yaml `deciders` validate."""
    data = _load((ROOT / "config" / name).read_text(encoding="utf-8"))
    if name == "models.yaml":
        deciders = s.DecidersSettings.model_validate(data["deciders"])
        assert deciders.openjev.api_key == "secret:OPENJEV_API_KEY"
    else:
        cfg = s.DecisionsConfig.model_validate(data)
        assert s.check_decider_refs(cfg, s.DecidersSettings()) == []


def test_ut03_09_jev_in_chain_while_disabled_is_one_error() -> None:
    """UT03-09 jev in escalation_chain with jev.enabled false: one error issue."""
    cfg = s.DecisionsConfig.model_validate(_decisions(escalation_chain=["jev", "llm"]))
    issues = s.check_decider_refs(cfg, s.DecidersSettings())
    assert issues == [
        {
            "severity": "error",
            "path": "decisions.escalation_chain",
            "message": "decisions.escalation_chain names jev but deciders.jev.enabled is false",
        }
    ]
    enabled = s.DecidersSettings.model_validate(_deciders("jev", enabled=True))
    assert s.check_decider_refs(cfg, enabled) == []


def test_ut03_09_decider_refs_check_every_position() -> None:
    """UT03-09 primary and per-question positions; disabled openjev is a warning."""
    questions = _load(DESIGN_DECISIONS)["questions"]
    questions[2]["primary_decider"] = "jev"
    questions[4]["primary_decider"] = "openjev"
    data = _decisions(primary_decider="openjev", escalation_chain=["llm"], questions=questions)
    cfg = s.DecisionsConfig.model_validate(data)
    deciders = s.DecidersSettings.model_validate(_deciders("openjev", enabled=False))
    issues = s.check_decider_refs(cfg, deciders)
    assert [(i["severity"], i["path"]) for i in issues] == [
        ("warn", "decisions.primary_decider"),
        ("error", "decisions.questions[2].primary_decider"),
        ("warn", "decisions.questions[4].primary_decider"),
    ]
    assert all(set(i) == {"severity", "path", "message"} for i in issues)


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        (
            {"mapping_suggest__weights": {"fuzzy": 0.35, "semantic": 0.35, "cooccurrence": 0.2}},
            "weights",
        ),
        ({"change_link__decider_band": [0.7, 0.3]}, "decider_band"),
        ({"change_link__decider_band": [0.3, 1.2]}, "decider_band"),
        ({"acceptance__choice": {}}, "criterion"),
        ({"escalation_chain": ["openjev", "openjev"]}, "duplicate"),
        ({"primary_decider": "llm", "escalation_chain": ["openjev", "llm"]}, "primary_decider"),
        ({"question_set_version": "2026-10-01"}, "question_set_version"),
        ({"clustering__k_min": 30000}, "k_min"),
        ({"clustering__assign_min_sim": 0.9}, "assign_min_sim"),
        ({"distill__spot_check_min": 600}, "spot_check_min"),
        ({"distill__active__per_round": 30000}, "per_round"),
        ({"mapping_suggest__abbreviations": {"PMT": "payment"}}, "abbreviations"),
        ({"mapping_suggest__abbreviations": {"pmt": "auth", "auth": "x"}}, "abbreviations"),
        ({"embedding__batch_size": 4}, "batch_size"),
        ({"ensemble__band": float("nan")}, "band"),
        ({"spot_check__nightly_rate": "0.1"}, "nightly_rate"),
    ],
)
def test_ut03_09_decisions_rules_reject(changes: dict[str, Any], needle: str) -> None:
    """UT03-09 each broken DecisionsConfig rule is a validation error naming the key."""
    with pytest.raises(ValidationError, match=needle):
        s.DecisionsConfig.model_validate(_decisions(**changes))


def test_ut03_09_question_list_rules_reject() -> None:
    """UT03-09 duplicate ids, more than 64 questions, empty per-question acceptance."""
    questions = _load(DESIGN_DECISIONS)["questions"]
    with pytest.raises(ValidationError, match="duplicate question id"):
        s.DecisionsConfig.model_validate(_decisions(questions=[*questions, questions[0]]))
    many = [{**questions[1], "id": f"q_{n}"} for n in range(65)]
    with pytest.raises(ValidationError, match="questions"):
        s.DecisionsConfig.model_validate(_decisions(questions=many))
    questions[0]["acceptance"] = {}
    with pytest.raises(ValidationError, match="criterion"):
        s.DecisionsConfig.model_validate(_decisions(questions=questions))


def test_ut03_09_use_decider_requires_pair_question() -> None:
    """UT03-09 change_link.use_decider needs change_caused_pair in questions (§9)."""
    questions = [q for q in _load(DESIGN_DECISIONS)["questions"] if q["id"] != "change_caused_pair"]
    with pytest.raises(ValidationError, match="change_caused_pair"):
        s.DecisionsConfig.model_validate(_decisions(questions=questions))
    data = _decisions(questions=questions, change_link__use_decider=False)
    assert not s.DecisionsConfig.model_validate(data).change_link.use_decider


@pytest.mark.parametrize(
    ("section", "changes", "needle"),
    [
        ("openjev", {"api_key": "OPENJEV_API_KEY"}, "api_key"),
        ("jev", {"api_key": "sk-live-abcdef0123456789"}, "api_key"),
        ("jev", {"base_url": "http://api.typesafe.ai"}, "https"),
        ("jev", {"base_url": "https://user:pw@api.typesafe.ai"}, "credentials"),
        ("openjev", {"samples": {"fast": 0, "standard": None, "deep": 5}}, "fast"),
        ("llm", {"votes": {"fast": 1, "standard": 10, "deep": 5}}, "standard"),
        ("laya", {"device": "tpu"}, "device"),
    ],
)
def test_ut03_09_deciders_rules_reject(section: str, changes: dict[str, Any], needle: str) -> None:
    """UT03-09 bare secret names, plain-http Jev and out-of-range keys are rejected."""
    with pytest.raises(ValidationError, match=needle) as caught:
        s.DecidersSettings.model_validate(_deciders(section, **changes))
    for value in changes.values():
        if isinstance(value, str) and section in {"openjev", "jev"}:
            assert value not in str(caught.value)
