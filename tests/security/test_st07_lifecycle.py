"""Security tests for the memory lifecycle (impl 07 §11.5; U07-51, U07-52, T07-09).

ST07-04, ST07-12, ST07-17: every test drives the real `MemoryLifecycle` on a migrated ops store
(LanceDB status mirror faked); ST07-12 writes the real spec 10 audit file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import init_sync_config
from tests.unit.harness.memory._lifecycle_env import (
    REVIEWER,
    make_lifecycle,
    review_of,
    row_of,
    seed_item,
)
from tests.unit.harness.memory._write_env import NOW, at_cosine, proposal, unit

from herness.harness.memory import lifecycle as lifecycle_mod
from herness.store.ops import core

pytestmark = pytest.mark.integration

_ENT_A = [{"type": "service", "id": "svc_a"}]
_ENT_B = [{"type": "service", "id": "svc_b"}]


def _glossary(term: str, entities: list[dict[str, str]]) -> dict[str, Any]:
    return {"term": term, "definition": "see content", "entities": entities}


def _snapshot() -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every ops table (FTS shadow tables excluded)."""
    names = [
        r[0]
        for r in core.read_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not str(r[0]).startswith(("memory_fts", "sqlite_"))
    ]
    return {n: [tuple(r) for r in core.read_all(f"SELECT * FROM {n}")] for n in names}  # noqa: S608 - names from sqlite_master


def _audit_lines(logs: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(logs.glob("audit-*.jsonl"))
        for line in path.read_bytes().split(b"\n")
        if line
    ]


# ---------------------------------------------------------------- ST07-04


def test_st07_04_weight_change_approval_only_creates_a_review_item(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-04 approving a weight_change correction: one derived review item, no score write."""
    env = make_lifecycle(tmp_path, monkeypatch)
    data = {"statement": "weight MTTR lower", "effective_date": None,
            "suggested_action": "weight_change"}  # fmt: skip
    memory_id = seed_item(kind="user_correction", data=data, content="weight MTTR lower")

    def no_warehouse(*args: object, **kwargs: object) -> None:
        msg = "the lifecycle opened a DuckDB warehouse"
        raise AssertionError(msg)

    monkeypatch.setattr(duckdb, "connect", no_warehouse)
    before = _snapshot()
    env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    after = _snapshot()
    changed = {name for name in after if after[name] != before.get(name)}
    assert changed == {"memory_item", "review_item"}
    added = len(after["review_item"]) - len(before["review_item"])
    assert added == 1
    derived = review_of(row_of(memory_id)["data"]["derived_review_item_id"])
    assert (derived.kind, derived.status) == ("weight_change", "pending")
    assert not list((tmp_path / "data").rglob("*.duckdb"))
    source = Path(lifecycle_mod.__file__).read_text(encoding="utf-8")
    assert "warehouse" not in source
    assert "score" not in source.replace("score or mapping", "")


# ---------------------------------------------------------------- ST07-12


def test_st07_12_approve_and_reject_record_decided_by_and_audit(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-12 approve and reject: decided_by / approved_by / rejected_by and audit lines."""
    logs = Path(init_sync_config(tmp_path).paths.logs)
    env = make_lifecycle(tmp_path, monkeypatch, real_audit=True)
    approved, rejected = seed_item(), seed_item()
    env.lifecycle.approve(approved, REVIEWER, now=NOW)
    env.lifecycle.reject(rejected, "c" * 32, "not true", now=NOW)
    assert row_of(approved)["data"]["approved_by"] == REVIEWER
    assert row_of(rejected)["data"]["rejected_by"] == "c" * 32
    decisions = {}
    for memory_id in (approved, rejected):
        review = review_of(row_of(memory_id)["data"]["review_item_id"])
        decisions[review.item_id] = (review.status, review.decided_by)
        assert review.decided_at is not None
    lines = [d for d in _audit_lines(logs) if d["event"] == "review_decision"]
    audited = {d["fields"]["item_id"]: (d["fields"]["status"], d["actor"]) for d in lines}
    assert audited == decisions
    assert set(decisions.values()) == {("approved", REVIEWER), ("rejected", "c" * 32)}
    assert all(d["fields"]["kind"] == "memory_write" for d in lines)
    assert "not true" not in "".join(json.dumps(d) for d in lines)


# ---------------------------------------------------------------- ST07-17


def test_st07_17_only_listed_active_conflicts_are_superseded(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-17 a MemoryWriter conflict: approval expires exactly the ids its payload lists."""
    env = make_lifecycle(tmp_path, monkeypatch)
    embed, writer = env.writer.embed, env.writer.writer
    embed.overrides["glossary: Alpha owns billing."] = unit(0)
    embed.overrides["glossary: Gamma owns billing."] = unit(0)
    embed.overrides["glossary: Beta owns billing."] = at_cosine(0.85)
    listed = writer.propose(proposal("Alpha owns billing.", data=_glossary("a", _ENT_A)), now=NOW)
    # as close, but about another entity: never a conflict, never shown to the reviewer
    unlisted = writer.propose(proposal("Gamma owns billing.", data=_glossary("g", _ENT_B)), now=NOW)
    new = writer.propose(proposal("Beta owns billing.", data=_glossary("b", _ENT_A)), now=NOW)
    assert (listed.status, unlisted.status, new.status) == ("active", "active", "pending_approval")
    assert new.review_item_id is not None
    payload = review_of(new.review_item_id).payload
    assert payload["conflicts_with"] == [listed.memory_id]
    assert row_of(new.memory_id)["data"]["conflicts_with"] == [listed.memory_id]
    env.lifecycle.approve(new.memory_id, REVIEWER, now=NOW)
    row = row_of(listed.memory_id)
    assert row["status"] == "expired"
    assert (row["data"]["superseded_by"], row["data"]["expired_reason"]) == (
        new.memory_id, "superseded",
    )  # fmt: skip
    assert row_of(unlisted.memory_id)["status"] == "active"
    assert ([listed.memory_id], "expired") in env.vectors.calls
    assert all(unlisted.memory_id not in ids for ids, _ in env.vectors.calls)


def test_st07_17_listed_but_not_active_ids_are_left_alone(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-17 listed ids that are no longer active (pending, rejected) are not superseded."""
    env = make_lifecycle(tmp_path, monkeypatch)
    pending, rejected = seed_item(), seed_item(status="rejected")
    memory_id = seed_item(data={"conflicts_with": [pending, rejected]})
    env.lifecycle.approve(memory_id, REVIEWER, now=NOW)
    assert row_of(pending)["status"] == "pending_approval"
    assert row_of(rejected)["status"] == "rejected"
    assert all(status != "expired" for _, status in env.vectors.calls)
