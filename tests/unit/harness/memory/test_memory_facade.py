"""Tests of the `MemoryStore` facade and its composition root (impl 07 UT07-85, UT07-48; T07-23).

UT07-85 builds the facade on the real collaborators (migrated `ops_store`, LanceDB in tmp,
the recording `FakeEmbed`), checks that every method delegates to exactly one unit with the
right arguments, and drives `health()` through ok / degraded / down. UT07-48 covers the
composition-root half: tools and both job handlers registered, idempotently. The import
tests run in a fresh interpreter: the package import stays cheap and cycle-free.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import structlog
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._write_env import (
    PATTERNS,
    ROOT,
    Env,
    make_writer,
    proposal,
    unit,
)

import herness.harness.memory as memory_pkg
from herness.core.config import get_config
from herness.core.errors import (
    ConfigError,
    ModelUnavailable,
    QueryError,
    ReportContractError,
    SchemaViolation,
)
from herness.core.ids import new_ulid
from herness.core.jobs.handlers import resolve_handler
from herness.core.resilience import ProcessState
from herness.core.types import RecommendationDraft
from herness.harness.memory import (
    HealthResult,
    MemoryStore,
    _compose,
    _facade,
    chat,
    episodic,
    get_memory_store,
    lora,
    maintenance,
    outcome,
    procedural,
    recommend,
)
from herness.harness.memory.recall import RecallResult
from herness.harness.memory.settings import MemoryConfig, WriteConfig
from herness.harness.memory.store import Embedder
from herness.harness.memory.tools import MemoryToolStore
from herness.harness.tools import ToolRegistry
from herness.store.errors import NotFoundError
from herness.store.ops import closed_loop, core

pytestmark = pytest.mark.unit

ALLOWED = (r"\b(19|20)\d{2}\b", r"(INC|CHG|PRB)\d+")
RUN_ID = "run_" + new_ulid()
FINDING_ID = "fnd_" + new_ulid()
HEAVY = ("lancedb", "torch", "sentence_transformers", "duckdb", "anthropic", "openai",
         "pyarrow", "sqlglot")  # fmt: skip


class Rec:
    """A recording stand-in for one unit: keeps every call, returns `result`."""

    def __init__(self, result: object = None) -> None:
        self.result, self.calls = result, list[tuple[tuple[Any, ...], dict[str, Any]]]()

    def __call__(self, *args: Any, **kwargs: Any) -> object:
        self.calls.append((args, kwargs))
        return self.result


def build(env: Env, tmp_path: Path, cfg: MemoryConfig | None = None,
          **overrides: Any) -> MemoryStore:  # fmt: skip
    """A facade on the writer env's collaborators (no LLM registry unless given)."""
    kwargs: dict[str, Any] = {
        "conn_factory": core.connection, "vectors": env.vectors,
        "embedder": Embedder(env.embed, model_name="bge-m3"), "redactor": env.redactor,
        "llms": None, "allowed_numeral_patterns": ALLOWED, "data_root": tmp_path / "data",
    }  # fmt: skip
    return MemoryStore(cfg or MemoryConfig(injection_patterns=PATTERNS), **(kwargs | overrides))


@pytest.fixture
def env(ops_store: OpsStoreHandle, tmp_path: Path) -> Env:
    """The writer env (Redactor, FakeEmbed, LanceDB in tmp) on the migrated ops store."""
    del ops_store
    return make_writer(tmp_path)


@pytest.fixture
def store(env: Env, tmp_path: Path) -> MemoryStore:
    """The facade under test."""
    return build(env, tmp_path)


# --- UT07-85 construct ---------------------------------------------------------------------


def test_ut07_85_construct_wires_the_units(store: MemoryStore, env: Env) -> None:
    """UT07-85 construction builds the deps bundles from the same collaborators, compiles
    the allowed numeral patterns once and creates the LanceDB table."""
    assert [p.pattern for p in store._allowed] == list(ALLOWED)
    assert store._writer._allowed == store._allowed  # one compiled tuple everywhere
    assert store._episodic.allowed is store._allowed
    assert store._chat.allowed is store._allowed
    assert store.outcome_deps.allowed is store._allowed
    assert store.outcome_deps.writer is store._writer
    assert store.outcome_deps.outcome is store._cfg.outcome
    assert store._episodic.episodic is store._cfg.episodic
    assert store._procedural.scanner is store._scanner
    assert store._procedural.writer is store._writer
    assert isinstance(store._procedural.guard, _compose.CurrentGuard)
    assert store.maintenance_deps.lifecycle is store._lifecycle
    assert store.maintenance_deps.procedural is store._procedural
    assert store.maintenance_deps.vectors is env.vectors
    assert store.maintenance_deps.embedder is store._embedder
    assert isinstance(store._chat.llms, _compose.NoModels)
    assert env.vectors.list_ids("", 1) == []  # ensure_table ran


def test_ut07_85_construct_with_vector_store_down(env: Env, tmp_path: Path) -> None:
    """UT07-85 a `ModelUnavailable` from `ensure_table` is logged; recall starts degraded."""

    class DownVectors:
        def ensure_table(self) -> None:
            msg = "memory vector store unavailable: ensure_table"
            raise ModelUnavailable(msg)

    with structlog.testing.capture_logs() as logs:
        build(env, tmp_path, vectors=DownVectors())
    assert [e["event"] for e in logs] == ["memory.recall.degraded"]
    assert logs[0]["reason"] == "ModelUnavailable"


def test_ut07_85_real_units_propose_recall_approve(store: MemoryStore, env: Env) -> None:
    """UT07-85 the facade runs the real write, recall and lifecycle paths end to end."""
    content = "Churn means customers who left the service."
    env.embed.overrides |= {"glossary: " + content: unit(0), "churn customers": unit(0)}
    stored = store.propose(proposal(content))
    assert stored.status == "active"
    hits = store.recall("churn customers", k=5)
    assert [h.item.memory_id for h in hits] == [stored.memory_id]
    status = store.recall_with_status("churn customers", k=5)
    assert status.degraded is False
    assert store.render(hits, 2000).startswith('<untrusted_data source="memory"')
    store.record_use([stored.memory_id], RUN_ID)
    store.expire_item(stored.memory_id, "superseded")
    assert store.recall("churn customers", k=5) == []
    assert store.expire() == 0


def test_ut07_85_satisfies_tool_store_protocol(store: MemoryStore) -> None:
    """UT07-85 C1: the facade has the `MemoryToolStore` methods with the same parameters
    (mypy checks the typed `register_memory_tools(registry, store)` call too)."""
    for name in ("recall_with_status", "propose", "record_use"):
        want = inspect.signature(getattr(MemoryToolStore, name))
        got = inspect.signature(getattr(MemoryStore, name))
        assert list(got.parameters) == list(want.parameters)
        assert [p.default for p in got.parameters.values()] == [
            p.default for p in want.parameters.values()
        ]
    tool_store: MemoryToolStore = store
    assert tool_store is store


# --- UT07-85 delegation ----------------------------------------------------------------------


def _sentinel() -> object:
    return object()


_NONE_RETURNING = frozenset({"reject", "expire_item", "record_use", "decide"})


def _patch(store: MemoryStore, monkeypatch: pytest.MonkeyPatch, owner: str, attr: str,
           result: object) -> Rec:  # fmt: skip
    rec = Rec(result)
    target = {"recaller": store._recaller, "writer": store._writer,
              "lifecycle": store._lifecycle}.get(owner)  # fmt: skip
    modules = {"episodic": episodic, "recommend": recommend, "procedural": procedural,
               "lora": lora, "chat": chat, "facade": _facade}  # fmt: skip
    monkeypatch.setattr(target if target is not None else modules[owner], attr, rec)
    return rec


@pytest.mark.parametrize(
    ("owner", "attr", "call", "args", "kwargs"),
    [
        ("recaller", "recall",
         lambda s: s.recall_with_status("q", ["semantic"], "flt", 5, "run-ctx"),
         ("q", ["semantic"], "flt", 5, "run-ctx"), {}),
        ("writer", "propose", lambda s: s.propose("item", "ctx"), ("item", "ctx"), {}),
        ("lifecycle", "approve", lambda s: s.approve("m", "u", "n", 0.9), ("m", "u", "n", 0.9),
         {}),
        ("lifecycle", "reject", lambda s: s.reject("m", "u", "n"), ("m", "u", "n"), {}),
        ("lifecycle", "expire", lambda s: s.expire("now"), ("now",), {}),
        ("lifecycle", "expire_item", lambda s: s.expire_item("m", "r", "s"), ("m", "r", "s"),
         {}),
        ("lifecycle", "record_use", lambda s: s.record_use(["m"], "r"), (["m"], "r"), {}),
        ("episodic", "prior_context", lambda s: s.prior_context("ctx", 900), ("ctx", 900),
         {"deps": "episodic"}),
        ("episodic", "decide", lambda s: s.decide("rec", "accepted", "why", "u", "eff"),
         ("rec", "accepted", "why", "u", "eff"), {"deps": "episodic"}),
        ("procedural", "promote_procedural", lambda s: s.promote_procedural("run"), ("run",),
         {"deps": "procedural"}),
        ("chat", "session_load", lambda s: s.session_load("sess"), ("sess",),
         {"deps": "chat"}),
        ("chat", "session_save_turn", lambda s: s.session_save_turn("sess", "run"),
         ("sess", "run"), {"deps": "chat"}),
    ],
)  # fmt: skip
def test_ut07_85_delegates_to_exactly_one_unit(  # noqa: PLR0913, PLR0917 - parametrized
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch, owner: str, attr: str,
    call: Callable[[MemoryStore], object], args: tuple[Any, ...], kwargs: dict[str, Any],
) -> None:  # fmt: skip
    """UT07-85 each facade method calls its one unit once with its arguments and the
    store's deps bundle, and returns the unit's result."""
    result = _sentinel()
    rec = _patch(store, monkeypatch, owner, attr, result)
    returned = call(store)
    assert returned is result or (returned is None and attr in _NONE_RETURNING)
    want = {k: getattr(store, "_" + v) for k, v in kwargs.items()}
    assert rec.calls == [(args, want)]


def test_ut07_85_recall_returns_hits(store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 `recall` is `MemoryRecaller.recall(...).hits`."""
    hits = [_sentinel()]
    rec = _patch(store, monkeypatch, "recaller", "recall",
                 RecallResult(hits=hits, degraded=True, n_candidates=1))  # type: ignore[arg-type]  # fmt: skip
    assert store.recall("q", None, "flt", 3, "run-ctx") is hits  # type: ignore[arg-type]
    assert rec.calls == [(("q", None, "flt", 3, "run-ctx"), {})]


def test_ut07_85_render_returns_text(store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 `render` returns `render_records(hits, max_tokens).text`."""
    rec = _patch(store, monkeypatch, "facade", "render_records", SimpleNamespace(text="block"))
    assert store.render(["h"], 120) == "block"  # type: ignore[list-item]
    assert rec.calls == [((["h"], 120), {})]


def test_ut07_85_outcome_adjustment(store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 `outcome_adjustment` binds U07-80 to the measured priors, the memory embedder,
    relatedness on CURRENT, `cfg.feedback` and the clock."""
    priors = [{"rec_id": "rec_x"}]
    monkeypatch.setattr(closed_loop, "outcomes_for_similarity", Rec(priors))
    rec = _patch(store, monkeypatch, "recommend", "outcome_adjustment", "adj")
    assert store.outcome_adjustment("draft", 0.6) == "adj"  # type: ignore[arg-type]
    ((args, kwargs),) = rec.calls
    assert args == ("draft", 0.6)
    assert kwargs["priors"] is priors
    assert kwargs["embed"] == store._embedder.embed
    assert kwargs["cfg"] is store._cfg.feedback
    assert kwargs["related"]("svc_a", "svc_a") is False  # no relatedness cache given
    assert kwargs["now"].tzinfo is not None


def _draft(rank: int = 1) -> dict[str, Any]:
    return {
        "rank": rank, "kind": "fund", "target_type": "service", "target_id": "svc_a",
        "summary": "Fund the checkout service.", "numbers": [], "expected_metric": None,
        "expected_delta_ref": None, "expected_usd_ref": None,
        "finding_ids": [FINDING_ID],
    }  # fmt: skip


def test_ut07_85_write_recommendations_converts_and_binds(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-85 dict drafts become `RecommendationDraft`s; priors are read once per call and
    reach every adjustment; the deps carry a non-default `write.max_content_chars`; log lines
    in the call carry `run_id`."""
    cfg = MemoryConfig(injection_patterns=PATTERNS, write=WriteConfig(max_content_chars=1500))
    store = build(env, tmp_path, cfg)
    measured = [{"rec_id": "rec_prior"}]
    priors = Rec(measured)
    monkeypatch.setattr(closed_loop, "outcomes_for_similarity", priors)
    seen: list[Any] = []

    def fake(run_id: str, recs: list[RecommendationDraft], *, deps: Any) -> list[str]:
        seen.append((run_id, recs, deps, structlog.contextvars.get_contextvars()))
        deps.adjust(recs[0], 0.5)
        deps.adjust(recs[1], 0.5)
        return ["rec_1", "rec_2"]

    monkeypatch.setattr(recommend, "write_recommendations", fake)
    adjust = Rec("adj")
    monkeypatch.setattr(recommend, "outcome_adjustment", adjust)
    typed = RecommendationDraft.model_validate(_draft(2))
    assert store.write_recommendations(RUN_ID, [_draft(1), typed]) == ["rec_1", "rec_2"]
    ((run_id, recs, deps, bound),) = seen
    assert run_id == RUN_ID
    assert bound["run_id"] == RUN_ID
    assert recs[0] == RecommendationDraft.model_validate(_draft(1))
    assert recs[1] is typed
    assert deps.content_max == 1500
    assert [(a, k["priors"]) for a, k in adjust.calls] == [
        ((recs[0], 0.5), measured),
        ((recs[1], 0.5), measured),
    ]
    assert all(k["priors"] is measured for _, k in adjust.calls)
    assert deps.allowed is store._allowed
    assert deps.writer is store._writer
    assert len(priors.calls) == 1
    assert "run_id" not in structlog.contextvars.get_contextvars()


def test_ut07_85_write_recommendations_invalid_dict(store: MemoryStore) -> None:
    """UT07-85 an invalid dict draft is `ReportContractError` naming rank and field only."""
    bad = _draft(3) | {"summary": ""}
    with pytest.raises(ReportContractError, match=r"^recommendation rank 3 invalid: summary$"):
        store.write_recommendations(RUN_ID, [bad])
    with pytest.raises(ReportContractError, match=r"rank 1 invalid: rank$"):
        store.write_recommendations(RUN_ID, [_draft(1) | {"rank": "x"}])


def test_ut07_85_write_recommendations_empty_and_bad_run_id(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-85 no drafts read no priors; an invalid run id is not bound (the unit decides)."""
    priors = Rec([])
    monkeypatch.setattr(closed_loop, "outcomes_for_similarity", priors)
    rec = _patch(store, monkeypatch, "recommend", "write_recommendations", [])
    assert store.write_recommendations("not-a-run", []) == []
    assert priors.calls == []
    assert rec.calls[0][0] == ("not-a-run", [])


def test_ut07_85_export_lora(store: MemoryStore, monkeypatch: pytest.MonkeyPatch,
                             tmp_path: Path) -> None:  # fmt: skip
    """UT07-85 `export_lora` builds `LoraDeps` (export root, config hash, embedder, scanner,
    `procedural.lora`) and delegates."""
    monkeypatch.setattr(_compose, "current_config_hash", lambda: "cfg_0123456789abcdef")
    rec = _patch(store, monkeypatch, "lora", "export_lora", "report")
    assert store.export_lora(tmp_path / "out", 0.9, golden_questions=["q"]) == "report"
    ((args, kwargs),) = rec.calls
    assert args == (tmp_path / "out", 0.9)
    assert kwargs["golden_questions"] == ["q"]
    deps = kwargs["deps"]
    assert deps.export_root == tmp_path / "data" / "models" / "lora_data"
    assert deps.config_hash == "cfg_0123456789abcdef"
    assert deps.embedder is store._embedder
    assert deps.scanner is store._scanner
    assert deps.config is store._cfg.procedural.lora
    assert deps.conn_factory is core.connection


@pytest.mark.parametrize("with_llms", [False, True])
def test_ut07_85_compactor(store: MemoryStore, monkeypatch: pytest.MonkeyPatch,
                           with_llms: bool) -> None:  # fmt: skip
    """UT07-85 `compactor` builds one `ContextCompactor` with the profile's client (None
    without a registry), a `TokenCounter(profile)` and the scratchpad ops (R-21)."""
    compactor, counter = Rec("compactor"), Rec("counter")
    monkeypatch.setattr(_facade, "ContextCompactor", compactor)
    monkeypatch.setattr(_facade, "TokenCounter", counter)
    if with_llms:
        monkeypatch.setattr(store, "_llms", SimpleNamespace(client=Rec("client")))
    profile = SimpleNamespace(name="fake")
    assert store.compactor(profile, ctx="ctx") == "compactor"  # type: ignore[arg-type]
    ((args, kwargs),) = compactor.calls
    assert args == (profile,)
    assert counter.calls == [((profile,), {})]
    assert kwargs["client"] == ("client" if with_llms else None)
    assert kwargs["ctx"] == "ctx"
    assert kwargs["cfg"] is store._cfg.compaction
    assert kwargs["allowed"] is store._allowed
    assert isinstance(kwargs["ops"], _compose.ScratchpadOps)


# --- UT07-85 health ----------------------------------------------------------------------------


def test_ut07_85_health_ok(store: MemoryStore) -> None:
    """UT07-85 a readable ops store and vector table: ok."""
    assert store.health() == HealthResult("ok", "")


def test_ut07_85_health_down(env: Env, tmp_path: Path) -> None:
    """UT07-85 an unreadable ops store: down (reason names the error type, no text)."""
    empty = tmp_path / "empty.sqlite"
    store = build(env, tmp_path)
    store._conn = lambda: sqlite3.connect(empty)  # no memory_item table
    result = store.health()
    assert (result.status, result.reason) == ("down", "ops store unavailable (OperationalError)")


def test_ut07_85_health_degraded_vectors(store: MemoryStore,
                                         monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """UT07-85 `list_ids` raising: degraded ("vector store unavailable")."""
    down = Rec()

    def raise_down(after: str, limit: int) -> None:
        down(after, limit)
        msg = "memory vector store unavailable: list_ids"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(store._vectors, "list_ids", raise_down)
    assert store.health() == HealthResult("degraded", "vector store unavailable (ModelUnavailable)")
    assert down.calls == [(("", 1), {})]


@pytest.mark.parametrize(("pending", "status"), [(1000, "ok"), (1001, "degraded")])
def test_ut07_85_health_pending_embeddings(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch, pending: int, status: str
) -> None:
    """UT07-85 more than 1,000 `embedding_pending` items: degraded; exactly 1,000: ok."""
    monkeypatch.setattr(_facade.ops, "pending_embedding_count", lambda conn: pending)
    assert store.health().status == status


# --- UT07-85 from_config (U07-98 collaborators) ----------------------------------------------


def test_ut07_85_from_config_builds_per_u07_98(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 `from_config` uses the ops connection, `VectorIndex()`, the decisions embedding
    model, `get_redactor()` (C3: the tools' slug redactor), `LLMRegistry(cfg.models, ...)`,
    the reports numeral patterns, `paths.data` and a `RelatednessCache`."""
    cfg = get_config()
    made: dict[str, Any] = {}
    for name in ("VectorIndex", "Embedder", "LLMRegistry", "RelatednessCache"):
        monkeypatch.setattr(_facade, name, made.setdefault(name, Rec(name)))
    monkeypatch.setattr(_facade, "get_redactor", lambda: "process-redactor")
    init = Rec()
    monkeypatch.setattr(MemoryStore, "__init__", init)
    MemoryStore.from_config(cfg)
    ((args, kwargs),) = init.calls
    assert args == (cfg.memory,)
    assert kwargs["conn_factory"] is core.connection
    assert kwargs["vectors"] == "VectorIndex"
    assert made["VectorIndex"].calls == [((), {})]
    assert made["Embedder"].calls == [((), {"model_name": cfg.decisions.embedding.model})]
    assert kwargs["redactor"] == "process-redactor"
    assert made["LLMRegistry"].calls == [((cfg.models,), {
        "profile": cfg.profile, "egress_enabled": cfg.security.egress.enabled})]  # fmt: skip
    assert kwargs["allowed_numeral_patterns"] == cfg.app.reports.allowed_numeral_patterns
    assert kwargs["data_root"] == cfg.paths.data
    assert kwargs["relatedness"] == "RelatednessCache"


# --- U07-98 get_memory_store ---------------------------------------------------------------


def test_ut07_85_get_memory_store_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 one instance per process, built from `get_config()`, until the reset hook."""
    built: list[Any] = []

    def fake(cfg: Any) -> object:
        built.append(cfg)
        return object()

    monkeypatch.setattr(MemoryStore, "from_config", fake)
    first = get_memory_store()
    assert get_memory_store() is first
    assert built == [get_config()]
    memory_pkg._reset_memory_store()
    second = get_memory_store()
    assert second is not first
    assert len(built) == 2


def test_ut07_85_get_memory_store_concurrent(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 concurrent first calls build exactly one store (lock-protected lazy init)."""
    gate, built = threading.Event(), list[object]()

    def slow(cfg: Any) -> object:
        gate.wait(5)
        built.append(object())
        return built[-1]

    monkeypatch.setattr(MemoryStore, "from_config", slow)
    got: list[object] = []
    threads = [threading.Thread(target=lambda: got.append(get_memory_store())) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(built) == 1
    assert got == built * 8


def test_ut07_85_get_memory_store_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 a `ConfigError` from the build propagates and nothing is cached."""

    def broken(cfg: Any) -> object:
        msg = "client x is off-network but egress is disabled in profile local"
        raise ConfigError(msg)

    monkeypatch.setattr(MemoryStore, "from_config", broken)
    with pytest.raises(ConfigError, match="off-network"):
        get_memory_store()
    monkeypatch.setattr(MemoryStore, "from_config", lambda cfg: "built")
    assert get_memory_store() == "built"


def test_ut07_85_reset_clears_handler_seams(store: MemoryStore) -> None:
    """UT07-85 the reset hook also clears the outcome and maintenance seams."""
    outcome.configure_outcome(store.outcome_deps)
    maintenance.configure_maintenance(store.maintenance_deps)
    memory_pkg._reset_memory_store()
    with pytest.raises(ConfigError, match="configure_outcome"):
        outcome.outcome_deps()
    with pytest.raises(ConfigError, match="configure_maintenance"):
        maintenance.maintenance_deps()


def test_ut07_85_lazy_exports() -> None:
    """UT07-85 `MemoryStore` and `HealthResult` are the `_facade` objects; other names fail."""
    assert memory_pkg.MemoryStore is _facade.MemoryStore
    assert memory_pkg.HealthResult is _facade.HealthResult
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        _ = memory_pkg.Nope


# --- UT07-48 composition-root registration ------------------------------------------------


def test_ut07_48_register_components_twice(
    store: MemoryStore, reset_process_state: ProcessState
) -> None:
    """UT07-48 register twice: the two tools once (owner 07), `outcome_measure` and
    `memory_maintenance` handlers registered, the handler seams set to the store's deps."""
    del reset_process_state
    registry = ToolRegistry()
    memory_pkg.register_memory_components(registry, store)
    tools = {name: registry._tools[name] for name in registry.names()}
    memory_pkg.register_memory_components(registry, store)
    assert registry.names() == ["propose_memory", "recall_memory"]
    assert all(registry._tools[n] is t for n, t in tools.items())
    assert resolve_handler("outcome_measure") is outcome.outcome_measure_handler
    assert resolve_handler("memory_maintenance") is maintenance.memory_maintenance_handler
    assert outcome.outcome_deps() is store.outcome_deps
    assert maintenance.maintenance_deps() is store.maintenance_deps


def test_ut07_48_seams_follow_latest_store(
    store: MemoryStore, env: Env, tmp_path: Path, reset_process_state: ProcessState
) -> None:
    """UT07-48 a second store re-points the seams; a foreign handler is a `ConfigError`."""
    del reset_process_state
    registry = ToolRegistry()
    memory_pkg.register_memory_components(registry, store)
    other = build(env, tmp_path)
    memory_pkg.register_memory_components(registry, other)
    assert outcome.outcome_deps() is other.outcome_deps
    assert maintenance.maintenance_deps() is other.maintenance_deps


def test_ut07_48_foreign_handler_conflict(
    store: MemoryStore, reset_process_state: ProcessState
) -> None:
    """UT07-48 a different handler already registered for `outcome_measure` propagates."""
    del reset_process_state
    from herness.core.jobs.handlers import register_handler  # noqa: PLC0415 - local to test

    register_handler("outcome_measure", lambda ctx: None)  # type: ignore[arg-type, return-value]
    with pytest.raises(ConfigError, match="already registered for outcome_measure"):
        memory_pkg.register_memory_components(ToolRegistry(), store)


@pytest.mark.parametrize("kind", ["outcome_measure", "memory_maintenance"])
def test_ut07_48_conflict_registers_nothing(
    store: MemoryStore, reset_process_state: ProcessState, kind: str
) -> None:
    """UT07-48 a foreign handler for either kind fails before any tool, handler or seam is
    registered or configured (atomic composition)."""
    del reset_process_state
    from herness.core.jobs.handlers import register_handler  # noqa: PLC0415 - local to test

    foreign = lambda ctx: None  # noqa: E731 - a stand-in handler
    register_handler(kind, foreign)  # type: ignore[arg-type]
    registry = ToolRegistry()
    with pytest.raises(ConfigError, match=f"already registered for {kind}"):
        memory_pkg.register_memory_components(registry, store)
    assert registry.names() == []
    assert resolve_handler(kind) is foreign
    other = "memory_maintenance" if kind == "outcome_measure" else "outcome_measure"
    with pytest.raises(ConfigError, match="no handler"):
        resolve_handler(other)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="configure_outcome"):
        outcome.outcome_deps()
    with pytest.raises(ConfigError, match="configure_maintenance"):
        maintenance.maintenance_deps()


# --- import cost and cycles (C13) -------------------------------------------------------------


def _child(code: str) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
        check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


_REPORT = (
    "import json, sys; print(json.dumps({'heavy': [m for m in %r if m in sys.modules],"
    " 'store': getattr(sys.modules['herness.harness.memory'], 'MemoryStore').__name__"
    " if %r else None}))"
)


def test_ut07_85_package_import_is_cheap() -> None:
    """UT07-85 `import herness.harness.memory` loads none of the heavy libraries."""
    got = _child("import herness.harness.memory; " + _REPORT % (HEAVY, False))
    assert got == {"heavy": [], "store": None}


@pytest.mark.parametrize(
    "first",
    [
        "herness.harness.memory.tools", "herness.harness.memory.outcome",
        "herness.harness.memory.maintenance", "herness.core.config",
        "herness.harness.memory.settings", "herness.harness.memory._facade",
    ],
)  # fmt: skip
def test_ut07_85_import_order_is_cycle_free(first: str) -> None:
    """UT07-85 importing a submodule (or the config root) first, then the package and
    `MemoryStore`, works in a fresh interpreter (no import cycle)."""
    code = (f"import {first}; import herness.harness.memory as m; m.MemoryStore; "
            "import herness.core.config; " + _REPORT % ((), True))  # fmt: skip
    assert _child(code)["store"] == "MemoryStore"


def test_ut07_85_config_then_store_import() -> None:
    """UT07-85 `from herness.harness.memory import MemoryStore` before the config root."""
    code = ("from herness.harness.memory import MemoryStore, get_memory_store; "
            "import herness.core.config; " + _REPORT % ((), True))  # fmt: skip
    assert _child(code)["store"] == "MemoryStore"


# --- UT07-85 composition adapters (_compose) ---------------------------------------------


def _build_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA core; CREATE SCHEMA metrics; CREATE SCHEMA scratch")
    con.execute("CREATE TABLE core.service (service_id VARCHAR, team_id VARCHAR)")
    con.execute("CREATE TABLE scratch.secret (x VARCHAR)")
    return con


def test_ut07_85_current_guard_follows_current_build() -> None:
    """UT07-85 `CurrentGuard` allows the CURRENT build's tables only (empty and unknown
    schemas dropped), allows nothing without a build, and rebuilds when CURRENT moves."""
    current: list[str | None] = [None]
    opened: list[str] = []

    def open_build(build_id: str) -> duckdb.DuckDBPyConnection:
        opened.append(build_id)
        return _build_db()

    guard = _compose.CurrentGuard(blocked=lambda: (), read_current=lambda: current[0],
                                  open_build=open_build)  # fmt: skip
    sql = "SELECT service_id FROM core.service"
    with pytest.raises(QueryError):
        guard.check(sql)
    current[0] = "20260901-120000-ABCDEF"
    assert guard.check(sql).sql
    guard.check(sql, allow_catalog=True)
    assert opened == ["20260901-120000-ABCDEF"]  # cached per build id
    with pytest.raises(QueryError):
        guard.check("SELECT x FROM scratch.secret")
    current[0] = "20260902-120000-ABCDEF"
    guard.check(sql)
    assert opened == ["20260901-120000-ABCDEF", "20260902-120000-ABCDEF"]


def test_ut07_85_current_guard_unreadable_build() -> None:
    """UT07-85 an unreadable CURRENT pointer or build allows nothing (and is retried)."""

    def bad_pointer() -> str | None:
        msg = "corrupt CURRENT"
        raise SchemaViolation(msg)

    def bad_open(build_id: str) -> duckdb.DuckDBPyConnection:
        msg = "build missing"
        raise NotFoundError(msg, kind="build", key=build_id)

    sql = "SELECT service_id FROM core.service"
    for guard in (
        _compose.CurrentGuard(blocked=lambda: (), read_current=bad_pointer),
        _compose.CurrentGuard(blocked=lambda: (), read_current=lambda: "b", open_build=bad_open),
    ):
        with pytest.raises(QueryError):
            guard.check(sql)
        with pytest.raises(QueryError):
            guard.check(sql)


def test_ut07_85_current_guard_blocked_columns_from_config() -> None:
    """UT07-85 the default blocked columns are `harness.sql.blocked_columns` of the config."""
    assert _compose._blocked_columns() == get_config().models.harness.sql.blocked_columns


def test_ut07_85_scratchpad_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 the compactor ops read U07-29 and save the "scratchpad" checkpoint key."""
    read, save = Rec("{}"), Rec()
    monkeypatch.setattr(_compose, "get_task_scratchpad", read)
    monkeypatch.setattr(_compose, "save_checkpoint", save)
    ops = _compose.ScratchpadOps()
    assert ops.get_task_scratchpad("task_x") == "{}"
    ops.save_scratchpad("task_x", {"a": 1})
    assert read.calls == [(("task_x",), {})]
    assert save.calls == [(("task_x", "scratchpad", {"a": 1}), {})]


def test_ut07_85_no_models() -> None:
    """UT07-85 without an LLM registry every chat model call is a `ConfigError`."""
    models = _compose.NoModels()
    for call in (lambda: models.model_for("chat", "fast"), lambda: models.client("x"),
                 lambda: models.config("x")):  # fmt: skip
        with pytest.raises(ConfigError, match="needs an LLM registry"):
            call()


def test_ut07_85_related(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 relatedness on CURRENT: no cache, no build or an unreadable pointer is False."""
    cache = SimpleNamespace(related=Rec(True))
    assert _compose.related(None, "a", "b") is False
    monkeypatch.setattr(_compose.warehouse, "read_current", lambda: None)
    assert _compose.related(cache, "a", "b") is False  # type: ignore[arg-type]
    monkeypatch.setattr(_compose.warehouse, "read_current", lambda: "build")
    assert _compose.related(cache, "a", "b") is True  # type: ignore[arg-type]
    assert cache.related.calls == [(("build", "a", "b"), {})]

    def broken() -> str | None:
        msg = "pointer unreadable"
        raise OSError(msg)

    monkeypatch.setattr(_compose.warehouse, "read_current", broken)
    assert _compose.related(cache, "a", "b") is False  # type: ignore[arg-type]


def test_ut07_85_current_config_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-85 the LoRA config hash is `config_hash(get_config())`."""
    seen = Rec("cfg_0123456789abcdef")
    monkeypatch.setattr(_compose, "config_hash", seen)
    assert _compose.current_config_hash() == "cfg_0123456789abcdef"
    assert seen.calls == [((get_config(),), {})]
