"""ST09-19 end to end (TH09-20): the pilot path never shows a secret value (T09-26).

The pilot runbook runs through ``herness.cli.main`` in this process with the ``local`` profile
on a temporary config and data directory, as the admin ``root`` in an elevated shell:
``init`` (config templates copied from a test template tree through the ``_init.TEMPLATE_DIR``
seam), ``secrets init`` and ``secrets set`` (hidden prompts answered by the test), ``config
show``, ``sync files`` (the real files connector and spec 01 sync handler on a dropped inbox),
``build``, ``review --kind funding``, ``report render``, ``jobs list``, ``doctor`` and ``chat
--new``. Every job runs ``--inline`` through impl 08's real ``run_inline``; the CLI registers
no handlers for ``--inline`` (T09-26 concern), so the test registers them as the worker
bootstrap would: the real connector handlers, and seams for ``build_pipeline`` (installs the
T11-17 ``tiny_build`` warehouse prepared by ``report_env`` as the new CURRENT build: a real
build needs the ``synth`` profile and the enrichment stubs) and ``review`` (seeds the run rows
the funding fixture draft cites, writes the draft and renders it, as spec 06's record step
does). Chat answers come from ``FakeChatService``. Sentinel values sit in the keyring for every
secret the config references; an audit-hook spy records every socket connect and name lookup.

Afterwards no sentinel is in any stdout or stderr (human and ``--json``), the rendered report
files, the ``config show`` output or any file under the log directory, and no connection or
off-host lookup was attempted.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_chat import FakeChatService, all_kinds
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_memory import FakeMemoryStore
from tests.support.report_env import BUILD_ID, FUND_RUN, fixture_draft, seed_ops
from tests.support.sync_env import FILES_SOURCES_YAML, drop_inbox

from herness import cli
from herness._cli import _chat_turn, _init, cmd_chat, identity
from herness.admin import doctor_host
from herness.connectors.files import FilesConnector
from herness.core import registry
from herness.core.jobs import inline as jobs_inline
from herness.core.jobs.handlers import register_handler
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome

pytestmark = pytest.mark.integration

ADMIN: Final = "root"
SENTINELS: Final = {
    "redact.hmac_key": "5e" * 32,
    "vllm.api_key": "St0919e2e-vllm-sentinel",  # pragma: allowlist secret - test sentinel
    "anthropic.api_key": "St0919e2e-anthropic-sentinel",  # pragma: allowlist secret - sentinel
    "openjev_api_key": "St0919e2e-openjev-sentinel",  # pragma: allowlist secret - sentinel
    "typesafe_api_key": "St0919e2e-typesafe-sentinel",  # pragma: allowlist secret - sentinel
}
TYPED: Final = "St0919e2e-typed-at-the-prompt"  # pragma: allowlist secret - test sentinel
QUESTION: Final = "How many incidents did team one have?"

# sys.addaudithook cannot be removed: one process-wide recorder, active only inside a test.
_SOCKETS: list[list[tuple[str, str]]] = []


def _record(event: str, args: tuple[object, ...]) -> None:
    if _SOCKETS and event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        _SOCKETS[-1].append((event, repr(args[:2])[:200]))


sys.addaudithook(_record)


@pytest.fixture
def sockets() -> Iterator[list[tuple[str, str]]]:
    """Socket connects and name lookups made while the test runs (audit hook)."""
    seen: list[tuple[str, str]] = []
    _SOCKETS.append(seen)
    try:
        yield seen
    finally:
        _SOCKETS.remove(seen)


def _templates(base: Path) -> Path:
    """The test template tree ``init`` copies: a loadable config with ``root`` as admin
    and the files source enabled (the runbook's operator edits, made up front)."""
    env = CliEnv(base)
    cfg = env.write_config(admins=[ADMIN])
    (cfg / "sources.yaml").write_text(FILES_SOURCES_YAML, encoding="utf-8")
    return cfg


class Pilot:
    """The pilot's CLI runs: every stdout and stderr, kept for the sentinel search."""

    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root, self.capsys = root, capsys
        self.outputs: list[str] = []
        self.envelopes: list[dict[str, Any]] = []

    @property
    def data(self) -> Path:
        return self.root / "data"

    def run(self, *args: str, json_mode: bool = True) -> tuple[int, dict[str, Any] | None]:
        self.capsys.readouterr()
        argv = ["--config-dir", str(self.root / "config"), "--profile", "local"]
        argv += ["--json", *args] if json_mode else list(args)
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        out, err = self.capsys.readouterr()
        self.outputs += [out, err]
        envelope = json.loads(out) if json_mode and out.strip() else None
        if envelope is not None:
            self.envelopes.append(envelope)
        code = exc.value.code
        assert isinstance(code, int)
        return code, envelope


def _build_seam(warehouse: Path, data: Path) -> None:
    """``build_pipeline``: install the prepared tiny build as the new CURRENT build."""
    from herness.store._warehouse_rw import write_current  # noqa: PLC0415 - after config load
    from herness.store.layout import DataLayout  # noqa: PLC0415

    def handle(ctx: JobContext) -> JobOutcome:
        del ctx
        dest = data / "warehouse" / f"wh-{BUILD_ID}.duckdb"
        shutil.copyfile(warehouse, dest)
        write_current(BUILD_ID, layout=DataLayout.from_root(data))
        return JobOutcome(status="done", result={"build_id": BUILD_ID, "promoted": True})

    register_handler("build_pipeline", handle)


def _review_seam(data: Path) -> None:
    """``review``: the run rows and draft of the funding fixture, then the report render."""

    def handle(ctx: JobContext) -> JobOutcome:
        from herness.reports.render import render_run  # noqa: PLC0415 - after config load

        assert ctx.job.payload["request"]["kind"] == "funding_review"
        seed_ops()
        draft = data / "reports" / FUND_RUN / "draft.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(json.dumps(fixture_draft("funding"), indent=2) + "\n", encoding="utf-8")
        render_run(FUND_RUN, ["html", "md"])
        return JobOutcome(status="done", result={"run_id": FUND_RUN, "partial": False})

    register_handler("review", handle)


@pytest.fixture
def pilot(
    cli_env: CliEnv, fake_keyring: MemoryKeyring, report_warehouse: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> Pilot:  # fmt: skip
    """Sentinel secrets in the keyring (no user-ref key yet: ``secrets init`` creates it),
    the template seam, an elevated admin shell, the prompts, the job seams and chat fakes."""
    for name, value in SENTINELS.items():
        fake_keyring.store[("herness", name)] = value
    del fake_keyring.store[("herness", "ui_user_ref_key")]
    monkeypatch.setattr(_init, "TEMPLATE_DIR", _templates(cli_env.root / "templates"))
    cli_env.user = ADMIN
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)

    def prompt(text: str, **kwargs: object) -> str:
        del kwargs
        return "ESCROWED" if "vault" in text else TYPED

    monkeypatch.setattr("typer.prompt", prompt)
    monkeypatch.setattr(jobs_inline, "_gpu_controller", lambda *args: None)  # no model server
    monkeypatch.setattr(doctor_host, "doctor_checks", lambda cfg: [])  # no host subprocesses
    registry.register("connector", "files")(FilesConnector)
    data = cli_env.root / "data"
    _build_seam(report_warehouse, data)
    _review_seam(data)
    chat = FakeChatService(write_rows=True)
    chat.scripts = [all_kinds()]
    monkeypatch.setattr(cmd_chat, "build_services", lambda cfg: (chat, FakeMemoryStore()))
    monkeypatch.setattr(_chat_turn, "policy", lambda now: ("live", None))
    return Pilot(cli_env.root, capsys)


def _files(folder: Path) -> list[str]:
    return [p.read_text("utf-8", errors="replace") for p in sorted(folder.rglob("*"))
            if p.is_file()]  # fmt: skip


def test_st09_19_pilot_path_end_to_end_no_sentinel_no_egress(
    pilot: Pilot, fake_keyring: MemoryKeyring, sockets: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """ST09-19 init -> secrets init/set -> config show -> sync -> build -> review -> report
    -> jobs list, doctor -> chat on the ``local`` profile with sentinel secrets: no sentinel
    in any output, JSON envelope, report file or log; zero outbound connections."""
    from herness.connectors import jobs as connector_jobs  # noqa: PLC0415 - after the guard

    code, init = pilot.run("init")
    assert code == 0, pilot.outputs[-1]
    assert init is not None
    assert init["data"]["migrations_applied"]
    assert "herness.yaml" in init["data"]["copied"]
    assert pilot.run("secrets", "init", json_mode=False)[0] == 0
    assert fake_keyring.store[("herness", "ui_user_ref_key")]  # created, escrow confirmed
    assert pilot.run("secrets", "set", "snow.token", json_mode=False)[0] == 0
    assert fake_keyring.store[("herness", "snow.token")] == TYPED
    assert pilot.run("config", "show", json_mode=False)[0] == 0
    code, shown = pilot.run("config", "show")
    assert code == 0
    assert "secret:anthropic.api_key" in json.dumps(shown)  # a reference, never a value
    connector_jobs.register_job_handlers()
    drop_inbox(pilot.data)
    code, synced = pilot.run("sync", "files", "--inline")
    assert code == 0, pilot.outputs[-1]
    assert synced is not None
    assert synced["data"]["status"] == "done"
    assert list((pilot.data / "raw" / "files" / "teams").rglob("*.parquet"))
    code, built = pilot.run("build", "--inline")
    assert (code, built["data"]["status"] if built else None) == (0, "done")
    assert (pilot.data / "warehouse" / "CURRENT").is_file()
    code, reviewed = pilot.run("review", "--kind", "funding", "--inline")
    assert code == 0, pilot.outputs[-1]
    assert reviewed is not None
    assert reviewed["data"]["jobs"][0]["run_id"] == FUND_RUN
    code, report = pilot.run("report", "render", FUND_RUN)
    assert (code, report["data"]["run_id"] if report else None) == (0, FUND_RUN)
    assert pilot.run("report", "render", FUND_RUN, json_mode=False)[0] == 0
    code, listed = pilot.run("jobs", "list")
    assert code == 0
    assert listed is not None
    assert {row["kind"] for row in listed["data"]["jobs"]} == {"sync", "build_pipeline", "review"}
    code, doctor = pilot.run("doctor")
    assert (code, doctor["ok"] if doctor else None) == (0, True), pilot.outputs[-1]
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{QUESTION}\n/quit\n"))
    assert pilot.run("chat", "--new", json_mode=False)[0] == 0
    corpus = "\n".join(
        pilot.outputs + _files(pilot.data / "reports" / FUND_RUN) + _files(pilot.data / "logs")
    )
    for sentinel in (*SENTINELS.values(), TYPED):
        assert sentinel not in corpus
    assert sockets == []  # zero outbound: no connect and no name lookup at all
    # Non-vacuous: the stages ran, wrote reports and logs, and audited the secret writes.
    assert (pilot.data / "reports" / FUND_RUN / "report.html").stat().st_size > 0
    assert len(pilot.envelopes) >= 8
    logs = "\n".join(_files(pilot.data / "logs"))
    assert '"secret_set"' in logs
    assert "cli.command.completed" in logs
    assert "Session ses_" in "\n".join(pilot.outputs)


@pytest.mark.xfail(
    strict=True,
    reason="product gap T09-29 (inline helper does not call worker_bootstrap; no handlers in a "
    "fresh process): `--inline` fails with ConfigError 'no handler for <kind>' (exit 3)",
)
def test_st09_19_inline_job_runs_without_test_registered_handlers(pilot: Pilot) -> None:
    """ST09-19 pilot stage ``sync files --inline`` as a fresh CLI process runs it: the CLI
    itself registers the owners' handlers (here the test registers none for ``sync``)."""
    assert pilot.run("init")[0] == 0
    drop_inbox(pilot.data)
    code, synced = pilot.run("sync", "files", "--inline")
    assert code == 0, synced
    assert synced is not None
    assert synced["data"]["status"] == "done"
    assert list((pilot.data / "raw" / "files" / "teams").rglob("*.parquet"))
