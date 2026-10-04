"""Shared setup for the ChatService tests (impl 06 T06-25: IT06-11 … IT06-36, ST06-11 … ST06-14).

Not a test module. A turn runs the real `ChatService` over a migrated ops store with the SQLite
jobs backend, the real loop (`run_agent`, `HarnessHooks`, the shipped `chat` role prompts) and
the real job queue. The model is a scripted `ListClient` per client key, the registry and the
spec 08 `chat_model_profile` are stand-ins keyed by chat mode, process tools are recording
stand-ins registered under their owners, and the Verifier compares each cited number with a
fixed truth table (`TruthVerifier`), so a planted wrong number fails as `mismatch`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from tests.support import loop_standin as ls
from tests.support.dispatch_standin import SyncTool, strict_schema, use_test_config
from tests.support.harness_fakes import FakeOps, FakeVectors, RecordingTracer
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness._blackboard_env import BUILD_ID, FakeWarehouse

from herness.core import config as c
from herness.core import redact as r
from herness.core.config import HernessConfig
from herness.core.errors import HernessError
from herness.core.jobs import queue
from herness.core.jobs.ports import bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import (
    ChatAnswer,
    ChatEvent,
    ChatMode,
    ItemResult,
    LLMResponse,
    NumberCheck,
    PriorContext,
    ToolCall,
    ToolContext,
    ToolResult,
    VerificationResult,
)
from herness.harness.memory.types import SessionContext
from herness.harness.pipelines.chat import ChatDeps, ChatService
from herness.harness.tools import TOOL_OWNERS, tool_registry
from herness.store.ops import append_chat_message, create_chat_session
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.resilience import SqliteResilienceBackend

LIVE, SMALL, CLOUD = "local-30b", "local-small-cpu", "claude-sonnet"
KEYS: dict[str, str] = {"live": LIVE, "small_model": SMALL, "cloud": CLOUD}
USER_REF = "a" * 32
NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
Q1, Q2 = "q_" + "1" * 16, "q_" + "2" * 16
PLANTED = "Priya Raman"
PROCESS_TOOLS = [name for name, owner in TOOL_OWNERS.items() if owner in {"05", "07"}]


class Tracer(RecordingTracer):
    """`RecordingTracer` with the `Tracer.close` the service calls."""

    closed = False

    def close(self, timeout_s: float = 5.0) -> None:
        del timeout_s
        self.closed = True


class Registry:
    """`LLMRegistry` stand-in: real client configs, one scripted client per key."""

    def __init__(self, cfg: HernessConfig, clients: Mapping[str, ls.ListClient]) -> None:
        self._cfg = cfg
        self.clients = dict(clients)

    def config(self, name: str) -> Any:
        return self._cfg.models.models.clients[name]

    def client(self, name: str) -> ls.ListClient:
        return self.clients[name]


class Jobs:
    """`ChatJobs`: the real queue's `enqueue`, `chat_model_profile` keyed by mode."""

    enqueue = staticmethod(queue.enqueue)

    def __init__(self, keys: Mapping[str, str | None] | None = None) -> None:
        self.keys = dict(KEYS if keys is None else keys)
        self.asked: list[str] = []

    def chat_model_profile(self, mode: ChatMode, depth: str = "fast") -> str | None:
        del depth
        self.asked.append(mode)
        return self.keys.get(mode)


class Memory:
    """`MemoryStore` stand-in recording the chat calls; `save` is the save-turn outcome."""

    def __init__(self, save: str | HernessError | None = None) -> None:
        self.save = save
        self.saved: list[tuple[str, str]] = []
        self.loaded: list[str] = []
        self.contexts: list[Any] = []

    def session_load(self, session_id: str) -> SessionContext:
        self.loaded.append(session_id)
        return SessionContext(session_id=session_id, summary=None, messages=[], memory_ids=[])

    def prior_context(self, run_ctx: Any, max_tokens: int = 3000) -> PriorContext:
        del max_tokens
        self.contexts.append(run_ctx)
        return PriorContext(items=[], rendered="", memory_ids=[], tally={})

    def session_save_turn(self, session_id: str, run_id: str) -> str | None:
        self.saved.append((session_id, run_id))
        if isinstance(self.save, HernessError):
            raise self.save
        return self.save

    def compactor(self, profile: Any, *, ctx: ToolContext) -> None:
        del profile, ctx


class TruthVerifier:
    """`verify_answer` against a truth table `(query_id, column) -> value`."""

    def __init__(self, truth: Mapping[tuple[str, str], object] | None = None) -> None:
        self.truth = dict(truth or {(Q1, "n"): 40, (Q2, "n"): 7})
        self.calls: list[ChatAnswer] = []
        self.error: HernessError | None = None

    def verify_answer(self, answer: ChatAnswer, build_id: str) -> VerificationResult:
        self.calls.append(answer)
        if self.error is not None:
            raise self.error
        checks = [
            NumberCheck(
                number_id=n.id, query_id=n.query_id, column=n.column, row_key=None,
                claimed=n.value, actual=self.truth.get((n.query_id, n.column)),
                result="match" if self.truth.get((n.query_id, n.column)) == n.value
                else "mismatch",
            )
            for n in answer.numbers
        ]  # fmt: skip
        failed = sum(1 for ch in checks if ch.result != "match")
        item = ItemResult(
            where="answer", passed=failed == 0, checks=checks, uncited=[], unknown_markers=[],
            bad_refs=[], unverified_findings=[],
        )  # fmt: skip
        return VerificationResult(
            build_id=build_id, passed=failed == 0, items=[item], n_numbers=len(checks),
            n_failed=failed, verified_at=NOW, duration_ms=1,
        )  # fmt: skip


class Metrics:
    """`ChatMetrics` recording every counter and histogram."""

    def __init__(self) -> None:
        self.counters: list[tuple[str, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def record_counter(
        self, name: str, value: float = 1.0, *, component: str,
        labels: Mapping[str, str] | None = None,
    ) -> None:  # fmt: skip
        del value, component
        self.counters.append((name, dict(labels or {})))

    def record_histogram(
        self, name: str, value: float, *, component: str, labels: Mapping[str, str] | None = None
    ) -> None:
        del component
        self.histograms.append((name, value, dict(labels or {})))


class Pool:
    """`WarehouseSource` over one in-memory `FakeWarehouse`."""

    def __init__(self) -> None:
        self.warehouse = FakeWarehouse()

    def get(self, build_id: str) -> FakeWarehouse:
        assert build_id == BUILD_ID
        return self.warehouse


# --- scripted model replies ----------------------------------------------------------------


def call(name: str, call_id: str = "c1", **arguments: JsonValue) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def ref(ref_id: str, value: int, query_id: str = Q1) -> dict[str, JsonValue]:
    return {"id": ref_id, "value": value, "unit": "count", "query_id": query_id, "column": "n",
            "row_key": None}  # fmt: skip


def final(text: str, numbers: Sequence[dict[str, JsonValue]] = (), **kw: Any) -> LLMResponse:
    """The structured `ChatAnswer` reply of the final call."""
    qids = sorted({str(n["query_id"]) for n in numbers})
    parsed: dict[str, JsonValue] = {"text": text, "numbers": list(numbers), "query_ids": qids}
    return ls.resp(text, parsed=parsed, **kw)


def answer_script(
    text: str,
    numbers: Sequence[dict[str, JsonValue]] = (),
    *,
    tool: bool = True,
    client: str = LIVE,
) -> list[LLMResponse | Exception]:
    """Optional `run_sql` step, the end turn, then the structured final."""
    steps: list[LLMResponse | Exception] = []
    if tool:
        steps.append(ls.resp(calls=[call("run_sql", sql="SELECT 1")], client=client))
    steps += [ls.resp("draft", client=client), final(text, numbers, client=client)]
    return steps


# --- environment ---------------------------------------------------------------------------


@dataclasses.dataclass
class Env:
    """One test's service, its fakes and the seeded session."""

    cfg: HernessConfig
    registry: Registry
    jobs: Jobs
    memory: Memory
    verifier: TruthVerifier
    metrics: Metrics
    tracers: list[Tracer]
    tools: dict[str, SyncTool]
    session_id: str = ""

    def service(self, *, hcfg: HernessConfig | None = None, **deps: Any) -> ChatService:
        base = ChatDeps.from_config(hcfg or self.cfg)
        tracers = self.tracers

        def tracer_factory(run_id: str, build_id: str) -> Any:
            del build_id
            tracers.append(Tracer(run_id))
            return tracers[-1]

        fields: dict[str, Any] = {
            "verifier": self.verifier, "warehouses": Pool(), "tracer_factory": tracer_factory,
            "metrics": self.metrics, "jobs": self.jobs,
            "current_build": lambda: BUILD_ID, "ops": FakeOps(), "vectors": FakeVectors(),
            "past_reader": lambda **_k: [],
        }  # fmt: skip
        replaced = dataclasses.replace(base, **(fields | deps))
        pipelines = (hcfg or self.cfg).pipelines
        return ChatService(pipelines, self.registry, self.memory, deps=replaced)  # type: ignore[arg-type]

    def script(self, key: str, replies: Sequence[LLMResponse | Exception]) -> ls.ListClient:
        client = ls.ListClient(list(replies), name=key)
        self.registry.clients[key] = client
        return client

    def ask(self, text: str = "How many incidents did Team One have?") -> str:
        message_id = append_chat_message(self.session_id, "user", text, now=NOW)
        assert message_id is not None
        return message_id

    def turn(self, mode: ChatMode = "live", **deps: Any) -> list[ChatEvent]:
        return list(self.service(**deps).answer(self.session_id, "ignored", USER_REF, mode))


def _run_sql(ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
    del ctx, kwargs
    return ToolResult(ok=True, content="n\n40", query_ids=[Q1], row_count=1)


def register_tools() -> dict[str, SyncTool]:
    """Recording stand-ins for every spec 05 and 07 chat tool (`run_sql` returns Q1)."""
    tools: dict[str, SyncTool] = {}
    for name in PROCESS_TOOLS:
        body = _run_sql if name == "run_sql" else None
        schema = strict_schema({"sql": {"type": "string"}}) if name == "run_sql" else None
        tools[name] = SyncTool(name, body, schema=schema)
        tool_registry().register(tools[name], owner=TOOL_OWNERS[name])  # type: ignore[arg-type]
    return tools


@pytest.fixture
def chat_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
) -> Iterator[Env]:
    """Test config, migrated store with the jobs and resilience backends, a test redactor
    with the planted name, one chat session."""
    del ops_store, reset_process_state
    cfg = use_test_config(tmp_path / "cfg")
    directory = NameDirectory.from_files(None, (PLANTED,), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    bind_jobs_backend(SqliteJobsBackend())
    bind_ops_backend(SqliteResilienceBackend())
    env = Env(
        cfg=cfg, registry=Registry(cfg, {}), jobs=Jobs(), memory=Memory(),
        verifier=TruthVerifier(), metrics=Metrics(), tracers=[], tools=register_tools(),
    )  # fmt: skip
    env.session_id = create_chat_session(USER_REF, now=NOW)
    yield env
    c.reset_config()


def types_of(events: Sequence[ChatEvent]) -> list[str]:
    """The event type names in order, consecutive `token` events collapsed into one."""
    out: list[str] = []
    for ev in events:
        if not (ev.type == "token" and out and out[-1] == "token"):
            out.append(ev.type)
    return out


def with_policy(cfg: HernessConfig, *, profile: str, approved: bool, egress: bool) -> HernessConfig:
    """`cfg` with another profile, egress switch and chat approval (TH06-12 cases)."""
    sec = cfg.security
    egress_cfg = sec.egress.model_copy(
        update={"enabled": egress, "purposes": ("reasoning",) if egress else ()}
    )
    policy = sec.data_policy.model_copy(update={"chat_approved": approved})
    security = sec.model_copy(update={"egress": egress_cfg, "data_policy": policy})
    return cfg.model_copy(update={"profile": profile, "security": security})
