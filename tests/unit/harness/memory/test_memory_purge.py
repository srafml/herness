"""Tests of the memory erasure path (impl 07 U07-57, U07-100; UT07-38, UT07-88; T07-26, R-54).

UT07-38 drives `MemoryLifecycle.purge` on the migrated `ops_store` with a recording vector
fake that can fail like an unavailable LanceDB; UT07-88 checks that `MemoryStore.purge`
validates its selector and delegates to a fake lifecycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._lifecycle_env import (
    REVIEWER,
    FakeVectors,
    make_lifecycle,
    review_of,
)
from tests.unit.harness.memory._write_env import (
    NOW,
    PATTERNS,
    make_writer,
    memory_rows,
    provenance,
)

from herness.core import time as clock
from herness.core.errors import ModelUnavailable, ToolInputError
from herness.core.ids import new_ulid
from herness.harness.memory import MemoryStore
from herness.harness.memory._purge_review import PURGED_FIELDS
from herness.harness.memory.lifecycle import MemoryLifecycle, purge_args_ok
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store.ops import core, shared
from herness.store.ops import memory as ops

pytestmark = pytest.mark.unit

RECORD = "servicenow:incident:INC0042"
PERSON = "c" * 32
OTHER = "d" * 32
PLANTED = "zebrafish outage at the riverside depot"  # the planted record text


@dataclass
class PurgeVectors(FakeVectors):
    """`FakeVectors` plus `delete`: records the ids, or fails `fail_deletes` times."""

    deleted: list[str] = field(default_factory=list)
    fail_deletes: int = 0

    def delete(self, memory_ids: list[str]) -> None:
        """Record the deleted ids, or fail like a down LanceDB."""
        if self.fail_deletes:
            self.fail_deletes -= 1
            msg = "memory vector store unavailable: delete"
            raise ModelUnavailable(msg)
        self.deleted.extend(memory_ids)


def _lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[MemoryLifecycle, PurgeVectors]:
    env = make_lifecycle(tmp_path, monkeypatch)
    fake = PurgeVectors()
    life = MemoryLifecycle(MemoryConfig(), conn_factory=core.connection, vectors=cast(Any, fake),
                           writer=env.writer.writer, redactor=env.writer.redactor)  # fmt: skip
    return life, fake


def _seed(content: str, *, author: str = OTHER, status: str = "active", review: bool = False,
          cites: str | None = None, **data: Any) -> str:  # fmt: skip
    """One `glossary` item by `author`, citing record `cites` as an entity; `review` links a
    pending `memory_write` review item."""
    memory_id = "mem_" + new_ulid()
    entities = [] if cites is None else [{"type": "record", "id": cites}]
    stored: dict[str, Any] = {"term": "t", "definition": "d", "entities": entities, **data}
    prov = provenance(author_ref=author).model_dump(mode="json")
    if review:  # the design 07 §4.1 memory_write payload shape
        payload = {"memory_id": memory_id, "layer": "semantic", "kind": "glossary",
                   "content": content, "numbers": stored.get("numbers", []),
                   "entities": entities, "provenance": prov, "flags": []}  # fmt: skip
        stored["review_item_id"] = shared.create_review_item("memory_write", payload, now=NOW)
    ops.insert_memory_item({
        "memory_id": memory_id, "layer": "semantic", "kind": "glossary", "content": content,
        "data": stored, "provenance": prov,
        "confidence": 0.8, "status": status, "created_at": clock.format_utc(NOW),
        "expires_at": None, "last_used_at": None, "use_count": 0,
    })  # fmt: skip
    return memory_id


def _ids() -> list[str]:
    return sorted(r["memory_id"] for r in memory_rows())


def _snapshot() -> tuple[list[dict[str, Any]], list[tuple[Any, ...]]]:
    reviews = core.read_all("SELECT item_id, status, payload FROM review_item ORDER BY item_id")
    return memory_rows(), [tuple(r) for r in reviews]


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


# --- UT07-38 MemoryLifecycle.purge ---------------------------------------------------------


def test_ut07_38_vector_failure_then_retry_erases_the_record(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 a failing vector delete raises ModelUnavailable with SQLite unchanged; the
    retry deletes vectors and rows, blanks and rejects the review item; a rerun returns 0."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    pending = _seed(PLANTED, status="pending_approval", review=True, cites=RECORD)
    cited = _seed("a rule from the incident", cites=RECORD)
    other = _seed(f"churn definition mentioning {RECORD}")  # a mention is not a citation
    review_id = memory_rows()[0]["data"]["review_item_id"]
    before = _snapshot()
    fake.fail_deletes = 1

    with capture_logs() as logs, pytest.raises(ModelUnavailable) as info:
        life.purge(record_id=RECORD, now=NOW)
    assert str(info.value) == "memory vector purge failed"
    assert _snapshot() == before
    (failed,) = _events(logs, "memory.purge.vector_failed")
    assert failed == {"event": "memory.purge.vector_failed", "log_level": "error",
                      "component": "memory", "count": 2}  # fmt: skip

    with capture_logs() as logs:
        assert life.purge(record_id=RECORD, now=NOW) == 2
    assert sorted(fake.deleted) == sorted([pending, cited])
    assert _ids() == [other]
    review = review_of(review_id)
    assert (review.status, review.decided_by, review.note) == ("rejected", "system", "purged")
    assert review.payload["content"] == ""
    (done,) = _events(logs, "memory.purge.completed")
    assert done == {"event": "memory.purge.completed", "log_level": "info", "component": "memory",
                    "count": 2, "scrubbed": 0, "review_items": 1}  # fmt: skip
    assert PLANTED not in str(logs)
    assert RECORD not in str(logs)
    assert len(fake.deleted) == 2  # the mentioning item's vector is kept

    assert life.purge(record_id=RECORD, now=NOW) == 0  # idempotent rerun
    assert _ids() == [other]


def _payload_text(item_id: str) -> str:
    rows = core.read_all("SELECT payload FROM review_item WHERE item_id = ?", (item_id,))
    return str(rows[0][0])


def _blanked(item_id: str) -> None:
    payload = review_of(item_id).payload
    assert {k: payload[k] for k in PURGED_FIELDS} == {
        "content": "", "statement": "", "entities": [], "numbers": [], "provenance": {}
    }  # fmt: skip


def test_ut07_38_review_payloads_keep_no_purged_data(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 (T07-26 review ruling, TH07-21) the memory_write payload (content, entities,
    numbers citing the record by key, provenance) and a derived item's payload (statement,
    entities) keep nothing of the record's text or id after purge(record_id)."""
    del ops_store
    life, _ = _lifecycle(tmp_path, monkeypatch)
    ref = {"id": "n1", "value": 4, "unit": "count", "query_id": "q_" + "a" * 16,
           "column": "n", "row_key": {"key": RECORD.split(":", 2)[2]}}  # fmt: skip
    derived = shared.create_review_item("weight_change", {
        "source_memory_id": "mem_" + new_ulid(), "statement": PLANTED,
        "entities": [{"type": "record", "id": RECORD}], "suggested_action": "weight_change",
        "effective_date": None}, now=NOW)  # fmt: skip
    pending = _seed(PLANTED, status="pending_approval", review=True, numbers=[ref],
                    derived_review_item_id=derived)  # fmt: skip
    review_id = memory_rows()[0]["data"]["review_item_id"]
    assert RECORD.split(":", 2)[2] in _payload_text(review_id)

    assert life.purge(record_id=RECORD, now=NOW) == 1
    assert _ids() == []
    for item_id in (review_id, derived):
        _blanked(item_id)
        assert PLANTED not in _payload_text(item_id)
        assert RECORD.split(":", 2)[2] not in _payload_text(item_id)
        assert (review_of(item_id).status, review_of(item_id).note) == ("rejected", "purged")
    assert review_of(review_id).payload["memory_id"] == pending  # the skeleton stays


def test_ut07_38_author_purge_leaves_no_author_in_reviews(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 purge(author_ref) blanks the review payload provenance naming the person."""
    del ops_store
    life, _ = _lifecycle(tmp_path, monkeypatch)
    _seed("their correction", author=PERSON, status="pending_approval", review=True)
    review_id = memory_rows()[0]["data"]["review_item_id"]
    assert PERSON in _payload_text(review_id)
    assert life.purge(author_ref=PERSON, now=NOW) == 1
    _blanked(review_id)
    assert PERSON not in _payload_text(review_id)


def test_ut07_38_dangling_review_link_does_not_block(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 a linked review item that no longer exists (NotFoundError on both review
    functions) is skipped: the purge still removes the item."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    item = _seed("cites", cites=RECORD, review_item_id="rev_" + new_ulid())
    assert life.purge(record_id=RECORD, now=NOW) == 1
    assert _ids() == []
    assert fake.deleted == [item]


def test_ut07_38_item_citing_after_the_dry_run_is_erased_too(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 an item that starts citing the record between the dry run and the transaction
    is deleted with its review item blanked; its late vector delete failing is only logged
    (maintenance removes the orphan)."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    first = _seed("cites", cites=RECORD)
    late: list[str] = []
    real_delete = fake.delete

    def delete(memory_ids: list[str]) -> None:
        if not late:  # the first (dry-run) delete: a concurrent writer adds a citing item
            late.append(_seed(PLANTED, status="pending_approval", review=True, cites=RECORD))
            real_delete(memory_ids)
            fake.fail_deletes = 1
            return
        real_delete(memory_ids)

    monkeypatch.setattr(fake, "delete", delete)
    with capture_logs() as logs:
        assert life.purge(record_id=RECORD, now=NOW) == 2
    assert _ids() == []
    assert fake.deleted == [first]
    (warned,) = _events(logs, "memory.vector.sync_failed")
    assert (warned["op"], warned["count"]) == ("purge", 1)
    review = shared.list_review_items(kind="memory_write")[0]
    assert (review.status, review.note, review.payload["content"]) == ("rejected", "purged", "")


def test_ut07_38_author_purge_scrubs_history(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 items authored by the person go; the person's `provenance_history` entries
    leave an item kept for its other author."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    authored = _seed("their definition", author=PERSON)
    history = [{"author_ref": PERSON, "via": "chat"}, {"author_ref": OTHER, "via": "cli"}]
    merged = _seed("shared definition", provenance_history=history)

    assert life.purge(author_ref=PERSON, now=NOW) == 1
    assert fake.deleted == [authored]
    assert _ids() == [merged]
    assert memory_rows()[0]["data"]["provenance_history"] == [{"author_ref": OTHER, "via": "cli"}]
    assert life.purge(author_ref=PERSON, now=NOW) == 0


def test_ut07_38_decided_review_item_is_blanked_not_redecided(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 an already approved review item keeps its decision (conflict ignored) but its
    payload content is blanked."""
    del ops_store
    life, _ = _lifecycle(tmp_path, monkeypatch)
    item = _seed(PLANTED, status="pending_approval", review=True, cites=RECORD)
    review_id = memory_rows()[0]["data"]["review_item_id"]
    life.approve(item, REVIEWER, now=NOW)

    assert life.purge(record_id=RECORD) == 1
    review = review_of(review_id)
    assert (review.status, review.decided_by) == ("approved", REVIEWER)
    assert review.payload["content"] == ""


def test_ut07_38_unknown_record_changes_nothing(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-38 a record no item cites removes nothing and returns 0."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    _seed("cites", status="pending_approval", review=True, cites=RECORD)
    before = _snapshot()
    assert life.purge(record_id="jira:issue:NOPE-1", now=NOW) == 0
    assert _snapshot() == before
    assert fake.deleted == []


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"record_id": RECORD, "author_ref": PERSON}, {"record_id": "INC0042"},
     {"record_id": "a:b:" + "x" * 297}, {"author_ref": PERSON.upper()}, {"author_ref": 5}],
)  # fmt: skip
def test_ut07_38_needs_exactly_one_selector(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
) -> None:  # fmt: skip
    """UT07-38 neither, both or an off-pattern selector is ToolInputError; nothing changes."""
    del ops_store
    life, fake = _lifecycle(tmp_path, monkeypatch)
    _seed("cites", author=PERSON, cites=RECORD)
    with pytest.raises(ToolInputError, match=r"^purge needs exactly one of author_ref, record_id$"):
        life.purge(**kwargs)
    assert len(_ids()) == 1
    assert fake.deleted == []
    assert not purge_args_ok(kwargs.get("record_id"), kwargs.get("author_ref"))


# --- UT07-88 MemoryStore.purge -------------------------------------------------------------


class FakeLifecycle:
    """Records `purge` calls and returns `count`."""

    def __init__(self, count: int) -> None:
        self.count, self.calls = count, list[dict[str, Any]]()

    def purge(self, **kwargs: Any) -> int:
        """Record the call."""
        self.calls.append(kwargs)
        return self.count


@pytest.fixture
def store(ops_store: OpsStoreHandle, tmp_path: Path) -> MemoryStore:
    """A facade on the writer env's collaborators."""
    del ops_store
    env = make_writer(tmp_path)
    return MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=env.vectors, embedder=Embedder(env.embed, model_name="bge-m3"),
        redactor=env.redactor, llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",),
        data_root=tmp_path / "data",
    )  # fmt: skip


def test_ut07_88_delegates_with_the_selector(store: MemoryStore) -> None:
    """UT07-88 `purge(record_id)` and `purge(author_ref=…)` delegate to the lifecycle with
    the right selector and return its count."""
    fake = FakeLifecycle(3)
    store._lifecycle = cast(Any, fake)
    assert store.purge("src:incident:INC1") == 3
    assert store.purge(author_ref=PERSON, now=NOW) == 3
    assert fake.calls == [
        {"record_id": "src:incident:INC1", "author_ref": None, "now": None},
        {"record_id": None, "author_ref": PERSON, "now": NOW},
    ]


@pytest.mark.parametrize(
    "args",
    [((), {}), (("src:incident:INC1",), {"author_ref": PERSON}), (("INC1",), {}),
     ((), {"author_ref": "not-hex"})],
)  # fmt: skip
def test_ut07_88_needs_exactly_one_selector(
    store: MemoryStore, args: tuple[tuple[Any, ...], dict[str, Any]]
) -> None:
    """UT07-88 both, neither or an off-pattern selector is ToolInputError; no delegation."""
    fake = FakeLifecycle(3)
    store._lifecycle = cast(Any, fake)
    with pytest.raises(ToolInputError, match=r"^purge needs exactly one of record_id, author_ref$"):
        store.purge(*args[0], **args[1])
    assert fake.calls == []
