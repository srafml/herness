"""Integration tests of the T09-22 system commands through ``herness.cli.main`` (impl 09
U09-93): ``status`` on the fixture ops store and warehouse, ``doctor``'s CURRENT reader on a
real build file, and the in-process stand-in for the manual ``herness worker --once`` check.

``status`` reads the real ``report_env`` stores (migrated ops store, seeded runs, the
``tiny_build`` warehouse copy); its CURRENT pointer is written by the test. The worker test
runs the real impl 08 ``run_worker`` with ``--once`` on an empty queue in this process: the
real ``herness.cli:worker_bootstrap`` composition root runs, nothing is claimed, no child
process starts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_keyring import MemoryKeyring
from tests.support.report_env import BUILD_ID, FUND_RUN, ReportEnv

from herness import cli
from herness._cli import _status
from herness.core import time as clock

pytestmark = pytest.mark.integration


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


@pytest.fixture
def viewer(cli_env: CliEnv, report_env: ReportEnv) -> ReportEnv:
    """``alice`` is a viewer of the seeded report environment."""
    cli_env.write_config(admins=["root"])
    return report_env


def test_it09_25_status_without_current(
    viewer: ReportEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-25 `herness status` with no CURRENT: "No promoted warehouse yet." and every
    other section from the fixture ops store. The `local` profile cannot build its model
    clients with egress off (T07-25), so the jobs and chat sections are null with a warning."""
    assert _main(["status"]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "Warning: chat mode and job status unavailable (ConfigError)" in captured.err
    assert "No promoted warehouse yet." in out
    for section in ("last_build_job:", "dq_warnings:", "jobs:", "runs:", "chat_mode:"):
        assert section in out
    assert FUND_RUN in out


def test_it09_25_status_with_current_build(
    viewer: ReportEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """IT09-25 `herness --json status` with CURRENT on the fixture build: build id and age
    from `meta.build`, failed DQ rows, the last runs and the jobs snapshot (hybrid profile:
    its model clients build, so the chat policy has its chain registry)."""
    (viewer.data_root / "warehouse" / "CURRENT").write_text(BUILD_ID, encoding="ascii")
    assert _main(["--profile", "hybrid", "--json", "status"]) == 0
    data = _json(capsys)["data"]
    assert data["current_build"]["build_id"] == BUILD_ID
    assert data["current_build"]["age_h"] is None or data["current_build"]["age_h"] >= 0
    assert all("check_name" in row for row in data["dq_warnings"])
    assert FUND_RUN in {run["run_id"] for run in data["runs"]}
    assert len(data["runs"]) <= 5
    assert set(data["jobs"]) == {"workers", "running", "queue", "planned_rekey", "breakers",
                                 "faults_enabled"}  # fmt: skip
    assert data["chat_mode"] in {"live", "small_model", "defer", "cloud"}


def test_it09_25_current_build_reader_on_real_build(viewer: ReportEnv) -> None:
    """IT09-25 the CURRENT reader opens the build read-only: id, age from `finished_at`,
    failed DQ checks; None without CURRENT."""
    assert _status.current_build(clock.now()) is None
    (viewer.data_root / "warehouse" / "CURRENT").write_text(BUILD_ID, encoding="ascii")
    view = _status.current_build(clock.now())
    assert view is not None
    assert view.build_id == BUILD_ID
    assert view.age_h is None or view.age_h >= 0
    failures = _status.dq_failures(BUILD_ID)
    assert isinstance(failures, list)
    assert all(set(row) == {"check_name", "severity", "value", "threshold"} for row in failures)


def test_it09_25_doctor_on_fixture_stores(
    viewer: ReportEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-25 `herness --json doctor` on the fixture stores validates as `cli/1`; the owned
    rows come first (host rows faked: no subprocess in this test)."""
    from herness.admin import doctor_host  # noqa: PLC0415 - patched where used

    del viewer
    monkeypatch.setattr(doctor_host, "doctor_checks", lambda cfg: [])
    code = _main(["--json", "doctor"])
    envelope = _json(capsys)
    assert envelope["data"]["schema"] == "cli/1"
    assert envelope["command"] == "doctor"
    rows = envelope["data"]["checks"]
    assert [r["name"] for r in rows[:3]] == ["config", "current_build", "ops_migrations"]
    assert rows[2]["status"] == "PASS"  # report_env migrated the ops store
    assert code == (1 if any(r["status"] == "FAIL" for r in rows) else 0)
    assert envelope["ok"] is (code == 0)


def test_cv_t09_22_worker_once_in_process(
    cli_env: CliEnv,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CV T09-22 stand-in for the manual `herness worker --once` acceptance: the real
    `run_worker` with the real `herness.cli:worker_bootstrap` (hybrid profile) on an empty
    queue logs `jobs.worker.nothing_to_do` and exits 0; no child process starts."""
    from herness.core.jobs import supervisor  # noqa: PLC0415 - patched where used
    from herness.store import ops  # noqa: PLC0415

    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    cli_env.write_config(admins=["alice"])
    data = cli_env.root / "data"
    for name in ("HERNESS_CONFIG_DIR", "HERNESS_DATA_DIR", "HERNESS_PROFILE",
                 "HERNESS_SET_OVERRIDES"):  # fmt: skip
        monkeypatch.setenv(name, "")  # recorded, so the os.environ writes are undone
        monkeypatch.delenv(name)
    monkeypatch.setattr(supervisor, "run_scheduler", lambda now: None)  # no scheduled jobs
    monkeypatch.setattr(supervisor, "run_due_probes", lambda now: None)  # no model probes
    args = ["--config-dir", str(cli_env.config_dir), "--data-dir", str(data),
            "--profile", "hybrid"]  # fmt: skip
    assert _main([*args, "init"]) == 0
    assert ops.pending_migrations() == []
    with caplog.at_level(logging.INFO):
        code = _main([*args, "worker", "--once", "--gpu-class", "none"])
    assert code == 0
    events = " ".join(record.getMessage() for record in caplog.records)
    assert "jobs.worker.nothing_to_do" in events
    assert "jobs.worker.config_invalid" not in events
    assert Path(os.environ["HERNESS_CONFIG_DIR"]) == cli_env.config_dir.resolve()
