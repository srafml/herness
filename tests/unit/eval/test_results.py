"""Tests for herness.eval.results (U11-63): UT11-55, UT11-56 and controller checks (T11-28)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from herness.core.errors import ConfigError
from herness.eval.grading import GradeResult, UnsupportedReport
from herness.eval.results import QuestionResult, ResultsLog

pytestmark = pytest.mark.unit


def _result(qid: str = "G01", repeat: int = 1, **overrides: Any) -> QuestionResult:
    values: dict[str, Any] = {
        "id": qid, "repeat": repeat, "status": "passed",
        "checks": [GradeResult(check="numeric:n", status="passed", detail={"value": 3})],
        "reference": {"value": 3}, "final_text": "Team One had [[n1]] incidents.",
        "numbers": [{"id": "n1", "value": 3, "unit": "count", "query_id": "q_0123456789abcdef",
                     "column": "n", "row_key": None}],
        "unsupported": UnsupportedReport(total=1, unsupported=0, stray_numerals=[],
                                         orphan_markers=[], stale_refs=[], rate=0.0),
        "tokens_in": 120, "tokens_out": 40, "cost_usd": "0.0012", "latency_s": 1.5,
        "retries": 0, "fallbacks": 0, "repairs": 0,
        "pipeline_run_id": "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X", "draft_mode": None,
        "trace_path": None, "error": None,
    }  # fmt: skip
    values.update(overrides)
    return QuestionResult.model_validate(values)


def test_ut11_55_truncated_last_line_ignored_and_append_readable(tmp_path: Path) -> None:
    """UT11-55 a truncated last line is ignored by done_keys/read_all; the next append is
    written on a fresh line and is readable; the file is only ever appended to."""
    path = tmp_path / "results.jsonl"
    log = ResultsLog(path)
    log.append(_result("G01", 1))
    complete = path.read_bytes()
    with path.open("ab") as fh:  # a crash mid-write: half a JSON line, no newline
        fh.write(_result("G02", 1).model_dump_json().encode()[:40])
    assert log.done_keys() == {("G01", 1)}
    assert [r.id for r in log.read_all()] == ["G01"]

    log.append(_result("G02", 1))
    data = path.read_bytes()
    assert data.startswith(complete)  # append-only: earlier bytes untouched
    assert data.endswith(b"\n")
    assert log.done_keys() == {("G01", 1), ("G02", 1)}
    results = ResultsLog(path).read_all()
    assert [(r.id, r.repeat) for r in results] == [("G01", 1), ("G02", 1)]
    assert results[1] == _result("G02", 1)
    assert json.loads(data.splitlines()[-1])["id"] == "G02"


def test_ut11_56_append_existing_key_refused(tmp_path: Path) -> None:
    """UT11-56 appending an existing `(id, repeat)` is refused with ConfigError; the file
    is unchanged; another repeat of the same id is accepted."""
    path = tmp_path / "results.jsonl"
    log = ResultsLog(path)
    log.append(_result("G01", 1))
    before = path.read_bytes()
    with pytest.raises(ConfigError, match=r"G01.*1"):
        log.append(_result("G01", 1, status="failed"))
    assert path.read_bytes() == before
    log.append(_result("G01", 2))
    assert ResultsLog(path).done_keys() == {("G01", 1), ("G01", 2)}


def test_cv_t11_28_results_log_missing_file_and_blank_lines(tmp_path: Path) -> None:
    """A missing file has no keys; blank lines are skipped; the parent dir is created."""
    path = tmp_path / "run" / "results.jsonl"
    log = ResultsLog(path)
    assert log.done_keys() == set()
    assert log.read_all() == []
    log.append(_result("G01", 1))
    with path.open("ab") as fh:
        fh.write(b"\n")
    log.append(_result("G01", 2))
    assert [r.repeat for r in log.read_all()] == [1, 2]


def test_cv_t11_28_results_log_invalid_complete_line_is_config_error(tmp_path: Path) -> None:
    """A complete line that is JSON but not a QuestionResult is corruption: ConfigError."""
    path = tmp_path / "results.jsonl"
    path.write_text(
        '{"id": "G01", "repeat": 1}\n["not", "an", "object"]\n{"id": 7, "repeat": "x"}\n',
        encoding="utf-8",
    )
    log = ResultsLog(path)
    assert log.path == path
    assert log.done_keys() == {("G01", 1)}
    with pytest.raises(ConfigError, match="line 1"):
        log.read_all()


def test_cv_t11_28_results_log_concurrent_appends(tmp_path: Path) -> None:
    """Appends from several threads give one readable line each."""
    log = ResultsLog(tmp_path / "results.jsonl")
    threads = [threading.Thread(target=log.append, args=(_result(f"G{i:02}", 1),))
               for i in range(8)]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log.done_keys() == {(f"G{i:02}", 1) for i in range(8)}
