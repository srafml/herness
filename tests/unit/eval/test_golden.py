"""Tests for herness.eval.golden (U11-53 models, U11-54 load_suite, U11-55 resolve)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError
from tests.unit.eval._golden_fixtures import (
    BUILD_ID,
    ENTITIES_SQL,
    NUMERIC_SQL,
    T1_TEAM,
    make_con,
    question,
    truth,
    write_suite,
)

from herness.core.errors import ConfigError
from herness.core.ids import query_id
from herness.eval import golden as g
from herness.eval import truth as truth_module

pytestmark = pytest.mark.unit


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return make_con()


def _resolve(
    q: dict[str, Any] | g.EvalQuestion,
    con: duckdb.DuckDBPyConnection,
    *,
    dataset_kind: str = "synthetic",
    cache: dict[str, g.ReferenceResult] | None = None,
    with_truth: bool = True,
) -> g.ResolvedQuestion:
    model = q if isinstance(q, g.EvalQuestion) else g.EvalQuestion.model_validate(q)
    return g.resolve(
        model,
        truth() if with_truth else None,
        con,
        build_id=BUILD_ID,
        dataset_kind=dataset_kind,  # type: ignore[arg-type]
        cache={} if cache is None else cache,
    )


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real: Callable[..., Any] = g._run_sql

    def counted(cur: duckdb.DuckDBPyConnection, sql: str) -> Any:
        calls.append(sql)
        return real(cur, sql)

    monkeypatch.setattr(g, "_run_sql", counted)
    return calls


# ---------------------------------------------------------------- U11-53 models


def test_ut11_45_expected_and_tolerance_invariants(tmp_path: Path) -> None:
    """UT11-45 empty `Expected` and a two-sided `Tolerance` fail; the loader wraps them."""
    with pytest.raises(ValidationError, match="at least one"):
        g.Expected.model_validate({})
    with pytest.raises(ValidationError, match="exactly one"):
        g.Tolerance.model_validate({"abs": 0.0, "rel": 0.01})
    with pytest.raises(ValidationError, match="exactly one"):
        g.Tolerance.model_validate({})
    assert g.Tolerance.model_validate({"abs": 0}).abs == 0
    empty = write_suite(tmp_path / "empty.yaml", [question("G07", expected={})])
    with pytest.raises(ConfigError, match="G07") as info:
        g.load_suite(empty)
    assert info.value.context["question_id"] == "G07"
    assert info.value.hint is not None
    assert "at least one" in info.value.hint
    numeric = {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 1, "rel": 1}}
    both = write_suite(tmp_path / "both.yaml", [question("G01", expected={"numeric": numeric})])
    with pytest.raises(ConfigError, match="G01"):
        g.load_suite(both)


def test_ut11_45_hint_never_echoes_input(tmp_path: Path) -> None:
    """UT11-45 validation hints carry locations and messages, not the offending values."""
    path = write_suite(tmp_path / "s.yaml", [question("G02", pipeline="secret-pipeline-xyz")])
    with pytest.raises(ConfigError) as info:
        g.load_suite(path)
    assert "secret-pipeline-xyz" not in str(info.value)
    assert "secret-pipeline-xyz" not in (info.value.hint or "")


def test_ut11_46_entity_checks() -> None:
    """UT11-46 `rank1`, `topk_contains:5:4`, `kendall_tau>=0.8` pass and `top3` is rejected."""
    for check in ("rank1", "topk_contains:5:4", "kendall_tau>=0.8", "set_equals"):
        block = g.EntitiesExpected.model_validate({"reference_sql": ENTITIES_SQL, "check": check})
        assert block.check == check
    with pytest.raises(ValidationError):
        g.EntitiesExpected.model_validate({"reference_sql": ENTITIES_SQL, "check": "top3"})


def test_ut11_46_rule_parts() -> None:
    """UT11-46 claim patterns compile case-insensitively; bad regexes and rubrics fail."""
    rule = g.ClaimRule.model_validate({"entity": "X", "pattern": "paid off"})
    assert rule.regex.flags & re.IGNORECASE
    assert rule.regex.search("It PAID OFF")
    with pytest.raises(ValidationError, match="valid regex"):
        g.ClaimRule.model_validate({"entity": "X", "pattern": "(unclosed"})
    with pytest.raises(ValidationError):
        g.RubricExpected.model_validate({"criteria": [], "min_score": 3})
    with pytest.raises(ValidationError):
        g.RubricExpected.model_validate({"criteria": ["a"], "min_score": 6})
    with pytest.raises(ValidationError):
        g.SignRule.model_validate({"column": "trend_slope", "sign": "up"})


# ---------------------------------------------------------------- U11-54 load_suite


def test_ut11_47_load_suite_defaults_merge_and_hash(tmp_path: Path) -> None:
    """UT11-47 defaults applied; top-level must_mention merged; sha256 = file hash."""
    numeric = {"reference_sql": NUMERIC_SQL, "unit": "count"}
    own = {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 0}}
    path = write_suite(
        tmp_path / "golden.yaml",
        [
            question("G03", expected={"entities": {"reference_sql": ENTITIES_SQL,
                     "check": "rank1"}, "must_mention": ["{entity_name}"]}),
            question("G01", datasets=["synthetic"], expected={"numeric": numeric}),
            question("G14", expected={"numeric": own,
                     "must_not_claim": [{"entity": "X", "pattern": "degraded"}],
                     "rules": {"must_mention": ["volume"], "max_number_refs": 0}}),
        ],
    )  # fmt: skip
    suite = g.load_suite(path)
    assert suite.version == 3
    assert suite.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    g03, g01, g14 = suite.questions
    assert g03.datasets == ["synthetic", "real"]
    assert g01.datasets == ["synthetic"]
    assert g01.expected.numeric is not None
    assert g01.expected.numeric.tolerance == g.Tolerance(rel=0.01)
    assert g14.expected.numeric is not None
    assert g14.expected.numeric.tolerance == g.Tolerance(abs=0)
    assert g03.expected.rules is not None
    assert g03.expected.rules.must_mention == ["{entity_name}"]
    assert g03.expected.must_mention == []
    assert g14.expected.rules is not None
    assert g14.expected.rules.must_mention == ["volume"]
    assert [r.pattern for r in g14.expected.rules.must_not_claim] == ["degraded"]
    assert g14.expected.rules.max_number_refs == 0


def test_ut11_47_version_2_rejected(tmp_path: Path) -> None:
    """UT11-47 a version-2 suite is rejected with ConfigError."""
    path = write_suite(tmp_path / "v2.yaml", [question()], version=2)
    with pytest.raises(ConfigError, match="suite"):
        g.load_suite(path)


def test_ut11_48_duplicate_ids_and_oversized_file(tmp_path: Path) -> None:
    """UT11-48 duplicate ids and a 1.5 MB file each raise ConfigError."""
    dup = write_suite(tmp_path / "dup.yaml", [question("G03"), question("G03")])
    with pytest.raises(ConfigError, match="G03"):
        g.load_suite(dup)
    big = tmp_path / "big.yaml"
    big.write_text("# " + "x" * 1_500_000 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="1 MB"):
        g.load_suite(big)


def test_ut11_48_other_load_failures(tmp_path: Path) -> None:
    """UT11-48 missing file, non-mapping, bad YAML and 501 questions raise ConfigError."""
    with pytest.raises(ConfigError, match="cannot read"):
        g.load_suite(tmp_path / "missing.yaml")
    seq = tmp_path / "seq.yaml"
    seq.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        g.load_suite(seq)
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        g.load_suite(bad)
    many = [question(f"G{i % 100:02d}") for i in range(501)]
    with pytest.raises(ConfigError):
        g.load_suite(write_suite(tmp_path / "many.yaml", many))
    odd = write_suite(tmp_path / "odd.yaml", ["not a question"], defaults={"datasets": ["real"]})
    with pytest.raises(ConfigError, match="#0"):
        g.load_suite(odd)
    no_list = tmp_path / "nolist.yaml"
    no_list.write_text("version: 3\ndefaults: {tolerance: {rel: 0.01}}\nquestions: 1\n", "utf-8")
    with pytest.raises(ConfigError, match="golden suite failed"):
        g.load_suite(no_list)


# ---------------------------------------------------------------- U11-55 resolve


def test_ut11_49_shared_reference_uses_cache(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-49 two questions sharing a reference SQL execute it once (cache hit)."""
    calls = _counting(monkeypatch)
    cache: dict[str, g.ReferenceResult] = {}
    first = _resolve(question("G03"), con, cache=cache)
    second = _resolve(question("O01", pipeline="org"), con, cache=cache)
    assert len(calls) == 1
    assert len(cache) == 1
    ref = first.reference["entities"]
    assert second.reference["entities"] == ref
    assert ref.query_id == query_id(ENTITIES_SQL, {}, BUILD_ID)
    assert ref.columns == ["team_id", "org_name"]
    assert ref.rows[0] == (T1_TEAM, "Retail")
    assert first.skip_reason is None


def test_ut11_49_reference_failures(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-49 SQL error, empty result and timeout raise SuiteError; rows cap at 1,000."""
    cases = {
        "SELECT CAST(team_id AS INTEGER) AS v FROM core.team": "reference SQL failed",
        "SELECT team_id FROM core.team WHERE team_id = 'none'": "empty reference",
    }
    for sql, reason in cases.items():
        q = question(expected={"entities": {"reference_sql": sql, "check": "rank1"}})
        with pytest.raises(truth_module.SuiteError, match=reason) as info:
            _resolve(q, con)
        assert info.value.context["question_id"] == "G03"
    big = question(expected={"numeric": {"unit": "count",
                   "reference_sql": "SELECT r AS value FROM range(5000) AS t(r)"}})  # fmt: skip
    assert len(_resolve(big, con).reference["numeric"].rows) == 1_000
    monkeypatch.setattr(g, "QUERY_TIMEOUT_S", 0.05)
    slow_sql = "SELECT count(*) AS value FROM range(100000000000) AS t(r) WHERE r % 7 = 3"
    slow = question(expected={"numeric": {"unit": "count", "reference_sql": slow_sql}})
    with pytest.raises(truth_module.SuiteError, match="timed out"):
        _resolve(slow, con)


def test_ut11_50_placeholders(con: duckdb.DuckDBPyConnection) -> None:
    """UT11-50 `{T5.team}`, `{entity_name}` and `{org_name}` resolve to names and values."""
    q = question(
        question="Is {T5.team} worse than {entity_name} in {org_name}?",
        expected={
            "entities": {"reference_sql": ENTITIES_SQL, "check": "rank1",
                         "truth_ref": "plants.T1_bad_team.team_id"},
            "rules": {"must_mention": [{"entity": "{T6.paid.epic_key}", "with_any": ["paid"]}],
                      "must_not_claim": [{"entity": "{T3.owning_team}", "pattern": "(bad)"}]},
            "must_mention": ["{T5.peak_window}", "{T2.decoy}", "{T3.ci}"],
        },
    )  # fmt: skip
    resolved = _resolve(q, con)
    assert resolved.text == "Is Payments Core worse than Platform Ops L2 in Retail?"
    assert resolved.placeholders == {
        "T5.team": "Payments Core",
        "entity_name": "Platform Ops L2",
        "org_name": "Retail",
        "T6.paid.epic_key": "CHK-2001",
        "T3.owning_team": "Change Crew",
        "T5.peak_window": "07-15, 08-31",
        "T2.decoy": "DATA-310",
        "T3.ci": "Ledger API",
    }
    assert resolved.truth_values["plants.T1_bad_team.team_id"] == T1_TEAM
    assert resolved.truth_values["T5.team_id"] == "servicenow:sys_user_group:9f3"


def test_ut11_50_placeholder_sources(con: duckdb.DuckDBPyConnection) -> None:
    """UT11-50 `placeholders_sql` wins over entities; work items display their key."""
    q = question(
        question="Budget for {org_name} and {key}; epic {entity_name}",
        expected={
            "placeholders_sql": "SELECT name AS org_name, 'K-1' AS key FROM core.org",
            "entities": {"reference_sql": "SELECT record_id FROM core.work_item",
                         "check": "set_equals"},
            "numeric": {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 0},
                        "truth_ref": None},
        },
    )  # fmt: skip
    resolved = _resolve(q, con)
    assert resolved.text == "Budget for Retail and K-1; epic CHK-2001"
    assert set(resolved.reference) == {"placeholders_sql", "entities", "numeric"}
    assert resolved.reference["numeric"].rows == [(2,)]


def test_ut11_50_unresolved_placeholders(con: duckdb.DuckDBPyConnection) -> None:
    """UT11-50 unknown columns, missing entities, unknown ids and plants raise SuiteError."""
    numeric = {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 0}}
    unknown_id = {"reference_sql": "SELECT 'nobody' AS team_id", "check": "rank1"}
    cases = [
        (question(question="In {nowhere}?"), "unresolved placeholder"),
        (question(question="{entity_name}?", expected={"numeric": numeric}),
         "unresolved placeholder"),
        (question(question="{entity_name}?", expected={"entities": unknown_id}),
         "no display name"),
        (question(question="{T9.team}?"), "unknown plant"),
    ]  # fmt: skip
    for q, reason in cases:
        with pytest.raises(truth_module.SuiteError, match=reason) as info:
            _resolve(q, con)
        assert info.value.context["question_id"] == "G03"
    with pytest.raises(truth_module.SuiteError, match="truth manifest not available"):
        _resolve(question(question="{T5.team}?"), con, with_truth=False)


def test_ut11_50_missing_display_table_is_skipped() -> None:
    """UT11-50 display lookup skips a display table the build does not have."""
    con = make_con()
    con.execute("DROP TABLE core.team")
    q = question(expected={"entities": {"check": "rank1",
                 "reference_sql": "SELECT service_id FROM core.service ORDER BY service_id"}},
                 question="{entity_name}?")  # fmt: skip
    assert _resolve(q, con).text == "Ledger API?"


def test_ut11_51_dataset_skips(
    con: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-51 `datasets: [synthetic]` on real skips `dataset`; truth_ref on real skips."""
    calls = _counting(monkeypatch)
    synthetic_only = _resolve(question(datasets=["synthetic"]), con, dataset_kind="real")
    assert synthetic_only.skip_reason == "dataset"
    assert synthetic_only.reference == {}
    assert synthetic_only.text == synthetic_only.question.question
    numeric = {"reference_sql": NUMERIC_SQL, "unit": "count", "tolerance": {"abs": 0}}
    with_ref = question(expected={"numeric": {**numeric, "truth_ref": "T4.generated_noise_ratio"}})
    assert _resolve(with_ref, con, dataset_kind="real").skip_reason == "truth_ref_on_real"
    real_ok = _resolve(question(datasets=["synthetic", "real"]), con, dataset_kind="real")
    assert real_ok.skip_reason is None
    assert calls == [ENTITIES_SQL]


def test_ut11_52_truth_ref_mismatch(con: duckdb.DuckDBPyConnection) -> None:
    """UT11-52 a truth entity that differs from reference row 1 raises SuiteError."""
    q = question(expected={"entities": {"reference_sql": ENTITIES_SQL, "check": "rank1",
                 "truth_ref": "plants.T5_confounder.team_id"}})  # fmt: skip
    with pytest.raises(truth_module.SuiteError, match="truth_ref mismatch") as info:
        _resolve(q, con)
    assert info.value.context["question_id"] == "G03"
    with pytest.raises(truth_module.SuiteError, match="truth manifest not available"):
        _resolve(q, con, with_truth=False)


def test_ut11_52_suite_error_is_reexported() -> None:
    """UT11-52 golden re-exports the single `SuiteError` class declared in truth."""
    assert g.SuiteError is truth_module.SuiteError
    assert "SuiteError" in g.__all__
