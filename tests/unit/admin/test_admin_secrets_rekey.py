"""Tests for ``herness secrets rekey`` (impl 10 U10-71; UT10-73; design 10 §5.3 step 1).

The new key reaches the operator only through ``show`` and is stored only after ``ESCROWED``;
``data``, warnings, audit lines and logs carry the job id and planned time, never the value.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.admin import commands_secrets
from herness.admin.commands_secrets import cmd_secrets_rekey
from herness.core import config as c
from herness.core.errors import StoreBusy

pytestmark = pytest.mark.unit

USER = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - audit actor id
SVC = "herness"
NEXT = "redact.hmac_key.next"
PLANNED = datetime.datetime(2026, 9, 26, 19, 0, tzinfo=datetime.UTC)


class Prompt:
    """Scripted ``prompt``: records each question, returns the queued answers in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        return self.answers.pop(0)


class Jobs:
    """Fake ``schedule_rekey`` plus the jobs backend lookup of the planned time."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.staged_seen: list[bool] = []

    def schedule_rekey(self) -> str:
        self.calls += 1
        self.staged_seen.append(commands_secrets.exists(NEXT))
        if self.error is not None:
            raise self.error
        return "job_rekey_1"

    def get_job(self, job_id: str) -> SimpleNamespace:
        assert job_id == "job_rekey_1"
        return SimpleNamespace(scheduled_for=PLANNED)


@pytest.fixture
def cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A cached config (keyring backend; audit lines under ``paths.logs``)."""
    del fake_keyring
    yield c.init_config(config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


def _install(monkeypatch: pytest.MonkeyPatch, jobs: Jobs) -> None:
    monkeypatch.setattr(commands_secrets, "schedule_rekey", jobs.schedule_rekey)
    monkeypatch.setattr(commands_secrets, "require_jobs_backend", lambda: jobs)


def _audit_fields(cfg: c.HernessConfig) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted(Path(cfg.paths.logs).glob("audit-*.jsonl")):
        lines += [json.loads(raw)["fields"] for raw in path.read_text("utf-8").splitlines()]
    return lines


def _audit_text(cfg: c.HernessConfig) -> str:
    return "".join(p.read_text("utf-8") for p in Path(cfg.paths.logs).glob("audit-*.jsonl"))


def test_ut10_73_rekey_already_staged_exits_1(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-73 ``.next`` present: exit 1 "a rekey is already staged"; nothing shown or planned."""
    fake_keyring.store[(SVC, NEXT)] = "cd" * 32
    jobs = Jobs()
    _install(monkeypatch, jobs)
    shown: list[str] = []
    prompt = Prompt()
    result = cmd_secrets_rekey(actor=USER, prompt=prompt, show=shown.append)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.warnings == ["a rekey is already staged"]
    assert (shown, prompt.questions, jobs.calls) == ([], [], 0)
    assert fake_keyring.store[(SVC, NEXT)] == "cd" * 32
    assert _audit_fields(cfg) == []


def test_ut10_73_rekey_stages_plans_and_audits(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-73 ``.next`` absent: shows the key, stages it after ESCROWED, calls the fake
    schedule_rekey and audits ``redact_rekey`` staged with the planned time."""
    jobs = Jobs()
    _install(monkeypatch, jobs)
    shown: list[str] = []
    prompt = Prompt("ESCROWED")
    with structlog.testing.capture_logs() as logs:
        result = cmd_secrets_rekey(actor=USER, prompt=prompt, show=shown.append)
    assert (result.ok, result.exit_code, result.warnings) == (True, 0, [])
    planned = "2026-09-26T19:00:00.000000Z"
    assert result.data == {"job_id": "job_rekey_1", "planned_at": planned}
    value = fake_keyring.store[(SVC, NEXT)]
    assert re.fullmatch(r"[0-9a-f]{64}", value)
    assert len(shown) == 1
    assert value in shown[0]
    assert NEXT in shown[0]
    assert prompt.questions == [f"Type ESCROWED after storing {NEXT} in the vault"]
    assert (jobs.calls, jobs.staged_seen) == (1, [True])  # staged before the job is planned
    fields = _audit_fields(cfg)
    assert {"action": "secret_set", "target": NEXT} in fields
    assert fields[-1] == {"action": "redact_rekey", "target": "staged", "detail": planned}
    assert value not in _audit_text(cfg)
    assert value not in repr(logs)
    assert value not in json.dumps(result.data) + json.dumps(result.warnings)


def test_ut10_73_rekey_not_escrowed_stores_nothing(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-73 an answer other than ESCROWED: exit 1, nothing stored, no job, no audit."""
    jobs = Jobs()
    _install(monkeypatch, jobs)
    result = cmd_secrets_rekey(actor=USER, prompt=Prompt("no"), show=lambda _t: None)
    assert (result.ok, result.exit_code) == (False, 1)
    assert result.warnings == [f"{NEXT} not created: escrow not confirmed"]
    assert (SVC, NEXT) not in fake_keyring.store
    assert jobs.calls == 0
    assert _audit_fields(cfg) == []


def test_ut10_73_rekey_planning_failure_unstages_the_key(
    cfg: c.HernessConfig, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-73 schedule_rekey raising StoreBusy propagates and leaves no staged key, so the
    command can be rerun; no ``redact_rekey`` audit line."""
    jobs = Jobs(StoreBusy("ops store busy"))
    _install(monkeypatch, jobs)
    with pytest.raises(StoreBusy):
        cmd_secrets_rekey(actor=USER, prompt=Prompt("ESCROWED"), show=lambda _t: None)
    assert (SVC, NEXT) not in fake_keyring.store
    assert all(f.get("action") != "redact_rekey" for f in _audit_fields(cfg))
