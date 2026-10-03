"""ST03-04 (TH03-03): no ticket text or email in logs, review items or other sinks.

A deep enrichment run on the planted small build (`_planted`, the `tiny_build` stand-in)
and a distillation round (T03-32 environment) run under the real logging pipeline
(`configure_logging` at INFO with the `scrub_secrets` file scrubber, configured after the
capture starts). The sinks scanned: stderr JSON lines, the daily log files, every review
item (`label_check` spot-checks and ensemble disagreements, `mapping_suggestion`, gold and
distill spot-check items) read through impl 02's `list_review_items` and the full `.dump` of
ops.sqlite (review items, evidence, events, jobs and their `last_error`), every parquet / JSON
file written under the data directory outside the raw lake and the warehouse (enrich cache,
labels, pair index, Laya candidate files) and the job result. The LLM echoes the ticket in
its invalid replies, so error paths are exercised too. Each sink has a planted-leak positive
control, so the scan is not vacuous; a known keyring secret is masked by the scrubber.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.integration.enrich._pipeline_env import PipelineEnv
from tests.integration.enrich.security._planted import (
    SENTINEL,
    _planted,
    fragments,
    pipeline_env,
    planted_env,
    reconfigure,
    scan,
)
from tests.support.build_harness import FakeJobContext
from tests.support.ops_store import OpsStoreHandle
from tests.support.secret_leak import ops_dump
from tests.unit.enrich._distill_support import FakeCtx, build_env
from tests.unit.enrich._fake_llm import Reply
from tests.unit.enrich._openjev_support import jev_env

from herness.core import secrets
from herness.core import time as clock
from herness.core.errors import FatalError
from herness.core.jobs import queue
from herness.core.jobs.handlers import run_handler
from herness.core.jobs.outcomes import finish_job
from herness.core.jobs.ports import JobsBackend, bind_jobs_backend
from herness.core.logging import configure_logging, get_logger, reset_logging
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome, LLMRequest
from herness.enrich.distill import run_distill
from herness.model.build import make_build_pipeline_handler
from herness.store.ops import ReviewItem, create_review_item, list_review_items
from herness.store.ops.jobs import SqliteJobsBackend

pytestmark = pytest.mark.integration

__all__ = ["_planted", "jev_env", "pipeline_env", "planted_env"]  # fixtures used by name

_PAYLOAD: Final[dict[str, Any]] = {"stages": ["build", "enrich"], "depth": "deep"}
_KINDS: Final = ("label_check", "mapping_suggestion")
_STATUSES: Final = ("pending", "approved", "rejected")
_TEXT_FILES: Final = frozenset({".json", ".jsonl", ".txt", ".md", ".yaml", ".csv"})
_SKIP_DIRS: Final = frozenset({"raw", "warehouse", "logs"})  # raw lake, DuckDB files, logs
_SECRET_NAME: Final = "zqx_scan_token"  # noqa: S105 - a secret name, not a value
_KNOWN: Final = "ZQXSENTINEL" + "KEY-4fT9wQ2zR7"  # built at run time (detect-secrets)
_OWNER: Final = "h:1:gpu"  # build_pipeline is a GPU-slot kind (R-43)
_CLASSES: Final = ("none", "reasoning", "decider", "large")
_NOW: Final = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_BLOCK_RE: Final = re.compile(r"<untrusted_data [^>]*>(.*?)</untrusted_data>", re.DOTALL)


# --- sink readers -----------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, default=lambda o: dict(o) if isinstance(o, Mapping) else str(o))


def _review_items() -> list[ReviewItem]:
    return [item for kind in _KINDS for status in _STATUSES
            for item in list_review_items(kind=kind, status=status, limit=5000)]  # fmt: skip


def _lake_files(root: Path) -> Iterator[tuple[str, str]]:
    """(relative path, text) of every parquet / text file outside raw lake, warehouse, logs."""
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if rel.parts[0] in _SKIP_DIRS:
            continue
        if path.suffix == ".parquet":
            rows = pq.read_table(path).to_pylist()
            yield rel.as_posix(), json.dumps(rows, default=str)
        elif path.suffix in _TEXT_FILES:
            yield rel.as_posix(), path.read_text("utf-8", errors="replace")


def _log_files(root: Path) -> str:
    return "\n".join(
        p.read_text("utf-8") for p in sorted((root / "logs").rglob("*")) if p.is_file()
    )


def _hits(text: str, texts: frozenset[str]) -> list[str]:
    """`scan` plus ticket text: the sentinel, whole stored texts and any cut fragment of one
    (a `WINDOW`-character shingle, so a truncated log field or message is caught too)."""
    return scan(text, extra=texts | {SENTINEL}) + fragments(text, texts)


def _leaks(sinks: dict[str, str], texts: frozenset[str]) -> dict[str, list[str]]:
    """Leaks per sink (see `_hits`)."""
    return {name: hits for name, text in sinks.items() if (hits := _hits(text, texts))}


def _fragment(texts: frozenset[str]) -> str:
    """60 characters of a stored ticket text without sentinel, email or placeholder: missed
    by whole-value matching, caught only as a fragment."""
    text = next(t for t in sorted(texts) if "Users report" in t)
    start = text.index("Users report")
    fragment = text[start : start + 60]
    assert SENTINEL not in fragment
    assert "[" not in fragment
    assert not scan(fragment, extra=texts | {SENTINEL})  # whole values alone miss it
    return fragment


def _uncontrolled(lines: Iterable[str]) -> str:
    """`lines` without the positive-control log lines."""
    return "\n".join(x for x in lines if ".leak.control" not in x and ".leak.fragment" not in x)


def _start_logging(root: Path) -> None:
    """INFO JSON lines to stderr and to `data/logs` through the known-secret scrubber."""
    configure_logging("INFO", log_dir=root / "logs", scrubber=secrets.scrub_secrets)


def _known_secret() -> str:
    """A keyring secret resolved by this process: a known value for `scrub_secrets`."""
    secrets.set_secret(_SECRET_NAME, _KNOWN, actor="system")
    value = secrets.resolve(f"secret:{_SECRET_NAME}").get_secret_value()
    assert value in secrets.known_values()
    return value


def _controls(err_lines: list[str], file_text: str, planted: str) -> None:
    """Positive controls: the planted line is in both log sinks, the known secret is not."""
    control = [line for line in err_lines if '"enrich.leak.control"' in line]
    assert len(control) == 1
    assert _leaks({"control": control[0]}, frozenset()) == {"control": [SENTINEL]}
    assert planted in file_text
    masked = [line for line in err_lines if '"enrich.leak.secret_control"' in line]
    assert len(masked) == 1
    assert _KNOWN not in masked[0]
    assert _KNOWN not in file_text


def _echo_script(env: PipelineEnv) -> None:
    """The LLM echoes the untrusted block back as invalid text on every third request."""
    vote = env.llm._script
    count = [0]

    def script(req: LLMRequest) -> Reply:
        count[0] += 1
        if count[0] % 3:
            return vote(req)
        parts = [p.text for m in req.messages for p in m.parts if p.type == "text"]
        blocks = [b for text in parts for b in _BLOCK_RE.findall(text)]
        return "I refuse. The ticket said: " + " | ".join(blocks)

    env.llm._script = script


# --- tests ------------------------------------------------------------------------------------


def test_st03_04_pipeline_logs_review_items_and_files_carry_no_ticket_text(
    planted_env: PipelineEnv, ops_store: OpsStoreHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST03-04 deep enrichment on the planted build: INFO logs (stderr and files), every
    review item, ops.sqlite, files under the data dir and the job result hold no ticket text
    and no email; planted leaks in each sink are found; the known secret is masked."""
    env = planted_env
    reconfigure(env, lambda d: d["mapping_suggest"].update(min_score=0.0))  # suggestions too
    _echo_script(env)
    known = _known_secret()
    capsys.readouterr()
    _start_logging(env.data_root)
    try:
        outcome, _ = env.job(_PAYLOAD)
        assert isinstance(outcome, JobOutcome), outcome
        build_id = str(outcome.result["build_id"])
        texts = frozenset(str(r[0]) for r in env.query(build_id, "SELECT text FROM "
                          "enrich.text_redacted WHERE length(text) >= 12"))  # fmt: skip
        log = get_logger("enrich.pipeline")
        planted = f"{SENTINEL} planted ticket text"
        log.info("enrich.leak.control", detail=planted)
        log.info("enrich.leak.secret_control", detail=f"token {known}")
        fragment = _fragment(texts)
        log.info("enrich.leak.fragment_control", detail=fragment)
    finally:
        err = capsys.readouterr().err
        reset_logging()
    lines = [line for line in err.splitlines() if line.startswith("{")]
    events = {json.loads(line)["event"] for line in lines}
    assert {"enrich.stage.started", "enrich.stage.completed"} <= events  # capture is live
    file_text = _log_files(env.data_root)
    _controls(lines, file_text, planted)
    cut = [line for line in lines if '"enrich.leak.fragment_control"' in line]
    assert len(cut) == 1
    assert fragments(cut[0], texts)  # a 60-character fragment is caught in both log sinks
    assert fragments(file_text, texts)
    items = _review_items()
    kinds = {(i.kind, str(i.payload.get("purpose", ""))) for i in items}
    assert ("label_check", "ensemble_disagreement") in kinds
    assert any(kind == "mapping_suggestion" for kind, _ in kinds)
    sinks = {
        "stderr": _uncontrolled(lines),
        "log files": _uncontrolled(file_text.splitlines()),
        "review items": _json([i.payload for i in items]),
        "ops.sqlite": ops_dump(ops_store.db_path),
        "job result": json.dumps(outcome.result, default=str),
        **dict(_lake_files(env.data_root)),
    }
    assert any(name.startswith("cache/decisions/") for name in sinks)  # cache was written
    assert _leaks(sinks, texts) == {}
    # positive controls for the store and file sinks
    create_review_item("label_check", {"purpose": "spot_check", "note": planted}, now=_NOW)
    assert _leaks({"ops": ops_dump(ops_store.db_path)}, texts)
    assert any(planted in _json(i.payload) for i in _review_items())
    control = env.data_root / "cache" / "leak-control.parquet"
    pq.write_table(pa.table({"text": [planted]}), control)
    assert [n for n, text in _lake_files(env.data_root) if planted in text] == [
        "cache/leak-control.parquet"
    ]


def _job_error(db_path: Path, job_id: str) -> tuple[str, dict[str, Any]]:
    """(status, parsed `last_error`) of one job row in ops.sqlite."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT status, last_error FROM job WHERE job_id = ?", [job_id])
        row = row.fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), dict(json.loads(row[1]))


def _failing_build(env: PipelineEnv, echo: str) -> str:
    """Run one `build_pipeline` job through the real queue whose encoder raises an error that
    echoes ticket text; `finish_job` stores its `last_error`. Returns the job id."""
    bind_jobs_backend(cast("JobsBackend", SqliteJobsBackend()))

    def boom() -> None:
        msg = f"tokenizer failed on input: {echo}"
        raise RuntimeError(msg)

    env.encoder.on_encode.append(boom)
    job_id = queue.enqueue("build_pipeline", _PAYLOAD, "none", max_attempts=1)
    row = queue.claim(owner=_OWNER, allowed_classes=_CLASSES, job_id=job_id)
    assert row is not None
    ctx = FakeJobContext(dict(row.payload), job_id=row.job_id)
    env.gpu.ctx = ctx
    outcome = run_handler(ctx, make_build_pipeline_handler(llm_factory=env.factory))
    assert not isinstance(outcome, JobOutcome)
    assert _leaks({"error": str(outcome)}, frozenset({echo})) == {}  # no text in the error
    finish_job(row, _OWNER, outcome, attempt_started_at=clock.now(), stop_reason=None)
    return job_id


def test_st03_04_failed_enrichment_job_last_error_carries_no_ticket_text(
    planted_env: PipelineEnv, ops_store: OpsStoreHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST03-04 an encoder error echoing a ticket fails the enrichment job: its `last_error`,
    events and the INFO logs hold no ticket text; a planted error message is found."""
    env = planted_env
    capsys.readouterr()
    _start_logging(env.data_root)
    echo = f"{SENTINEL}x00 Contact zqx.cx00@corp-mail.test"
    try:
        job_id = _failing_build(env, echo)
    finally:
        err = capsys.readouterr().err
        reset_logging()
    dump = ops_dump(ops_store.db_path)
    status, error = _job_error(ops_store.db_path, job_id)
    assert status == "failed"
    assert error["class"] == "FatalError"
    sinks = {"stderr": err, "log files": _log_files(env.data_root), "ops.sqlite": dump}
    assert _leaks(sinks, frozenset({echo})) == {}
    # positive control: a herness error whose message carries the sentinel is kept verbatim
    planted_id = queue.enqueue("build_pipeline", {"stages": ["build"]}, "none", max_attempts=1)
    row = queue.claim(owner=_OWNER, allowed_classes=_CLASSES, job_id=planted_id)
    assert row is not None
    finish_job(row, _OWNER, FatalError(f"planted {SENTINEL}"), attempt_started_at=clock.now(),
               stop_reason=None)  # fmt: skip
    assert SENTINEL in _job_error(ops_store.db_path, planted_id)[1]["message"]
    assert _leaks({"ops.sqlite": ops_dump(ops_store.db_path)}, frozenset())


def test_st03_04_distill_round_logs_and_review_items_carry_no_ticket_text(
    jev_env: ProcessState,
    ops_store: OpsStoreHandle,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ST03-04 a distillation round: INFO logs, spot-check and gold review items, labels and
    candidate files hold no ticket text; a planted log line and review item are found."""
    del jev_env
    env = build_env(ops_store.data_root, monkeypatch)
    texts = frozenset(str(r[0]) for r in env.wh.execute(
        "SELECT text FROM enrich.text_redacted").fetchall())  # fmt: skip
    assert texts
    capsys.readouterr()
    _start_logging(env.root)
    try:
        report = run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())
        planted = max(sorted(texts), key=len)  # long: its 40-char head is a fragment
        get_logger("enrich.distill").info("enrich.leak.control", detail=planted)
    finally:
        err = capsys.readouterr().err
        reset_logging()
    assert report.version is not None
    lines = [line for line in err.splitlines() if line.startswith("{")]
    assert any('"enrich.distill.step_completed"' in line for line in lines)
    control = [line for line in lines if '"enrich.leak.control"' in line]
    assert len(control) == 1
    assert planted in control[0]
    file_text = _log_files(env.root)
    assert planted in file_text  # the planted line reaches the log files too
    items = _review_items()
    assert {str(i.payload.get("purpose")) for i in items} >= {"spot_check"}
    sinks = {
        "stderr": _uncontrolled(lines),
        "log files": _uncontrolled(file_text.splitlines()),
        "review items": _json([i.payload for i in items]),
        "ops.sqlite": ops_dump(ops_store.db_path),
        "report": report.model_dump_json(),
        **dict(_lake_files(env.root)),
    }
    assert any("labels" in name for name in sinks)
    assert _leaks(sinks, texts) == {}
    create_review_item("label_check", {"purpose": "gold", "note": planted}, now=_NOW)
    assert _leaks({"ops": ops_dump(ops_store.db_path)}, texts)
    labels = next(p for p in sorted(env.root.rglob("*.parquet")) if "labels" in p.parts)
    pq.write_table(pa.table({"note": [planted[:40]]}), labels.parent / "leak-control.parquet")
    hit = [n for n, text in _lake_files(env.root) if _hits(text, texts)]
    assert hit == [(labels.parent / "leak-control.parquet").relative_to(env.root).as_posix()]
