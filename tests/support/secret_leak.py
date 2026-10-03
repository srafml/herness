"""The sentinel secret-leak run of impl 10 IT10-10 and ST10-14 (T10-21, TH10-07).

``run_sentinel_pipeline`` plants three sentinel values and one blocked host, drives the
fake pipeline and returns what the grep needs:

* ``SENTINEL_KEY`` is stored in the keyring (``set_secret``) under the name the connector
  config references (``sources.jira.auth.credentials``) and resolved at the point of use, so
  it is a known value (U10-32); ``SENTINEL_CRED`` (``password=``) and ``SENTINEL_URL``
  (``?token=``) are CREDENTIAL / URL_TOKEN shaped. All three sit in the text of a record
  the files connector syncs (the impl 11 fixture connector has no symbol on the tree:
  controller ruling, the files connector over a tmp inbox stands in for it).
* ``BLOCKED_HOST`` is the ``base_url``/``hosts`` of a disabled connector in ``sources.yaml``.

The run: config load, file logging (``data/logs``, ``scrub_secrets``), ``record_config_change``
(``data/config_snapshots``), socket guard, a files ``sync`` job, a connector-side auth failure
that echoes the resolved secret into a log line and a trace field, one chat turn (a tool turn
then a text turn; no ``ChatService`` exists on the tree, so the turn is driven through the
registry's ``llm_client:fake`` (``FakeLLMClient``) and again through ``OpenAICompatClient`` on
a ``ScriptRouter``, with the record text passed through ``redact_text`` as the warehouse
tools' redact-on-read does, TH05-05), a ``Tracer`` writing ``data/traces``, and one
``review_item`` created and approved. No report renderer exists on the tree (spec note).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import socket
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import pyarrow.parquet as pq
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_llm import FakeLLMClient, respx_router
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import SETTLED_S, init_sync_config

from herness.connectors.files import FilesConnector
from herness.connectors.jobs import handle_sync
from herness.core import egress_socket as es
from herness.core import redact as r
from herness.core import registry, secrets
from herness.core import time as clock
from herness.core.audit import record_config_change
from herness.core.errors import EgressBlocked, FatalError
from herness.core.ids import new_ulid
from herness.core.jobs import queue
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.jobs.outcomes import finish_job
from herness.core.jobs.ports import JobsBackend, bind_jobs_backend
from herness.core.logging import configure_logging, get_logger, reset_logging
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.resilience.events import record_event
from herness.core.settings import RedactionConfig
from herness.core.types import (
    JobOutcome,
    LLMRequest,
    LLMResponse,
    Message,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
)
from herness.eval.scripted import LLMScript, ScriptBook
from herness.harness.llm.openai_compat import OpenAICompatClient
from herness.harness.llm.settings import ClientConfig, TraceSettings
from herness.harness.tracing import Tracer, llm_call_fields, tool_call_fields
from herness.store.ops import create_review_item, decide_review_item, list_review_items
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

__all__ = ["BLOCKED_HOST", "JOB_ERROR", "SECRET_NAME", "SENTINELS", "SENTINEL_CRED"]
__all__ += ["SENTINEL_KEY", "SENTINEL_URL", "SOURCES_YAML", "TICKET_TEXT", "USER_REF"]
__all__ += ["LeakRun", "RecordingFakeLLM", "artefacts", "fail_job", "known_secret_job_dump"]
__all__ += ["leak_run", "ops_dump", "run_sentinel_pipeline"]

SENTINEL_KEY: Final = "synthetic-sentinel-keyring-7Qv2Lm9Xw4Pz"
SENTINEL_CRED: Final = "synthetic-sentinel-cred-3Hd8Rk1Nc6Ua"
SENTINEL_URL: Final = "synthetic-sentinel-urltok-5Tb4Wy7Je2Ko"
SENTINELS: Final = (SENTINEL_KEY, SENTINEL_CRED, SENTINEL_URL)
BLOCKED_HOST: Final = "tracker.synthetic-blocked.invalid"
SECRET_NAME: Final = "jira_api_token"  # noqa: S105 - a secret name, not a value
USER_REF: Final = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret - a user_ref
_NOW: Final = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)
_OWNER: Final = "h:1:cpu0"
_INBOX_MTIME: Final = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
_OPENAI_BASE: Final = "http://127.0.0.1:8000/v1"
_ANSWER: Final = "Ticket TCK-1 reports a printer outage; the pasted credentials were redacted."
_ZERO: Final = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}

SOURCES_YAML: Final = f"""\
version: 1
sources:
  files:
    enabled: true
    inbox: data/inbox
    entities:
      tickets: {{pattern: "*.csv", key_field: [ticket_id]}}
  jira:
    enabled: false
    flavor: cloud
    base_url: https://{BLOCKED_HOST}
    hosts: [{BLOCKED_HOST}]
    auth: {{method: api_token, credentials: "secret:{SECRET_NAME}"}}
"""

TICKET_TEXT: Final = (
    f"Printer outage. Admin pasted token={SENTINEL_KEY} and password={SENTINEL_CRED} "
    f"see https://wiki.synthetic-corp.test/kb?token={SENTINEL_URL} for the old runbook"
)
TICKETS_CSV: Final = f'ticket_id,category,description\nTCK-1,printer,"{TICKET_TEXT}"\n'

_SCRIPT: Final[dict[str, Any]] = {
    "match": {"dedup_key": "*"},
    "turns": [
        {"tool_calls": [{"name": "get_record", "arguments": {"key": "TCK-1"}}]},
        {"final": {"text": _ANSWER}},
    ],
    "source": "t10_21#0",
}


class RecordingFakeLLM(FakeLLMClient):
    """``FakeLLMClient`` that keeps every request it is asked to complete."""

    def __init__(self, scripts: Path | ScriptBook) -> None:
        super().__init__(scripts)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Record the request, then serve the scripted turn."""
        self.requests.append(req)
        return super().complete(req)


@dataclass
class LeakRun:
    """What the sentinel run left behind for the grep (IT10-10 / ST10-14)."""

    data_root: Path
    db_path: Path
    synced_rows: int
    lake_text: str
    answers: list[str]
    fake_requests: list[LLMRequest]
    wire_bodies: list[str]
    review_payloads: list[str]
    blocked: EgressBlocked | None
    connects: list[object] = field(default_factory=list)
    resolved_blocked: bool = False
    unguarded_error: str | None = None


def _book() -> ScriptBook:
    return ScriptBook([LLMScript.model_validate(_SCRIPT)])


def _openai_cfg() -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "name": "local-30b", "kind": "openai_compat", "base_url": _OPENAI_BASE,
            "model": "qwen3-30b", "context_window": 32768, "max_output_tokens": 4096,
            "tokenizer": "estimate", "max_concurrency": 4, "price_per_mtok": _ZERO,
            "server": "vllm",
        }
    )  # fmt: skip


def _drop_ticket(data_root: Path) -> None:
    path = data_root / "inbox" / "tickets" / "tickets.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TICKETS_CSV, encoding="utf-8", newline="")
    past = _INBOX_MTIME.timestamp() - SETTLED_S  # fixed and long settled: no wall clock
    os.utime(path, (past, past))


def _lake_text(data_root: Path) -> str:
    rows = [
        row
        for path in sorted((data_root / "raw" / "files" / "tickets").rglob("*.parquet"))
        for row in pq.read_table(path).to_pylist()
    ]
    return json.dumps(rows, default=str, ensure_ascii=False)


def _record_text(lake_text: str) -> str:
    """The synced record's description, read back from the raw lake (the tool's source)."""
    for row in json.loads(lake_text):
        payload = row.get("payload")
        data = json.loads(payload) if isinstance(payload, str) else row
        if isinstance(data, dict) and "description" in data:
            return str(data["description"])
    msg = "synced ticket not found in the lake"
    raise AssertionError(msg)


def _meta(run_id: str, step: int) -> RequestMeta:
    return RequestMeta(
        run_id=run_id,
        task_id=None,
        role="analyst",
        model_role="writer",
        step=step,
        request_key=f"{run_id}:{step}:call",
    )


def _request(run_id: str, step: int, *extra: Message) -> LLMRequest:
    question = Message(role="user", parts=[TextPart(text="What does ticket TCK-1 say?")])
    return LLMRequest(
        client="local-30b",
        system=[SystemBlock(text="You answer questions about service tickets.")],
        messages=[question, *extra],
        max_output_tokens=512,
        timeout_s=30,
        metadata=_meta(run_id, step),
    )


def _chat_turn(
    complete: Callable[[LLMRequest], LLMResponse], record_text: str, tracer: Tracer, run_id: str
) -> str:
    """One chat turn: tool turn (``get_record``), redact-on-read tool result, text turn."""
    first_req = _request(run_id, 0)
    first = complete(first_req)
    fields, payload = llm_call_fields(first_req, first, prompt_hash="ph0", gate_wait_ms=0)
    tracer.emit("llm_call", step=0, payload=payload, **fields)
    (call,) = first.tool_calls
    content = r.redact_text(record_text)  # warehouse redact-on-read before the model (TH05-05)
    assert content is not None
    result = ToolResult(tool_call_id=call.id, name=call.name, ok=True, content=content)
    tfields, tpayload = tool_call_fields(call, result)
    tracer.emit("tool_call", step=0, payload=tpayload, **tfields)
    tool_msgs = (
        Message(role="assistant", parts=[ToolCallPart(call=call)]),
        Message(role="tool", parts=[ToolResultPart(tool_call_id=call.id, content=content)]),
    )
    second_req = _request(run_id, 1, *tool_msgs)
    second = complete(second_req)
    fields, payload = llm_call_fields(second_req, second, prompt_hash="ph1", gate_wait_ms=0)
    tracer.emit("llm_call", step=1, payload=payload, **fields)
    return second.text


def _connector_auth_failure(tracer: Tracer) -> None:
    """A connector-side failure whose message echoes the resolved secret: log, trace, and a
    ``resilience_event`` row in ops.sqlite through the production ``record_event``."""
    token = secrets.resolve(f"secret:{SECRET_NAME}").get_secret_value()  # point of use
    log = get_logger("connectors.files")
    try:
        msg = f"upstream rejected credential {token}"
        raise PermissionError(msg)  # noqa: TRY301 - the planted failure is raised here
    except PermissionError as exc:
        log.exception("connectors.auth.failed", source="jira", detail=str(exc))
        tracer.emit("retry", attempt=1, error_type=type(exc).__name__, error=str(exc))
        detail = {"key": "source:jira", "failures": 1, "trips": 1, "reason": str(exc)}
        record_event("breaker_open", component="resilience", target="jira", detail=detail)


def _failing_handler(message: str) -> Callable[[object], JobOutcome]:
    def handler(_ctx: object) -> JobOutcome:
        raise FatalError(message)

    return handler


def fail_job(message: str) -> str:
    """Run one ``reconcile`` job through the real queue whose handler fails with ``message``;
    ``finish_job`` stores its ``last_error`` in ops.sqlite. Returns the job id."""
    register_handler("reconcile", _failing_handler(message))
    job_id = queue.enqueue("reconcile", {"source": "jira"}, "none")
    row = queue.claim(owner=_OWNER, allowed_classes=("none",))
    assert row is not None
    assert row.job_id == job_id
    ctx = FakeJobContext(dict(row.payload), kind="reconcile", job_id=row.job_id)
    outcome = run_handler(ctx, resolve_handler("reconcile"))
    finish_job(row, _OWNER, outcome, attempt_started_at=clock.now(), stop_reason=None)
    return job_id


# Only CREDENTIAL / URL_TOKEN shapes: the bare known value case (scrubbed since T08-15b) is
# checked on its own by ST10-14's known-secret job test.
JOB_ERROR: Final = (
    f"reconcile failed: password={SENTINEL_CRED} "
    f"see https://wiki.synthetic-corp.test/kb?token={SENTINEL_URL}"
)


def _try_blocked_host(monkeypatch: pytest.MonkeyPatch, run: LeakRun) -> None:
    """The connector's blocked host: the socket guard refuses it before any connect."""
    real_connect = socket.socket.connect

    def recording_connect(self: socket.socket, address: Any) -> None:
        run.connects.append(address)
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", recording_connect)
    try:
        socket.create_connection((BLOCKED_HOST, 443), timeout=1)
    except EgressBlocked as exc:
        run.blocked = exc
    except OSError as exc:  # no guard: resolution or connect failed on its own
        run.unguarded_error = type(exc).__name__
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect)
    run.resolved_blocked = es._fresh(BLOCKED_HOST)


def _review_item(record_key: str) -> None:
    """Structural only: production review payloads carry ids, scores and counts, never ticket
    text (TH03-03, enrich.mapping_suggest._payload), so no sentinel has a path here."""
    payload = {"source": "files", "entity": "tickets", "key": record_key, "field": "category"}
    item_id = create_review_item("mapping_suggestion", payload | {"value": "printer"}, now=_NOW)
    decide_review_item(item_id, "approved", decided_by=USER_REF, now=_NOW)


_OFFLINE_VARS: Final = ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK")
_TRACE_ALL: Final = TraceSettings(payload_sample_rate={"eval": 1.0, "chat": 1.0, "review": 1.0})


def _bind_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Files connector and recording fake registered, fixed-key redactor, ops backend bound."""
    registry.register("connector", "files")(FilesConnector)
    registry.register("llm_client", "fake")(RecordingFakeLLM)
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    bind_ops_backend(SqliteResilienceBackend())
    bind_jobs_backend(cast("JobsBackend", SqliteJobsBackend()))
    for var in _OFFLINE_VARS:  # install_socket_guard sets them; monkeypatch restores them
        monkeypatch.setenv(var, "1")


def _run(run: LeakRun, tracer: Tracer, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = handle_sync(FakeJobContext({"source": "files"}, kind="sync"))
    results = outcome.result["results"]
    assert isinstance(results, list)
    counts = [res["rows"] for res in results if isinstance(res, dict)]
    run.synced_rows = sum(n for n in counts if isinstance(n, int))
    run.lake_text = _lake_text(run.data_root)
    _connector_auth_failure(tracer)
    fail_job(JOB_ERROR)
    record = _record_text(run.lake_text)
    fake = registry.get("llm_client", "fake")(_book())  # the registry's model client
    run.answers.append(_chat_turn(fake.complete, record, tracer, tracer.run_id))
    run.fake_requests.extend(fake.requests)
    router = respx_router(_book(), openai_base_url=_OPENAI_BASE).install(monkeypatch)
    adapter = OpenAICompatClient(_openai_cfg())
    run.answers.append(_chat_turn(adapter.complete, record, tracer, tracer.run_id))
    run.wire_bodies.extend(req.content.decode("utf-8") for req in router.requests)
    _try_blocked_host(monkeypatch, run)
    _review_item("TCK-1")
    run.review_payloads.extend(_review_json(item) for item in list_review_items())


def _json_default(value: object) -> object:
    return dict(value) if isinstance(value, Mapping) else str(value)


def _review_json(item: object) -> str:
    """Every field of a ``ReviewItem`` (payload and note included) as JSON text."""
    fields = {f.name: getattr(item, f.name) for f in dataclasses.fields(item)}  # type: ignore[arg-type]
    return json.dumps(fields, default=_json_default, ensure_ascii=False)


def run_sentinel_pipeline(
    tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LeakRun:
    """Plant the sentinels and drive the fake pipeline (IT10-10); needs ``ops_store`` (db at
    ``tmp_path/data/ops.sqlite``), ``fake_keyring`` and ``reset_process_state`` active."""
    cfg = init_sync_config(tmp_path, SOURCES_YAML)
    data_root = Path(cfg.paths.data)
    _bind_state(monkeypatch)
    configure_logging(
        "INFO", log_dir=Path(cfg.paths.logs), scrubber=secrets.scrub_secrets, stderr=False
    )
    tracer = Tracer(
        "run_" + new_ulid(),
        build_id=None,
        run_kind="chat",
        traces_dir=data_root / "traces",
        settings=_TRACE_ALL,
    )
    run = LeakRun(data_root, db_path, 0, "", [], [], [], [], None)
    try:
        secrets.set_secret(SECRET_NAME, SENTINEL_KEY, actor="system")  # the operator stores it
        record_config_change(cfg)  # data/config_snapshots/<hash>.yaml and a config_change line
        es.install_socket_guard(cfg)
        _drop_ticket(data_root)
        _run(run, tracer, monkeypatch)
    finally:
        tracer.close()
        reset_logging()
        es.reset_socket_guard()
    return run


def _files(root: Path) -> Iterator[tuple[str, str]]:
    if root.is_dir():
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            yield path.relative_to(root).as_posix(), path.read_text("utf-8", errors="replace")


def ops_dump(db_path: Path) -> str:
    """The ``sqlite3`` ``.dump`` text of the ops store."""
    conn = sqlite3.connect(db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def artefacts(run: LeakRun) -> dict[str, list[tuple[str, str]]]:
    """Every artefact ST10-14 greps, by category, as ``(name, text)`` pairs."""
    root = run.data_root
    return {
        "data/logs": list(_files(root / "logs")),
        "data/traces": list(_files(root / "traces")),
        "data/config_snapshots": list(_files(root / "config_snapshots")),
        "ops.sqlite .dump": [("ops.sqlite", ops_dump(run.db_path))],
        "data/reports": list(_files(root / "reports")),
        "model requests (FakeLLMClient)": [
            (f"request {i}", req.model_dump_json()) for i, req in enumerate(run.fake_requests)
        ],
        "model requests (wire)": [(f"body {i}", b) for i, b in enumerate(run.wire_bodies)],
        "review items": [(f"item {i}", p) for i, p in enumerate(run.review_payloads)],
    }


def known_secret_job_dump(tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A ``reconcile`` job fails with a message echoing the resolved keyring secret (no
    credential shape); returns the ops.sqlite dump for the known-value check."""
    init_sync_config(tmp_path, SOURCES_YAML)
    _bind_state(monkeypatch)
    secrets.set_secret(SECRET_NAME, SENTINEL_KEY, actor="system")
    token = secrets.resolve(f"secret:{SECRET_NAME}").get_secret_value()
    fail_job(f"upstream rejected credential {token}")
    return ops_dump(db_path)


@pytest.fixture
def leak_run(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LeakRun:
    """The IT10-10 sentinel run over the fixture ops store (shared by IT10-10 and ST10-14)."""
    del fake_keyring, reset_process_state
    return run_sentinel_pipeline(tmp_path, ops_store.db_path, monkeypatch)
