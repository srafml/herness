"""Foundation benchmarks (impl 00 §10, §11.6). Run: pytest -m "integration and slow" tests/bench."""

import time
from pathlib import Path
from typing import Any

import pytest

from herness.core import ids, numbers
from herness.core.logging import configure_logging, get_logger, reset_logging
from tools import check_traceability

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _p95(benchmark: Any) -> float:
    data = sorted(benchmark.stats.stats.data)
    return float(data[int(0.95 * (len(data) - 1))])


def test_bt00_01_ulid_throughput() -> None:
    """BT00-01 at least 200,000 new_ulid calls per second."""
    start = time.perf_counter()
    for _ in range(1_000_000):
        ids.new_ulid()
    rate = 1_000_000 / (time.perf_counter() - start)
    assert rate >= 200_000


def test_bt00_02_query_id_p95(benchmark: Any) -> None:
    """BT00-02 query_id on 2 KB SQL with 10 params: p95 under 50 microseconds."""
    sql = "SELECT " + ", ".join(f"col_{i}" for i in range(250)) + " FROM t"  # noqa: S608 - benchmark text, never executed
    params = {f"p{i}": i for i in range(10)}
    benchmark.pedantic(
        ids.query_id, args=(sql, params, "20260924-211403-ABCDEF"), rounds=10_000, iterations=1
    )
    assert _p95(benchmark) < 50e-6


def test_bt00_03_file_logging_throughput(tmp_path: Any) -> None:
    """BT00-03 10,000 INFO lines of 10 fields through the file handler in under 1 s."""

    def passthrough(logger: object, method_name: str, event_dict: Any) -> Any:
        return event_dict

    configure_logging("INFO", log_dir=tmp_path, scrubber=passthrough, stderr=False)
    log = get_logger("core.bench")
    fields = {f"f{i}": i for i in range(10)}
    start = time.perf_counter()
    for _ in range(10_000):
        log.info("core.bench.line", **fields)
    elapsed = time.perf_counter() - start
    reset_logging()
    assert elapsed < 1.0


def _bench_text() -> str:
    parts: list[str] = []
    for i in range(1000):
        parts.append(f"metric [[n{i}]] rose")
        if i % 2 == 0:
            parts.append(f"in {1990 + i % 30}")
        if i % 20 == 0:
            parts.append(f"by {i + 3} units")
    text = " ".join(parts)
    return (text + " " + "padding " * 20_000)[:100_000]


def test_bt00_05_find_uncited_median(benchmark: Any) -> None:
    """BT00-05 find_uncited on a 100,000-character text: median under 100 ms."""
    allowed = numbers.compile_allowed_patterns(
        [
            r"\b(19|20)\d{2}\b",
            r"\d{4}-\d{2}-\d{2}",
            r"Q[1-4] \d{4}",
            r"(INC|CHG|PRB)\d+",
            r"[A-Z][A-Z0-9]+-\d+",
        ]
    )
    text = _bench_text()
    benchmark.pedantic(numbers.find_uncited, args=(text, allowed), rounds=100, iterations=1)
    assert benchmark.stats.stats.median < 0.1


def test_bt00_04_traceability_on_repo(capsys: pytest.CaptureFixture[str]) -> None:
    """BT00-04 check_traceability.main on the repository finishes in under 5 s."""
    root = Path(__file__).resolve().parents[2]
    start = time.perf_counter()
    # The exit code is deliberately ignored: BT00-04 measures run time only, and the repo still
    # carries doc defects from specs 01-11 (IT00-02 tracks the result).
    check_traceability.main(["--root", str(root)])
    elapsed = time.perf_counter() - start
    assert "defined=" in capsys.readouterr().out
    assert elapsed < 5.0
