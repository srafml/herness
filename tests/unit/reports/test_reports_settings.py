"""Tests for herness.reports.settings: config/app.yaml section model (T09-01)."""

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from herness.reports import settings as s

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]


def _app_yaml() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_ut09_02_empty_mapping_uses_every_default() -> None:
    """UT09-02 an empty app.yaml mapping with version: 1 loads every default of design §7."""
    cfg = s.AppConfig.model_validate({"version": 1})
    assert cfg.app.cache_ttl_s == s.CacheTtl(warehouse=3600, ops=10)
    assert cfg.app.current_recheck_s == 60
    assert cfg.app.page_row_limit == 5000
    assert cfg.chat.max_question_chars == 4000
    assert cfg.chat.poll_queued_s == 15
    assert cfg.reports.formats == ["html", "md"]
    assert cfg.reports.top_n == 10
    assert cfg.reports.strict_numbers is True
    assert cfg.reports.evidence_sample_rows == 20
    assert cfg.reports.allowed_numeral_patterns == [
        r"\b(19|20)\d{2}\b",
        r"\d{4}-\d{2}-\d{2}",
        r"Q[1-4] \d{4}",
        r"(INC|CHG|PRB)\d+",
        r"[A-Z][A-Z0-9]+-\d+",
    ]
    assert cfg.reports.prior_outcomes_window_days == (60, 120)
    assert cfg.cli.poll_interval_s == 2.0


def test_ut09_02_shipped_app_yaml_matches_defaults() -> None:
    """UT09-02 config/app.yaml loads and equals the all-defaults model."""
    cfg = s.AppConfig.model_validate(_app_yaml())
    assert cfg == s.AppConfig.model_validate({"version": 1})


def test_ut09_02_compiled_numeral_patterns_cached() -> None:
    """UT09-02 compiled_numeral_patterns compiles each pattern, in file order, once."""
    cfg = s.AppConfig.model_validate({"version": 1})
    compiled = cfg.reports.compiled_numeral_patterns
    assert [p.pattern for p in compiled] == cfg.reports.allowed_numeral_patterns
    assert cfg.reports.compiled_numeral_patterns is compiled


def test_ut09_02_version_must_be_1() -> None:
    """UT09-02 a version other than 1 raises ValidationError."""
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate({"version": 2})


def test_ut09_02_models_frozen_and_closed() -> None:
    """UT09-02 models are frozen and reject unknown keys."""
    cfg = s.AppConfig.model_validate({"version": 1})
    with pytest.raises(ValidationError):
        cfg.app.page_row_limit = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate({"version": 1, "nope": True})
    with pytest.raises(ValidationError):
        s.AppSection.model_validate({"nope": True})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("app", "cache_ttl_s", "warehouse"), 0),
        (("app", "cache_ttl_s", "warehouse"), 86401),
        (("app", "cache_ttl_s", "ops"), 0),
        (("app", "cache_ttl_s", "ops"), 3601),
        (("app", "current_recheck_s"), 4),
        (("app", "current_recheck_s"), 3601),
        (("app", "page_row_limit"), 99),
        (("app", "page_row_limit"), 50001),
        (("chat", "max_question_chars"), 99),
        (("chat", "max_question_chars"), 20001),
        (("chat", "poll_queued_s"), 4),
        (("chat", "poll_queued_s"), 301),
        (("reports", "top_n"), 0),
        (("reports", "top_n"), 101),
        (("reports", "evidence_sample_rows"), -1),
        (("reports", "evidence_sample_rows"), 51),
        (("cli", "poll_interval_s"), 0.4),
        (("cli", "poll_interval_s"), 60.1),
    ],
)
def test_ut09_02_out_of_range_values_rejected(path: tuple[str, ...], value: object) -> None:
    """UT09-02 each range bound of design §7's table is enforced."""
    data: dict[str, Any] = {"version": 1}
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(data)


def test_ut09_03_bad_pattern_errors_at_indexed_path() -> None:
    """UT09-03 pattern '(' raises a validation error at reports.allowed_numeral_patterns.0."""
    data = {"version": 1, "reports": {"allowed_numeral_patterns": ["("]}}
    with pytest.raises(ValidationError) as exc:
        s.AppConfig.model_validate(data)
    locs = [tuple(str(p) for p in err["loc"]) for err in exc.value.errors()]
    assert ("reports", "allowed_numeral_patterns", "0") in locs


def test_ut09_03_pattern_count_and_length_bounds() -> None:
    """UT09-03 allowed_numeral_patterns rejects an empty list, >50 items and >200 chars."""
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate({"version": 1, "reports": {"allowed_numeral_patterns": []}})
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(
            {"version": 1, "reports": {"allowed_numeral_patterns": ["a"] * 51}}
        )
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(
            {"version": 1, "reports": {"allowed_numeral_patterns": ["a" * 201]}}
        )


def test_ut09_04_duplicate_formats_and_bad_window_both_rejected() -> None:
    """UT09-04 duplicate formats and window [120, 60] both raise, as separate errors."""
    data = {
        "version": 1,
        "reports": {
            "formats": ["html", "html"],
            "prior_outcomes_window_days": [120, 60],
        },
    }
    with pytest.raises(ValidationError) as exc:
        s.AppConfig.model_validate(data)
    locs = [tuple(str(p) for p in err["loc"]) for err in exc.value.errors()]
    assert ("reports", "formats") in locs
    assert ("reports", "prior_outcomes_window_days") in locs


def test_ut09_04_formats_subset_enforced() -> None:
    """UT09-04 a format outside {html, md, pdf} raises ValidationError."""
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate({"version": 1, "reports": {"formats": ["docx"]}})


def test_ut09_04_window_order_boundary() -> None:
    """UT09-04 window bounds: first must be < second, second must be <= 3650."""
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(
            {"version": 1, "reports": {"prior_outcomes_window_days": [60, 60]}}
        )
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(
            {"version": 1, "reports": {"prior_outcomes_window_days": [-1, 60]}}
        )
    with pytest.raises(ValidationError):
        s.AppConfig.model_validate(
            {"version": 1, "reports": {"prior_outcomes_window_days": [0, 3651]}}
        )
    cfg = s.AppConfig.model_validate(
        {"version": 1, "reports": {"prior_outcomes_window_days": [0, 3650]}}
    )
    assert cfg.reports.prior_outcomes_window_days == (0, 3650)


def test_ut09_02_settings_module_has_no_heavy_imports() -> None:
    """UT09-02 acceptance check: settings.py imports no duckdb, jinja2 or streamlit."""
    tree = ast.parse(Path(s.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= {"__future__", "functools", "re", "typing", "pydantic"}
    assert not names & {"duckdb", "jinja2", "streamlit"}
