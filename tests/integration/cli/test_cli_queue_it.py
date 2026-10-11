"""IT09-21 (CLI part): review-queue and memory decisions through ``herness.cli.main`` (T09-24,
U09-38, U09-97, U09-101; R-33).

``cli_env`` (config tree with roles, OS user, keyring) and ``ops_store`` (migrated ops store)
share one temporary root, so the CLI's ``paths.data`` is the seeded store. The memory store the
CLI builds is replaced by impl 07's real ``MemoryLifecycle`` for ``approve`` / ``reject`` (as
the page half of IT09-21 does), so a ``memory_write`` item is decided by impl 07 in its own
transaction; other kinds go to impl 02 ``decide_review_item``. Audit lines are the real files.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import USER_REF_KEY, CliEnv
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._lifecycle_env import make_lifecycle, review_of, row_of, seed_item

from herness import cli
from herness.core.ids import new_ulid
from herness.harness import memory
from herness.reports.rules import user_ref_for
from herness.store.ops import shared

pytestmark = pytest.mark.integration

ADMIN, REVIEWER = "root", "rev"
QSV = "qs_2026_10"


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _item(kind: str, payload: dict[str, object]) -> str:
    from herness.core import time as clock  # noqa: PLC0415

    return shared.create_review_item(kind, payload, now=clock.now())  # type: ignore[arg-type]


def _audit(root: Path, event: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted((root / "data" / "logs").glob("audit-*.jsonl")):
        lines += [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line["event"] == event]


@pytest.fixture
def env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Roles, the seeded store and a memory store whose decisions are impl 07's lifecycle."""
    cli_env.write_config(admins=[ADMIN], reviewers=[REVIEWER])
    life = make_lifecycle(tmp_path, monkeypatch, real_audit=True)
    calls: list[tuple[str, str, str, str | None]] = []

    def approve(memory_id: str, user_ref: str, note: str | None = None) -> None:
        calls.append(("approve", memory_id, user_ref, note))
        life.lifecycle.approve(memory_id, user_ref, note)

    def reject(memory_id: str, user_ref: str, note: str) -> None:
        calls.append(("reject", memory_id, user_ref, note))
        life.lifecycle.reject(memory_id, user_ref, note)

    store = SimpleNamespace(approve=approve, reject=reject)
    monkeypatch.setattr(memory, "get_memory_store", lambda: store)
    return SimpleNamespace(cli=cli_env, calls=calls, root=ops_store.data_root.parent)


def test_it09_21_decide_every_kind_via_cli(
    env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-21 CLI: `memory approve|reject` by memory id → MemoryStore.approve/reject and the
    linked review item decided in impl 07's transaction (R-33); `review-queue` by item id →
    impl 02 decide_review_item plus a review_decision audit line; a label check cannot be
    approved from the CLI (DD-11) and stays pending."""
    kept, dropped = seed_item(), seed_item(content="churn means something else")
    kept_item = row_of(kept)["data"]["review_item_id"]
    dropped_item = row_of(dropped)["data"]["review_item_id"]
    label = _item("label_check", {
        "record_id": "servicenow:incident:INC0001", "content_hash": "h1",
        "question": "root_cause", "question_set_version": QSV, "answer": "code",
        "probability": 0.55, "decider": "ensemble", "decider_version": "v1",
    })  # fmt: skip
    mapping = _item("mapping_suggestion", {"field": "assignment_group", "value": "Payments",
                                           "target": "T1"})  # fmt: skip
    refused = _item("mapping_suggestion", {"field": "assignment_group", "value": "Billing",
                                           "target": "T2"})  # fmt: skip
    weight = _item("weight_change", {"blocks": ["usd"], "origin": "operator"})
    env.cli.user = REVIEWER
    rev = user_ref_for(REVIEWER, USER_REF_KEY.encode("utf-8"))
    me = user_ref_for(ADMIN, USER_REF_KEY.encode("utf-8"))
    capsys.readouterr()  # drop what the seeding printed
    assert _main(["--json", "review-queue", "list"]) == 0
    listed = {row["item_id"]: row for row in json.loads(capsys.readouterr().out)["data"]["items"]}
    assert set(listed) == {kept_item, dropped_item, label, mapping, refused, weight}
    assert "content" not in listed[kept_item]["summary"]  # no memory text in the list
    assert _main(["memory", "approve", kept, "--note", "Looks right"]) == 0
    assert _main(["memory", "reject", dropped, "--note", "Duplicate glossary entry"]) == 0
    assert _main(["review-queue", "approve", label]) == 2  # DD-11: no answer option
    assert _main(["review-queue", "approve", mapping, "--note", "ok"]) == 0
    assert _main(["review-queue", "reject", refused, "--note", "Wrong team"]) == 0
    assert _main(["review-queue", "approve", weight]) == 11  # weight_change: admin (U09-38)
    env.cli.user = ADMIN
    assert _main(["review-queue", "approve", weight]) == 0
    assert env.calls == [
        ("approve", kept, rev, "Looks right"),
        ("reject", dropped, rev, "Duplicate glossary entry"),
    ]
    assert (row_of(kept)["status"], row_of(dropped)["status"]) == ("active", "rejected")
    decided = {kept_item: ("approved", rev), dropped_item: ("rejected", rev),
               mapping: ("approved", rev),
               refused: ("rejected", rev), weight: ("approved", me)}  # fmt: skip
    for item_id, (status, by) in decided.items():
        item = review_of(item_id)
        assert (item.status, item.decided_by) == (status, by), item_id
    lines = _audit(env.root, "review_decision")
    assert {(a["fields"]["item_id"], a["fields"]["status"]) for a in lines} == {
        (item_id, status) for item_id, (status, _) in decided.items()
    }
    assert review_of(label).status == "pending"


def test_cv_t09_24_label_check_reject_via_cli(env: SimpleNamespace) -> None:
    """CV-T09-24 `review-queue reject` of a label check with a note decides it (exit 0)."""
    label = _item("label_check", {"record_id": "servicenow:incident:INC0002",
                                  "question": "root_cause", "answer": "code"})  # fmt: skip
    env.cli.user = REVIEWER
    assert _main(["review-queue", "reject", label, "--note", "Wrong label"]) == 0
    item = review_of(label)
    assert item.status == "rejected"
    assert json.loads(item.note or "") == {"reason": "Wrong label"}


def test_it09_21_memory_item_id_and_review_id_are_not_interchangeable(
    env: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-21 `memory approve` takes the memory id only (R-33): a review item id is exit 2,
    an unknown memory id is not found; nothing is decided."""
    kept = seed_item()
    item_id = row_of(kept)["data"]["review_item_id"]
    env.cli.user = REVIEWER
    capsys.readouterr()  # drop what the seeding printed
    assert _main(["--json", "memory", "approve", item_id]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "UserInputError"
    assert _main(["memory", "approve", "mem_" + new_ulid()]) == 7
    assert review_of(item_id).status == "pending"
    assert row_of(kept)["status"] == "pending_approval"
