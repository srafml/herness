"""Bench recorder and regression rule (U11-51): UT11-57, UT11-58."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from tests.support import bench
from tests.support.bench import BenchRecord, BenchRecorder, BenchTarget, compare_bench

pytestmark = pytest.mark.unit

_PLUGINS = ("-p", "tests.support.bench", "-p", "no:asyncio", "-p", "no:cacheprovider")
_NAME = re.compile(r"^\d{8}-\d{6}-([0-9a-f]{12}|nogit)\.json$")


def _rec(target_id: str, value: float, better: str = "lower") -> dict[str, Any]:
    return {
        "target_id": target_id,
        "owner_spec": "11",
        "metric": "seconds",
        "value": value,
        "unit": "s",
        "better": better,
        "threshold": 100.0,
        "passed": True,
        "scale": "tiny",
    }


def _doc(*records: dict[str, Any], gate: bool = True) -> dict[str, Any]:
    return {"schema": 1, "gate": gate, "hardware": {}, "records": list(records)}


def test_ut11_57_lower_is_better_plus_25_percent_flagged() -> None:
    """UT11-57 lower-is-better +25 % is a regression at max_regression 0.20."""
    found = compare_bench(
        _doc(_rec("BT11-03", 12.5)), _doc(_rec("BT11-03", 10.0)), max_regression=0.2
    )
    assert [(r.target_id, r.metric) for r in found] == [("BT11-03", "seconds")]
    assert found[0].previous == 10.0
    assert found[0].current == 12.5
    assert found[0].change == pytest.approx(0.25)


def test_ut11_57_lower_is_better_plus_15_percent_not_flagged() -> None:
    """UT11-57 lower-is-better +15 % (and any improvement) is not a regression."""
    current = _doc(_rec("BT11-03", 11.5), _rec("BT11-02", 5.0))
    previous = _doc(_rec("BT11-03", 10.0), _rec("BT11-02", 50.0))
    assert compare_bench(current, previous, max_regression=0.2) == []


def test_ut11_57_higher_is_better_minus_25_percent_flagged() -> None:
    """UT11-57 higher-is-better -25 % is flagged; -15 % is not."""
    previous = _doc(_rec("BT10-03", 8000.0, "higher"), _rec("BT01-01", 8000.0, "higher"))
    current = _doc(_rec("BT10-03", 6000.0, "higher"), _rec("BT01-01", 6800.0, "higher"))
    found = compare_bench(current, previous, max_regression=0.2)
    assert [r.target_id for r in found] == ["BT10-03"]
    assert found[0].change == pytest.approx(-0.25)


def test_ut11_57_only_keys_present_in_both_are_compared() -> None:
    """UT11-57 a target or metric missing from either document is skipped."""
    current = _doc(_rec("BT11-03", 99.0), {**_rec("BT11-02", 99.0), "metric": "rss"})
    previous = _doc(_rec("BT11-02", 1.0), _rec("BT11-01", 1.0))
    assert compare_bench(current, previous, max_regression=0.2) == []


def _record() -> BenchRecord:
    return BenchRecord(
        target_id="BT11-03",
        owner_spec="11",
        metric="seconds",
        value=3.5,
        unit="s",
        better="lower",
        threshold=5.0,
        passed=True,
        scale="tiny",
    )


def test_ut11_58_write_file_name_pattern_and_content(tmp_path: Path) -> None:
    """UT11-58 `write` names the file `YYYYMMDD-HHMMSS-<sha>.json` and stores schema 1,
    the gate flag, hardware, the records and the embedded pytest-benchmark JSON."""
    recorder = BenchRecorder()
    recorder.record(_record())
    embedded = tmp_path / "pb.json"
    embedded.write_text(json.dumps({"benchmarks": []}), encoding="utf-8")
    path = recorder.write(tmp_path / "data" / "bench", gate=True, benchmark_json=embedded)
    assert path.parent == tmp_path / "data" / "bench"
    assert _NAME.match(path.name), path.name
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == 1
    assert doc["gate"] is True
    assert set(doc["hardware"]) == {"cpu_count", "ram_gb", "platform"}
    assert doc["records"] == [_record().as_dict()]
    assert doc["pytest_benchmark"] == {"benchmarks": []}
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # atomic: no temp left


def test_ut11_58_git_sha_falls_back_to_nogit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-58 the sha part is `nogit` when `git rev-parse` fails."""
    monkeypatch.setattr(bench, "_GIT", "git-command-that-does-not-exist")
    recorder = BenchRecorder()
    recorder.record(_record())
    path = recorder.write(tmp_path, gate=False, benchmark_json=None)
    assert path.name.endswith("-nogit.json")
    assert json.loads(path.read_text(encoding="utf-8"))["pytest_benchmark"] is None


def test_ut11_58_record_median_and_value() -> None:
    """UT11-58 `record_median` gates on the median of the repeats; `record_value` records
    one measurement; both judge `passed` against the threshold in the `better` direction."""
    recorder = BenchRecorder()
    target = BenchTarget("BT11-03", "11", "seconds", "s", 5.0, "tiny")
    rec = bench.record_median(recorder, target, [4.0, 6.0, 4.5])
    assert (rec.value, rec.passed) == (4.5, True)
    high = BenchTarget("BT10-03", "10", "records_per_s", "1/s", 5000.0, "tiny", "higher")
    assert bench.record_value(recorder, high, 4000.0).passed is False
    assert [r.target_id for r in recorder.records] == ["BT11-03", "BT10-03"]


def test_ut11_58_max_regression_from_eval_yaml() -> None:
    """UT11-58 the session threshold is `eval.yaml: thresholds.bench_regression_max`."""
    assert bench.max_regression() == 0.20


_INNER = """
import pytest
from tests.support.bench import BenchTarget, record_value

def test_inner(bench_recorder):
    record_value(bench_recorder, BenchTarget("BT11-03", "11", "seconds", "s", 5.0, "tiny"), {v})
"""


def _older_gate_file(bench_dir: Path, value: float, *, gate: bool = True) -> None:
    bench_dir.mkdir(parents=True, exist_ok=True)
    doc = _doc(_rec("BT11-03", value), gate=gate)
    (bench_dir / "20000101-000000-nogit.json").write_text(json.dumps(doc), encoding="utf-8")


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: float) -> Any:
    monkeypatch.setenv("HERNESS_BENCH_GATE", "1")
    monkeypatch.setenv("HERNESS_BENCH_DIR", str(pytester.path / "data" / "bench"))
    pytester.makepyfile(test_inner=_INNER.format(v=value))
    return pytester.runpytest_inprocess(*_PLUGINS, "-q")


def test_ut11_58_gate_regression_fails_the_session(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-58 with `HERNESS_BENCH_GATE=1` and an older gate file at 3.0 s, a passing test
    recording 4.0 s (+33 %) fails the session with exit code 1 and writes a gate file."""
    bench_dir = pytester.path / "data" / "bench"
    _older_gate_file(bench_dir, 3.0)
    result = _run(pytester, monkeypatch, 4.0)
    result.assert_outcomes(passed=1)
    assert result.ret == 1
    written = [p for p in bench_dir.iterdir() if not p.name.startswith("2000")]
    assert len(written) == 1
    assert _NAME.match(written[0].name)
    assert json.loads(written[0].read_text(encoding="utf-8"))["gate"] is True


def test_ut11_58_gate_within_threshold_passes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-58 +10 % against the newest earlier gate file keeps exit code 0; a newer
    non-gate file is not the comparison base."""
    bench_dir = pytester.path / "data" / "bench"
    _older_gate_file(bench_dir, 3.0)
    non_gate = _doc(_rec("BT11-03", 1.0), gate=False)
    (bench_dir / "20000102-000000-nogit.json").write_text(json.dumps(non_gate), encoding="utf-8")
    result = _run(pytester, monkeypatch, 3.3)
    assert result.ret == 0


def test_ut11_58_no_record_writes_nothing(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-58 a session without records writes no bench file."""
    monkeypatch.setenv("HERNESS_BENCH_DIR", str(pytester.path / "data" / "bench"))
    pytester.makepyfile(test_inner="def test_inner(bench_recorder):\n    pass\n")
    result = pytester.runpytest_inprocess(*_PLUGINS, "-q")
    assert result.ret == 0
    assert not (pytester.path / "data" / "bench").exists()
