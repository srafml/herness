"""ST09-32 (TH09-17), ST09-33 (TH09-19), ST09-34 (TH09-24): report and decision integrity
through the real CLI (T09-26).

Each test runs ``herness.cli.main`` on ``cli_env`` (config with roles, OS user, keyring) over
``report_env`` (migrated ops store with the fixture runs, recommendations and evidence, the
``tiny_build`` warehouse copy and both fixture drafts). ST09-32 cites a query id that does not
exist and a recommendation of another run; ST09-33 records a decision and then has the audit
writer fail; ST09-34 makes the replace of the second report file fail during a re-render.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore
from tests.support.report_env import FUND_RUN, ORG_RUN, REC1, REC3, ReportEnv, fixture_draft

from herness import cli
from herness.core.audit import verify_chain
from herness.core.errors import FatalError
from herness.harness import memory
from herness.reports import actions

pytestmark = pytest.mark.integration

REASON: Final = "Approved in the funding review meeting."


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


@pytest.fixture
def env(cli_env: CliEnv, report_env: ReportEnv) -> ReportEnv:
    """Admin ``root``, reviewer ``rev``, viewer ``alice`` over the seeded report stores."""
    cli_env.write_config(admins=["root"], reviewers=["rev"])
    return report_env


def _error(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    envelope: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    error: dict[str, Any] = envelope["error"]
    return error


# --- ST09-32 ------------------------------------------------------------------------------------


def _unknown_query() -> dict[str, Any]:
    draft = fixture_draft("funding")
    draft["recommendations"][1]["numbers"][0]["query_id"] = "q_ffffffffffffffff"
    return draft


def _foreign_rec() -> dict[str, Any]:
    draft = fixture_draft("funding")
    draft["recommendations"][0]["rec_id"] = REC3  # a recommendation of ORG_RUN
    return draft


@pytest.mark.parametrize(
    ("make", "where"),
    [(_unknown_query, "unknown query_id q_ffffffffffffffff"),
     (_foreign_rec, "recommendations[0].rec_id: unknown rec_id")],
    ids=["unknown-query", "foreign-recommendation"],
)  # fmt: skip
def test_st09_32_forged_evidence_references_refused(
    env: ReportEnv, capsys: pytest.CaptureFixture[str],
    make: Callable[[], dict[str, Any]], where: str,
) -> None:  # fmt: skip
    """ST09-32 a draft citing a query id with no evidence row, or another run's
    recommendation: ``report render`` exits 12 (``ReportContractError`` naming the field);
    no report file and no manifest is written."""
    assert ORG_RUN != FUND_RUN
    env.write_draft(make())
    capsys.readouterr()
    assert _main(["--json", "report", "render", FUND_RUN]) == 12
    error = _error(capsys)
    assert (error["type"], error["exit_code"]) == ("ReportContractError", 12)
    assert where in error["details"]["where"]
    folder = env.reports_root / FUND_RUN
    assert sorted(p.name for p in folder.iterdir()) == ["draft.json"]


# --- ST09-33 ------------------------------------------------------------------------------------


def test_st09_33_decision_audited_with_user_ref_and_blocked_without_audit(
    env: ReportEnv, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """ST09-33 a reviewer's ``decide``: one hash-chained ``recommendation_decision`` audit
    line whose actor and ``decided_by`` are the reviewer's ``user_ref`` (never the OS name);
    with the audit writer failing the decision is refused and nothing is recorded."""
    store = FakeMemoryStore()
    monkeypatch.setattr(memory, "get_memory_store", lambda: store)
    cli_env.user = "rev"
    capsys.readouterr()
    assert _main(["--json", "decide", REC1, "accepted", "--reason", REASON]) == 0
    ((name, args, _),) = store.calls
    user_ref = args[3]
    assert (name, len(user_ref)) == ("decide", 32)
    logs = env.data_root / "logs"
    lines = [json.loads(raw) for p in sorted(logs.glob("audit-*.jsonl"))
             for raw in p.read_text("utf-8").splitlines()]  # fmt: skip
    (line,) = [x for x in lines if x["event"] == "recommendation_decision"]
    assert line["actor"] == line["fields"]["decided_by"] == user_ref != "rev"
    assert "rev" not in json.dumps(line["fields"])
    chain = verify_chain(logs)
    assert (chain.ok, chain.first_break) == (True, None)

    def failing_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "audit log unavailable"
        raise FatalError(msg)

    monkeypatch.setattr(actions, "audit", failing_audit)
    capsys.readouterr()
    assert _main(["--json", "decide", REC1, "rejected", "--reason", REASON]) == 1
    assert _error(capsys)["type"] == "FatalError"
    assert len(store.calls) == 1  # the refused decision never reached the memory store


# --- ST09-34 ------------------------------------------------------------------------------------


def test_st09_34_failed_rerender_leaves_no_torn_file_and_old_manifest(
    env: ReportEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST09-34 a re-render whose replace of ``report.md`` fails (after ``report.html`` was
    replaced): exit 5 (``SchemaViolation`` naming the file); no tmp file left; every report
    file is a complete render (the old or the new one); ``manifest.json`` is still the one of
    the last complete render, written last."""
    folder = env.reports_root / FUND_RUN
    assert _main(["report", "render", FUND_RUN]) == 0
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    draft = copy.deepcopy(fixture_draft("funding"))
    draft["title"] = "Funding review (revised draft)"
    env.write_draft(draft)
    real = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst).name == "report.md":
            msg = "disk full"
            raise OSError(28, msg)
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    capsys.readouterr()
    assert _main(["--json", "report", "render", FUND_RUN]) == 5
    error = _error(capsys)
    assert (error["type"], error["details"]) == ("SchemaViolation", {"file": "report.md"})
    monkeypatch.setattr(os, "replace", real)
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["draft.json", "manifest.json", "report.html", "report.md"]
    assert folder.joinpath("manifest.json").read_bytes() == before["manifest.json"]
    assert folder.joinpath("report.md").read_bytes() == before["report.md"]
    html = folder.joinpath("report.html").read_bytes()
    assert html != before["report.html"]  # replaced whole, before the failure
    assert html.rstrip().endswith(b"</html>")
    assert b"Funding review (revised draft)" in html
