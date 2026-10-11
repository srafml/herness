"""Tests for herness._cli.cmd_queue: jobs, review-queue, memory (T09-24, U09-97).

Commands run through ``herness.cli.main`` as the OS user ``alice`` (``cli_env``). The owner
functions (impl 08 ``list_jobs``, impl 02 / impl 09 ops reads, ``herness.reports.actions``,
impl 07 ``MemoryStore``) are replaced on their modules, which the command bodies import lazily.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore

import herness.core.audit
import herness.core.jobs
import herness.eval.golden
import herness.reports.actions
import herness.store.ops
from herness import cli
from herness.core.jobs import JobRow
from herness.harness import memory

pytestmark = pytest.mark.unit

JOB = "job_01JA0000000000000000000001"
ITEM = "rev_01JA0000000000000000000001"
MEM = "mem_01JA0000000000000000000001"
AUTHOR = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - author ref, not a key
CREATED = datetime(2026, 10, 1, 12, tzinfo=UTC)
NEW_COMMANDS = (
    "jobs list", "jobs cancel", "jobs retry", "review-queue list", "review-queue approve",
    "review-queue reject", "memory list", "memory approve", "memory reject",
    "memory export-lora", "memory purge",
)  # fmt: skip


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


@pytest.fixture
def env(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> FakeMemoryStore:
    """``root`` admin, ``rev`` reviewer, ``alice`` viewer; the memory store is a recording fake."""
    cli_env.write_config(admins=["root"], reviewers=["rev"])
    store = FakeMemoryStore()
    monkeypatch.setattr(memory, "get_memory_store", lambda: store)
    return store


def _item(kind: str, payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(item_id=ITEM, kind=kind, status="pending", created_at=CREATED,
                           payload=payload)  # fmt: skip


def _decide_spy(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def decide(actor: Any, **kw: Any) -> str:
        calls.append({"actor": actor, **kw})
        return "approved" if kw["approve"] else "rejected"

    monkeypatch.setattr(herness.store.ops, "get_review_item", lambda item_id: item)
    monkeypatch.setattr(herness.reports.actions, "decide_review", decide)
    return calls


# --- UT09-89 -------------------------------------------------------------------------------------


def test_ut09_89_label_check_approve_is_usage_error(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-89 `review-queue approve` on a label_check: UserInputError exit 2 (DD-11)."""
    calls = _decide_spy(monkeypatch, _item("label_check", {"question": "root_cause"}))
    cli_env.user = "rev"
    assert _main(["--json", "review-queue", "approve", ITEM]) == 2
    error = _json(capsys)["error"]
    assert error["type"] == "UserInputError"
    assert error["message"] == "Approve label checks in the dashboard Review Queue."
    assert calls == []
    assert env.calls == []


def test_ut09_89_memory_approve_with_review_item_id_is_usage_error(
    cli_env: CliEnv, env: FakeMemoryStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-89 `memory approve rev_…` (a review item id, not a memory id): exit 2 (R-33)."""
    cli_env.user = "rev"
    assert _main(["--json", "memory", "approve", ITEM]) == 2
    assert _json(capsys)["error"]["type"] == "UserInputError"
    assert env.calls == []


def test_ut09_89_label_check_reject_reaches_decide_review(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-89 rejecting a label check needs no answer: decide_review with answer None."""
    calls = _decide_spy(monkeypatch, _item("label_check", {}))
    cli_env.user = "rev"
    assert _main(["--json", "review-queue", "reject", ITEM, "--note", "Wrong label"]) == 0
    ((call,),) = [calls]
    assert (call["item_id"], call["approve"], call["note"], call["answer"]) == (
        ITEM, False, "Wrong label", None)  # fmt: skip
    assert call["memory"] is None  # only a memory_write item builds the store
    assert call["actor"].role == "reviewer"
    assert _json(capsys)["data"]["status"] == "rejected"
    assert _main(["review-queue", "reject", ITEM]) == 2  # --note is required


def test_ut09_89_memory_write_item_gets_the_store(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-89 approving a memory_write review item passes the MemoryStore (R-33)."""
    calls = _decide_spy(monkeypatch, _item("memory_write", {"memory_id": MEM}))
    cli_env.user = "rev"
    assert _main(["review-queue", "approve", ITEM, "--note", "ok"]) == 0
    assert calls[0]["memory"] is env
    assert calls[0]["note"] == "ok"
    assert isinstance(calls[0]["now"], datetime)


def test_ut09_89_review_list_summarises_key_fields_without_text(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-89 `review-queue list`: status defaults to pending; one line per item, key fields
    per kind, never the memory content."""
    seen: list[dict[str, Any]] = []
    items = [
        _item("memory_write", {"memory_id": MEM, "layer": "semantic", "kind": "glossary",
                               "content": "SECRET-TEXT churn", "flags": []}),
        _item("weight_change", {"blocks": ["usd"], "origin": "operator", "changes": []}),
        _item("odd_kind", {"text": "x"}),
    ]  # fmt: skip

    def list_items(**kw: Any) -> list[SimpleNamespace]:
        seen.append(kw)
        return items

    monkeypatch.setattr(herness.store.ops, "list_review_items", list_items)
    cli_env.user = "rev"
    assert _main(["--json", "review-queue", "list", "--kind", "memory_write"]) == 0
    assert seen == [{"kind": "memory_write", "status": "pending"}]
    rows = _json(capsys)["data"]["items"]
    assert rows[0]["summary"] == {"memory_id": MEM, "layer": "semantic", "kind": "glossary",
                                  "flags": []}  # fmt: skip
    assert rows[1]["summary"] == {"blocks": ["usd"], "origin": "operator"}
    assert rows[2]["summary"] == {}
    assert _main(["review-queue", "list", "--status", "approved"]) == 0
    out = capsys.readouterr().out
    assert seen[-1] == {"kind": None, "status": "approved"}
    assert "SECRET-TEXT" not in out
    assert out.count(f"item_id={ITEM}") == 3


# --- jobs ----------------------------------------------------------------------------------------


def _row(**kw: Any) -> JobRow:
    values: dict[str, Any] = {
        "job_id": JOB,
        "kind": "chat",
        "gpu_class": "reasoning",
        "status": "queued",
        "priority": 50,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {"question": "private text"},
    }
    return JobRow(**(values | kw))


def test_cv_t09_24_jobs_list_calls_list_jobs(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 `jobs list` (viewer): list_jobs(status, kind, limit); payloads not shown."""
    seen: list[dict[str, Any]] = []

    def list_jobs(**kw: Any) -> list[JobRow]:
        seen.append(kw)
        return [_row()]

    monkeypatch.setattr(herness.core.jobs, "list_jobs", list_jobs)
    assert _main(["--json", "jobs", "list", "--status", "queued", "--kind", "chat"]) == 0
    assert seen == [{"status": "queued", "kind": "chat", "limit": 50}]
    (job,) = _json(capsys)["data"]["jobs"]
    assert job["job_id"] == JOB
    assert "payload" not in job
    assert _main(["jobs", "list", "--limit", "500"]) == 0
    assert seen[-1]["limit"] == 500
    assert "private text" not in capsys.readouterr().out
    assert _main(["jobs", "list", "--limit", "501"]) == 2
    assert _main(["jobs", "list", "--limit", "0"]) == 2


@pytest.mark.parametrize("op", ["cancel", "retry"])
def test_cv_t09_24_jobs_cancel_retry_call_job_control(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch, op: str,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 `jobs cancel|retry JOB_ID` (admin): job_control(actor, job_id, op)."""
    calls: list[tuple[Any, ...]] = []

    def job_control(actor: Any, *, job_id: str, op: str) -> str:
        calls.append((actor.role, job_id, op))
        return "canceled"

    monkeypatch.setattr(herness.reports.actions, "job_control", job_control)
    cli_env.user = "root"
    assert _main(["--json", "jobs", op, JOB]) == 0
    assert calls == [("admin", JOB, op)]
    assert _json(capsys)["data"]["status"] == "canceled"
    cli_env.user = "alice"
    assert _main(["jobs", op, JOB]) == 11  # viewer refused (U09-89)
    assert len(calls) == 1


# --- memory --------------------------------------------------------------------------------------


def test_cv_t09_24_memory_list_cuts_and_cleans_content(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 `memory list`: ui_list_memory_items(layer, status, limit); content cut to 120
    characters, escape sequences removed (safe_terminal_text)."""
    seen: list[dict[str, Any]] = []
    row = {"memory_id": MEM, "layer": "semantic", "kind": "glossary",
           "content": "\x1b[31mred\x1b[0m " + "x" * 200, "data": {"secret": 1},
           "provenance": {}, "confidence": 0.5, "status": "active", "created_at": "t",
           "expires_at": None, "last_used_at": None, "use_count": 0}  # fmt: skip

    def list_items(**kw: Any) -> list[dict[str, Any]]:
        seen.append(kw)
        return [row]

    monkeypatch.setattr(herness.store.ops, "ui_list_memory_items", list_items)
    assert _main(["--json", "memory", "list", "--layer", "semantic", "--limit", "5"]) == 0
    assert seen == [{"layer": "semantic", "status": None, "limit": 5}]
    (item,) = _json(capsys)["data"]["memory_items"]
    assert item["content"].startswith("red xx")
    assert len(item["content"]) == 120
    assert item["content"].endswith("…")
    assert "\x1b" not in item["content"]
    assert "data" not in item
    assert _main(["memory", "list"]) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_cv_t09_24_memory_decisions_call_decide_memory(
    cli_env: CliEnv, env: FakeMemoryStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-T09-24 `memory approve|reject MEMORY_ID` (reviewer): decide_memory → the store."""
    cli_env.user = "rev"
    assert _main(["--json", "memory", "approve", MEM, "--note", "fine"]) == 0
    assert _json(capsys)["data"] == {"schema": "cli/1", "memory_id": MEM, "status": "approved"}
    other = "mem_01JA0000000000000000000002"
    assert _main(["memory", "reject", other, "--note", "Not a glossary entry"]) == 0
    assert [(c[0], c[1][0]) for c in env.calls] == [("approve", MEM), ("reject", other)]
    assert env.calls[1][1][2] == "Not a glossary entry"
    assert _main(["memory", "reject", other]) == 2  # --note is required


def test_cv_t09_24_memory_export_lora_passes_golden_questions(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 `memory export-lora --out DIR` (admin): MemoryStore.export_lora with the golden
    suite's questions (DD29), read from the checkout root; the export report is printed."""
    paths: list[Path] = []
    calls: list[tuple[Path, list[str]]] = []
    suite = SimpleNamespace(questions=[SimpleNamespace(question="How many incidents?")])

    def load_suite(path: Path) -> SimpleNamespace:
        paths.append(path)
        return suite

    def export_lora(out: Path, *, golden_questions: list[str]) -> SimpleNamespace:
        calls.append((out, golden_questions))
        return SimpleNamespace(model_dump=lambda mode: {"export_id": "e1", "train_count": 4})

    monkeypatch.setattr(herness.eval.golden, "load_suite", load_suite)
    env.export_lora = export_lora  # type: ignore[attr-defined]
    cli_env.user = "root"
    out = tmp_path / "data" / "models" / "lora_data" / "x"
    assert _main(["--json", "memory", "export-lora", "--out", str(out)]) == 0
    assert calls == [(out, ["How many incidents?"])]
    assert paths == [tmp_path / "tests" / "eval" / "golden.yaml"]
    assert _json(capsys)["data"]["train_count"] == 4


def test_cv_t09_24_memory_purge_validates_audits_then_purges(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 `memory purge --author-ref HASH` (admin): bad hash exit 2; else the
    admin_action audit (target author_ref:<first 8>) and then MemoryStore.purge."""
    order: list[tuple[Any, ...]] = []
    monkeypatch.setattr(herness.core.audit, "audit",
                        lambda event, actor, **kw: order.append((event, kw)))  # fmt: skip

    def purge(*, author_ref: str) -> int:
        order.append(("purge", author_ref))
        return 3

    env.purge = purge  # type: ignore[attr-defined]
    cli_env.user = "root"
    for bad in ("ABC", AUTHOR.upper(), AUTHOR + "0"):
        assert _main(["memory", "purge", "--author-ref", bad]) == 2
    assert order == []
    assert _main(["--json", "memory", "purge", "--author-ref", AUTHOR]) == 0
    assert order == [("admin_action", {"action": "purge", "target": "author_ref:01234567"}),
                     ("purge", AUTHOR)]  # fmt: skip
    assert _json(capsys)["data"]["removed"] == 3
    cli_env.user = "rev"
    assert _main(["memory", "purge", "--author-ref", AUTHOR]) == 11


def test_cv_t09_24_memory_export_needs_eval_settings(
    cli_env: CliEnv, env: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """CV-T09-24 an absolute suite path is used as is; no eval settings → ConfigError (3)."""
    paths: list[Path] = []
    suite = tmp_path / "abs" / "suite.yaml"
    monkeypatch.setattr(
        herness.eval.golden,
        "load_suite",
        lambda path: paths.append(path) or SimpleNamespace(questions=[]),
    )
    env.export_lora = lambda out, golden_questions: SimpleNamespace(  # type: ignore[attr-defined]
        model_dump=lambda mode: {}
    )
    cli_env.user = "root"
    eval_yaml = cli_env.config_dir / "eval.yaml"
    text = eval_yaml.read_text(encoding="utf-8")
    eval_yaml.write_text(text.replace("tests/eval/golden.yaml", suite.as_posix()), "utf-8")
    assert _main(["memory", "export-lora", "--out", "x"]) == 0
    assert paths == [suite]
    eval_yaml.unlink()
    assert _main(["--json", "memory", "export-lora", "--out", "x"]) == 3
    assert _json(capsys)["error"]["type"] == "ConfigError"


@pytest.mark.parametrize("path", NEW_COMMANDS)
def test_cv_t09_24_queue_commands_have_help(
    cli_env: CliEnv, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-T09-24 every command of the card answers `--help` with exit 0."""
    del cli_env
    assert _main([*path.split(), "--help"]) == 0
    assert "Usage" in capsys.readouterr().out
