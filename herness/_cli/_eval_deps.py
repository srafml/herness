"""Composition root of ``eval`` jobs: the deps factory bound to ``herness.eval.runner``
(impl 09 U09-98, U09-104; impl 11 U11-66, U11-68; R-04). Private sibling of ``cmd_admin``.

``bind`` installs ``make_deps`` with ``set_deps_factory`` and registers the ``eval`` handler;
both are idempotent, so the ``eval`` command and the worker bootstrap may each call it.
``make_deps`` builds nothing heavy itself: the swarm, the chat service and the judge are built
on first use, per profile, from the loaded config. One ``WarehousePool`` and one ``Verifier``
serve the swarm, the chat service and ``warehouse_for`` (a second pool's hardening fails on
DuckDB's shared instance). With ``mock_llm_dir`` in the payload every model registry is
wrapped in ``ScriptedRegistry`` (``--mock-llm``): swarm and judge calls keyed by the ops task's
``dedup_key``, chat calls keyed ``chat``.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.eval.judge import RubricJudge
    from herness.eval.runner import EvalDeps
    from herness.eval.scripted import ScriptBook
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.pipelines.chat import ChatService
    from herness.harness.swarm import Swarm
    from herness.harness.verifier import Verifier
    from herness.harness.warehouse import WarehousePool

__all__ = ["bind", "make_deps"]


class _Root:
    """The checkout root (``tests/eval/baselines/``, the judge cache): the config dir's parent."""

    path: Path | None = None


def bind(config_dir: Path) -> None:
    """Bind ``make_deps`` as the eval deps factory and register the ``eval`` handler."""
    from herness.eval import runner  # noqa: PLC0415 - lazy (BT09-07)

    _Root.path = config_dir.resolve().parent
    runner.set_deps_factory(make_deps)
    runner.register_handlers()


class _Models:
    """Model registries per profile, built once each; scripted with ``--mock-llm``."""

    def __init__(self, cfg: HernessConfig, book: ScriptBook | None) -> None:
        self._cfg, self._book = cfg, book
        self._lock = threading.Lock()
        self._made: dict[tuple[str, bool], LLMRegistry] = {}

    def registry(self, profile: str, *, chat: bool = False) -> LLMRegistry:
        with self._lock:
            key = (profile, chat)
            if key not in self._made:
                self._made[key] = self._build(profile, chat=chat)
            return self._made[key]

    def _build(self, profile: str, *, chat: bool) -> LLMRegistry:
        from herness.core.resilience import bind_chain_registry  # noqa: PLC0415 - lazy
        from herness.eval import scripted_client as sc  # noqa: PLC0415
        from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415

        inner = LLMRegistry(self._cfg.models, profile=profile)
        if chat:
            bind_chain_registry(inner)  # R-04 port read by the chat mode policy
        if self._book is None:
            return inner
        resolver = None if chat else sc.ops_dedup_key_resolver()
        client = sc.ScriptedLLMClient(self._book, dedup_key_resolver=resolver)
        return cast("LLMRegistry", sc.ScriptedRegistry(inner, client))


@dataclasses.dataclass(frozen=True)
class _Shared:
    """What every collaborator of one eval job shares."""

    cfg: HernessConfig
    models: _Models
    pool: WarehousePool
    verifier: Verifier


def _swarm(shared: _Shared, profile: str) -> Swarm:
    from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
    from herness.harness.swarm.run import Swarm, SwarmDeps  # noqa: PLC0415

    cfg = shared.cfg
    deps = dataclasses.replace(SwarmDeps.from_config(cfg), warehouses=shared.pool,
                               verifier=shared.verifier)  # fmt: skip
    return Swarm(cfg.pipelines, shared.models.registry(profile), get_memory_store(), deps=deps)


def _chat(shared: _Shared, profile: str) -> ChatService:
    from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
    from herness.harness.pipelines.chat import ChatDeps, ChatService  # noqa: PLC0415

    cfg = shared.cfg
    deps = dataclasses.replace(ChatDeps.from_config(cfg), warehouses=shared.pool,
                               verifier=shared.verifier)  # fmt: skip
    llms = shared.models.registry(profile, chat=True)
    return ChatService(cfg.pipelines, llms, get_memory_store(), deps=deps)


def _judge(shared: _Shared, root: Path) -> RubricJudge:
    """``eval.judge``: its client key on the loaded profile, cache under the checkout root."""
    from herness.core.redact import redact_text  # noqa: PLC0415 - lazy
    from herness.eval.judge import RubricJudge  # noqa: PLC0415

    cfg = shared.cfg
    settings = cfg.eval
    assert settings is not None  # make_deps refused a config without it  # noqa: S101
    registry = shared.models.registry(cfg.profile)
    key = settings.judge.profile
    cache = Path(settings.judge.cache_dir)
    return RubricJudge(registry.client(key), registry.config(key),
                       cache_dir=cache if cache.is_absolute() else root / cache,
                       temperature=settings.judge.temperature, tracer=None,
                       redact=redact_text)  # fmt: skip


def make_deps(ctx: JobContext) -> EvalDeps:
    """The ``EvalDeps`` of one eval job (U11-66): ``ConfigError`` without ``eval`` settings."""
    from herness.core import time as clock  # noqa: PLC0415 - lazy (BT09-07)
    from herness.core.config import get_config  # noqa: PLC0415
    from herness.core.errors import ConfigError  # noqa: PLC0415
    from herness.eval.runner import EvalDeps  # noqa: PLC0415
    from herness.eval.scripted import load_scripts  # noqa: PLC0415
    from herness.harness.verifier import Verifier  # noqa: PLC0415
    from herness.harness.warehouse import WarehousePool  # noqa: PLC0415
    from herness.store.layout import data_layout  # noqa: PLC0415
    from herness.store.ops import evidence  # noqa: PLC0415

    cfg = get_config()
    if cfg.eval is None:
        msg = "eval settings are not configured"
        raise ConfigError(msg)
    mock = ctx.job.payload.get("mock_llm_dir")
    book = load_scripts(Path(mock)) if isinstance(mock, str) else None
    pool = WarehousePool(data_layout(cfg=cfg).warehouse, cfg.models.harness.sql)
    shared = _Shared(
        cfg, _Models(cfg, book), pool, Verifier(evidence, pool, cfg.models.harness.verifier)
    )
    root = _Root.path or Path.cwd()
    return EvalDeps(
        warehouse_for=lambda build_id: pool.get(build_id).cursor(),
        swarm_for=lambda profile: _swarm(shared, profile),
        chat_for=lambda profile: _chat(shared, profile),
        judge_for=lambda: _judge(shared, root),
        memory=None,  # unused by the runner on this base
        # T11-33 carry-over: seed_prior_run provider (HERNESS_ENV=test only)
        job=ctx, clock=clock.now, data_root=Path(cfg.paths.data), settings=cfg.eval,
        repo_root=root,
    )  # fmt: skip
