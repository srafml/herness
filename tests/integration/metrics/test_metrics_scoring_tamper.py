"""Integration tests of TH04-06 on a fully scored build file (impl 04 ST04-06; T04-21).

The tiny build (`metrics_tiny`, small catalog) is scored with default steps on a DuckDB file;
then stored numbers are tampered with and the tampering must be detected: a stored row no
longer re-hashes to its evidence (a re-run restores it), a forged query id fails
`score_evidence_coverage`, and a changed input under the same query id is a hash conflict.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from tests.support.metrics_scoring import patches, save_as_file, small_catalog, tiny_with_facts
from tests.support.metrics_tiny import BUILD_ID

from herness.core.errors import SchemaViolation
from herness.metrics.evidence import result_hash
from herness.metrics.scoring import run_scoring

pytestmark = pytest.mark.integration

_FUNDING_ROWS = "SELECT * EXCLUDE (query_ids) FROM score.funding"


@pytest.fixture
def scored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[duckdb.DuckDBPyConnection]:
    """A build file scored with default steps (all checks run; the warn may fail)."""
    for obj, name, value in patches(small_catalog()):
        monkeypatch.setattr(obj, name, value)
    path = tmp_path / "build.duckdb"
    mem = tiny_with_facts()
    try:
        save_as_file(mem, path)
    finally:
        mem.close()
    con = duckdb.connect(str(path))
    try:
        report = run_scoring(BUILD_ID, con=con)
        assert report.steps_done[-1] == "check"
        yield con
    finally:
        con.close()


def _funding_qid(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute("SELECT DISTINCT query_ids[1] FROM score.funding").fetchall()
    assert len(row) == 1
    return str(row[0][0])


def _evidence_hash(con: duckdb.DuckDBPyConnection, qid: str) -> str:
    row = con.execute("SELECT result_hash FROM meta.evidence WHERE query_id = ?", [qid]).fetchone()
    assert row is not None
    return str(row[0])


def _table_hash(con: duckdb.DuckDBPyConnection) -> str:
    res = con.execute(_FUNDING_ROWS)
    columns = [(str(d[0]), str(d[1])) for d in res.description or ()]
    return result_hash(columns, res.fetchall())


def test_st04_06_edited_funding_value_detected_and_rerun_restores(
    scored: duckdb.DuckDBPyConnection,
) -> None:
    """ST04-06 an edited `score.funding` value: the stored rows no longer hash to the
    evidence `result_hash` of their query, and re-running `portfolio` (which reads it) fails
    as a rerun hash mismatch; re-running `funding` re-derives the same hash
    (no conflict) and rewrites the edited row."""
    qid = _funding_qid(scored)
    recorded = _evidence_hash(scored, qid)
    assert _table_hash(scored) == recorded
    scored.execute("UPDATE score.funding SET confidence = 0.9 WHERE rank = 1")
    assert _table_hash(scored) != recorded  # the tampering is visible against the evidence
    # product path: re-running the step that reads score.funding gives its pinned query id a
    # different result, which the evidence hash comparison rejects
    with pytest.raises(SchemaViolation, match="nondeterministic result"):
        run_scoring(BUILD_ID, steps=["portfolio"], con=scored)
    run_scoring(BUILD_ID, steps=["funding"], con=scored)
    assert _funding_qid(scored) == qid
    assert _evidence_hash(scored, qid) == recorded
    assert _table_hash(scored) == recorded


def test_st04_06_forged_query_id_fails_evidence_coverage(
    scored: duckdb.DuckDBPyConnection,
) -> None:
    """ST04-06 a `score.funding` row pointing at a query id with no evidence makes
    `score_evidence_coverage` fail on the re-run check: SchemaViolation, dq row failed."""
    scored.execute("UPDATE score.funding SET query_ids = ['q_forged0000000001'] WHERE rank = 1")
    with pytest.raises(
        SchemaViolation, match=r"^scoring invariants failed: .*score_evidence_coverage"
    ):
        run_scoring(BUILD_ID, steps=["check"], con=scored)
    row = scored.execute(
        "SELECT value, passed FROM meta.dq_result WHERE check_name = 'score_evidence_coverage'"
    ).fetchone()
    assert row == (1.0, False)


def test_st04_06_changed_input_is_rerun_hash_mismatch(
    scored: duckdb.DuckDBPyConnection,
) -> None:
    """ST04-06 an edited fact value read by the funding queries: re-running `funding` gives
    the same query id a different result, which the evidence hash comparison rejects as
    nondeterministic (the funding tables stay as they were)."""
    before = sorted(scored.execute(_FUNDING_ROWS).fetchall(), key=repr)
    scored.execute("UPDATE metrics.incident_fact SET total_usd = total_usd + 1000")
    with pytest.raises(SchemaViolation, match="nondeterministic result"):
        run_scoring(BUILD_ID, steps=["funding"], con=scored)
    assert sorted(scored.execute(_FUNDING_ROWS).fetchall(), key=repr) == before
