"""Security tests of privacy deletion (impl 10 T10-29): ST10-50 (TH10-42), ST10-57 (TH10-50).

Both run on the planted stores of ``privacy_env`` after the IT10-06 flow; the byte search
covers every file under ``data/`` (ops store with its WAL, lake, caches, vectors, warehouse,
traces, logs).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.privacy_env import (  # noqa: F401 - fixture import
    BASE,
    DESC,
    OLD_BUILD,
    RID,
    PrivacyEnv,
    privacy_env,
)

from herness.core.errors import ModelUnavailable

pytestmark = pytest.mark.integration

TOKENS = (DESC.encode(), DESC.split()[0].encode())  # the phrase and its unique word (FTS terms)


def _hits(root: Path) -> dict[str, int]:
    """``relative path -> occurrences`` of any description token in the bytes of each file."""
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            body = path.read_bytes()
            count = sum(body.count(token) for token in TOKENS)
            if count:
                found[path.relative_to(root).as_posix()] = count
    return found


def test_st10_50_no_description_bytes_left_in_data(
    privacy_env: PrivacyEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST10-50 after IT10-06 a byte search for the record's unique description text across
    ``data/`` finds nothing; the audit log holds the record_id only."""
    env = privacy_env
    before = _hits(env.data)
    for planted in ("ops.sqlite", "traces/run_aaaa.jsonl", f"warehouse/wh-{OLD_BUILD}.duckdb"):
        assert planted in before  # the search sees the planted plain bytes
    env.delete_end_to_end(monkeypatch)
    assert _hits(env.data) == {}
    audit = b"".join(p.read_bytes() for p in (env.data / "logs").glob("audit-*.jsonl"))
    assert RID.encode() in audit
    assert all(token not in audit for token in TOKENS)


def test_st10_57_memory_holds_nothing_citing_the_record(
    privacy_env: PrivacyEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST10-57 after IT10-06 memory recall and FTS search for the description text and the
    record_id, and a vector lookup of the purged memory IDs, return nothing citing it."""
    env = privacy_env
    assert env.fts(DESC.split()[0]) != []
    env.delete_end_to_end(monkeypatch)
    for query in (DESC, RID):
        hits = env.memory.recall(query, k=20)
        assert [h for h in hits if h.item.memory_id in env.cited] == []
        assert all(DESC not in h.item.content for h in hits)
    assert env.fts(DESC.split()[0]) == []
    assert env.fts('"INC0042"') == []
    assert env.vectors.vectors(env.cited) == {}
    assert env.memory_ids() == env.kept


def test_st10_57_step_3b_failure_then_retry(privacy_env: PrivacyEnv) -> None:  # noqa: F811
    """ST10-57 a deletion run whose step 3b fails once leaves the request running with step
    3b failed and the memory items in place; the retry completes 3b and purges them."""
    env = privacy_env
    calls: list[str] = []

    def flaky(record_id: str) -> int:
        calls.append(record_id)
        if len(calls) == 1:
            msg = "memory vector store unavailable"
            raise ModelUnavailable(msg)
        return env.memory.purge(record_id=record_id)

    with pytest.raises(ModelUnavailable):
        env.run(dict(BASE), memory_purge=flaky)
    req = env.request()
    assert req.status == "running"
    assert req.steps["3b"]["status"] == "failed"
    assert req.steps["3b"]["error"] == "ModelUnavailable"
    assert set(env.cited) <= set(env.memory_ids())
    outcome = env.run(dict(BASE), memory_purge=flaky)
    assert "waiting_for" in outcome.result
    req = env.request()
    assert req.steps["3b"] == {**req.steps["3b"], "status": "done", "error": None}
    assert req.steps["3b"]["counts"] == {"memory_items": 2}
    assert env.memory_ids() == env.kept
    assert env.vectors.vectors(env.cited) == {}
    assert calls == [RID, RID]
