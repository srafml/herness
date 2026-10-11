"""Composition root of ``eval`` jobs (impl 09 U09-98, U09-104; impl 11 U11-66, U11-68;
R-04); private sibling of ``cmd_admin``. ``bind`` installs ``make_deps`` (``set_deps_factory``)
and registers ``run_eval_job``: ``handle_eval``, then in ``finally`` the job's teardown (T09-24
review r2 ruling): every ``Swarm`` closed, then the job's ``WarehousePool``, so the build is
free for the next job's own pool (a second pool's hardening fails while a handle is open).
Within a job one pool and ``Verifier`` and one ``Swarm`` / ``ChatService`` per profile; only
the model registries (no DuckDB handle) are cached per process state and config. With
``mock_llm_dir`` each job wraps them in ``ScriptedRegistry`` over its own ``ScriptBook``
(swarm and judge keyed by the ops task's ``dedup_key``, chat keyed ``chat``).
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.core.resilience import ProcessState
    from herness.core.types import JobOutcome
    from herness.eval.judge import RubricJudge
    from herness.eval.runner import EvalDeps
    from herness.eval.scripted import ScriptBook
    from herness.harness.llm.registry import LLMRegistry
    from herness.harness.pipelines.chat import ChatService
    from herness.harness.swarm import Swarm
    from herness.harness.verifier import Verifier
    from herness.harness.warehouse import WarehousePool

__all__ = ["bind", "make_deps", "run_eval_job"]


class _Root:
    """The checkout root (``tests/eval/baselines/``, the judge cache): the config dir's parent."""

    path: Path | None = None


def bind(config_dir: Path) -> None:
    """Bind ``make_deps`` as the eval deps factory and register ``run_eval_job``."""
    from herness.core.jobs import handlers  # noqa: PLC0415 - lazy (BT09-07)
    from herness.eval import runner  # noqa: PLC0415

    _Root.path = config_dir.resolve().parent
    runner.set_deps_factory(make_deps)
    handlers.register_handler("eval", run_eval_job)


def run_eval_job(ctx: JobContext) -> JobOutcome:
    """The ``eval`` job handler: the runner's ``handle_eval``, then this job's teardown."""
    from herness.eval import runner  # noqa: PLC0415 - lazy (BT09-07)

    try:
        return runner.handle_eval(ctx)
    finally:
        close_job(ctx.job_id)


class _Registries:
    """Model registries per (profile, chat), cached for one process state and config."""

    lock = threading.Lock()
    key: tuple[ProcessState, HernessConfig] | None = None
    made: dict[tuple[str, bool], LLMRegistry] = {}  # noqa: RUF012 - reset with `key`

    @classmethod
    def get(cls, cfg: HernessConfig, profile: str, *, chat: bool) -> LLMRegistry:
        from herness.core.resilience import bind_chain_registry, process_state  # noqa: PLC0415
        from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415

        with cls.lock:
            key = (process_state(), cfg)
            if cls.key is None or cls.key[0] is not key[0] or cls.key[1] is not cfg:
                cls.key, cls.made = key, {}
            if (profile, chat) not in cls.made:
                registry = LLMRegistry(cfg.models, profile=profile)
                if chat:
                    bind_chain_registry(registry)  # R-04 port read by the chat mode policy
                cls.made[profile, chat] = registry
            return cls.made[profile, chat]


@dataclasses.dataclass
class _Job:
    """What one eval job builds; ``close`` ends its lifetime (see the module docstring)."""

    cfg: HernessConfig
    book: ScriptBook | None
    pool: WarehousePool
    verifier: Verifier
    lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
    swarms: dict[str, Swarm] = dataclasses.field(default_factory=dict)
    chats: dict[str, ChatService] = dataclasses.field(default_factory=dict)

    def registry(self, profile: str, *, chat: bool = False) -> LLMRegistry:
        from herness.eval import scripted_client as sc  # noqa: PLC0415 - lazy

        inner = _Registries.get(self.cfg, profile, chat=chat)
        if self.book is None:
            return inner
        resolver = None if chat else sc.ops_dedup_key_resolver()
        client = sc.ScriptedLLMClient(self.book, dedup_key_resolver=resolver)
        return cast("LLMRegistry", sc.ScriptedRegistry(inner, client))

    def swarm(self, profile: str) -> Swarm:
        from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
        from herness.harness.swarm.run import Swarm, SwarmDeps  # noqa: PLC0415

        with self.lock:
            if profile not in self.swarms:
                deps = dataclasses.replace(SwarmDeps.from_config(self.cfg), warehouses=self.pool,
                                           verifier=self.verifier)  # fmt: skip
                self.swarms[profile] = Swarm(self.cfg.pipelines, self.registry(profile),
                                             get_memory_store(), deps=deps)  # fmt: skip
            return self.swarms[profile]

    def chat(self, profile: str) -> ChatService:
        from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
        from herness.harness.pipelines.chat import ChatDeps, ChatService  # noqa: PLC0415

        with self.lock:
            if profile not in self.chats:
                deps = dataclasses.replace(ChatDeps.from_config(self.cfg), warehouses=self.pool,
                                           verifier=self.verifier)  # fmt: skip
                self.chats[profile] = ChatService(self.cfg.pipelines,
                                                  self.registry(profile, chat=True),
                                                  get_memory_store(), deps=deps)  # fmt: skip
            return self.chats[profile]

    def judge(self, root: Path) -> RubricJudge:
        """``eval.judge``: its client key on the loaded profile, cache under the checkout root."""
        from herness.core.redact import redact_text  # noqa: PLC0415 - lazy
        from herness.eval.judge import RubricJudge  # noqa: PLC0415

        settings = self.cfg.eval
        assert settings is not None  # make_deps refused a config without it  # noqa: S101
        registry = self.registry(self.cfg.profile)
        key, cache = settings.judge.profile, Path(settings.judge.cache_dir)
        return RubricJudge(registry.client(key), registry.config(key),
                           cache_dir=cache if cache.is_absolute() else root / cache,
                           temperature=settings.judge.temperature, tracer=None,
                           redact=redact_text)  # fmt: skip

    def close(self) -> None:
        """Stop every swarm's writer, then close every warehouse handle of the job."""
        with self.lock:
            try:
                for swarm in self.swarms.values():
                    swarm.close()
            finally:
                self.pool.close_all()


class _Jobs:
    lock = threading.Lock()
    open: dict[str, _Job] = {}  # noqa: RUF012 - job id -> its collaborators


def close_job(job_id: str) -> None:
    """End the lifetime of what ``make_deps`` built for ``job_id`` (no-op when none)."""
    with _Jobs.lock:
        job = _Jobs.open.pop(job_id, None)
    if job is not None:
        job.close()


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
    mock_dir = ctx.job.payload.get("mock_llm_dir")
    book = load_scripts(Path(mock_dir)) if isinstance(mock_dir, str) else None
    pool = WarehousePool(data_layout(cfg=cfg).warehouse, cfg.models.harness.sql)
    close_job(ctx.job_id)  # a retried job's earlier attempt, if any
    with _Jobs.lock:
        _Jobs.open[ctx.job_id] = job = _Job(
            cfg, book, pool, Verifier(evidence, pool, cfg.models.harness.verifier)
        )
    root = _Root.path or Path.cwd()
    return EvalDeps(
        warehouse_for=lambda build_id: job.pool.get(build_id).cursor(),
        swarm_for=job.swarm, chat_for=job.chat, judge_for=lambda: job.judge(root),
        memory=None,  # unused by the runner on this base
        # T11-33 carry-over: seed_prior_run provider (HERNESS_ENV=test only)
        job=ctx, clock=clock.now, data_root=Path(cfg.paths.data), settings=cfg.eval,
        repo_root=root,
    )  # fmt: skip
