"""Composition root of ``eval`` jobs: the deps factory bound to ``herness.eval.runner``
(impl 09 U09-98, U09-104; impl 11 U11-66, U11-68; R-04). Private sibling of ``cmd_admin``.

``bind`` installs ``make_deps`` with ``set_deps_factory`` and registers the ``eval`` handler;
both are idempotent, so the ``eval`` command and the worker bootstrap may each call it.
The runner has no teardown hook, so what a job uses is a process-wide cache, rebuilt (and the
old one closed) only when the process state or the loaded config object changes: one
``WarehousePool`` and one ``Verifier`` (a second pool's hardening fails while another handle
on the same build is open), the model registries and one ``Swarm`` / ``ChatService`` per
profile and mock mode, built on first use. With ``mock_llm_dir`` the registries are wrapped
in ``ScriptedRegistry`` over a client slot that gets each job's fresh ``ScriptBook`` (a book
counts calls): swarm and judge calls keyed by the ops task's ``dedup_key``, chat calls
keyed ``chat``. Mock jobs share that slot, so run them one at a time per process.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.core.resilience import ProcessState
    from herness.eval.judge import RubricJudge
    from herness.eval.runner import EvalDeps
    from herness.eval.scripted import ScriptBook
    from herness.eval.scripted_client import DedupKeyResolver, ScriptedLLMClient
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


class _ScriptSlot:
    """A scripted client whose ``ScriptBook`` is replaced per mock job."""

    def __init__(self, resolver: DedupKeyResolver | None) -> None:
        self._resolver = resolver
        self.client: ScriptedLLMClient | None = None

    def load(self, book: ScriptBook) -> None:
        from herness.eval.scripted_client import ScriptedLLMClient  # noqa: PLC0415 - lazy

        self.client = ScriptedLLMClient(book, dedup_key_resolver=self._resolver)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - delegates the LLMClient surface
        if self.client is None:
            msg = "no mock scripts loaded"
            raise AttributeError(msg)
        return getattr(self.client, name)


class _Models:
    """Model registries per (profile, chat, mock), built once each."""

    def __init__(self, cfg: HernessConfig) -> None:
        from herness.eval.scripted_client import ops_dedup_key_resolver  # noqa: PLC0415

        self._cfg = cfg
        self._made: dict[tuple[str, bool, bool], LLMRegistry] = {}
        self.slots = {False: _ScriptSlot(ops_dedup_key_resolver()), True: _ScriptSlot(None)}

    def registry(self, profile: str, *, chat: bool = False, mock: bool = False) -> LLMRegistry:
        key = (profile, chat, mock)
        if key not in self._made:
            self._made[key] = self._build(profile, chat=chat, mock=mock)
        return self._made[key]

    def _build(self, profile: str, *, chat: bool, mock: bool) -> LLMRegistry:
        from herness.core.resilience import bind_chain_registry  # noqa: PLC0415 - lazy
        from herness.eval.scripted_client import ScriptedRegistry  # noqa: PLC0415
        from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415

        inner = LLMRegistry(self._cfg.models, profile=profile)
        if chat:
            bind_chain_registry(inner)  # R-04 port read by the chat mode policy
        if not mock:
            return inner
        slot = cast("ScriptedLLMClient", self.slots[chat])
        return cast("LLMRegistry", ScriptedRegistry(inner, slot))


@dataclasses.dataclass
class _Shared:
    """The process-wide collaborators of eval jobs (see the module docstring)."""

    state: ProcessState
    cfg: HernessConfig
    models: _Models
    pool: WarehousePool
    verifier: Verifier
    lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
    swarms: dict[tuple[str, bool], Swarm] = dataclasses.field(default_factory=dict)
    chats: dict[tuple[str, bool], ChatService] = dataclasses.field(default_factory=dict)

    def swarm(self, profile: str, *, mock: bool) -> Swarm:
        from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
        from herness.harness.swarm.run import Swarm, SwarmDeps  # noqa: PLC0415

        with self.lock:
            if (profile, mock) not in self.swarms:
                deps = dataclasses.replace(SwarmDeps.from_config(self.cfg), warehouses=self.pool,
                                           verifier=self.verifier)  # fmt: skip
                llms = self.models.registry(profile, mock=mock)
                self.swarms[profile, mock] = Swarm(self.cfg.pipelines, llms, get_memory_store(),
                                                   deps=deps)  # fmt: skip
            return self.swarms[profile, mock]

    def chat(self, profile: str, *, mock: bool) -> ChatService:
        from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy
        from herness.harness.pipelines.chat import ChatDeps, ChatService  # noqa: PLC0415

        with self.lock:
            if (profile, mock) not in self.chats:
                deps = dataclasses.replace(ChatDeps.from_config(self.cfg), warehouses=self.pool,
                                           verifier=self.verifier)  # fmt: skip
                llms = self.models.registry(profile, chat=True, mock=mock)
                self.chats[profile, mock] = ChatService(self.cfg.pipelines, llms,
                                                        get_memory_store(), deps=deps)  # fmt: skip
            return self.chats[profile, mock]

    def judge(self, root: Path, *, mock: bool) -> RubricJudge:
        """``eval.judge``: its client key on the loaded profile, cache under the checkout root."""
        from herness.core.redact import redact_text  # noqa: PLC0415 - lazy
        from herness.eval.judge import RubricJudge  # noqa: PLC0415

        settings = self.cfg.eval
        assert settings is not None  # make_deps refused a config without it  # noqa: S101
        with self.lock:
            registry = self.models.registry(self.cfg.profile, mock=mock)
        key = settings.judge.profile
        cache = Path(settings.judge.cache_dir)
        return RubricJudge(registry.client(key), registry.config(key),
                           cache_dir=cache if cache.is_absolute() else root / cache,
                           temperature=settings.judge.temperature, tracer=None,
                           redact=redact_text)  # fmt: skip

    def close(self) -> None:
        """Stop every swarm's writer and close every warehouse handle."""
        for swarm in self.swarms.values():
            swarm.close()
        self.pool.close_all()


class _Cache:
    lock = threading.Lock()
    shared: _Shared | None = None


def _shared(cfg: HernessConfig) -> _Shared:
    """The cache for this process state and config object; a stale one is closed first."""
    from herness.core.resilience import process_state  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness.verifier import Verifier  # noqa: PLC0415
    from herness.harness.warehouse import WarehousePool  # noqa: PLC0415
    from herness.store.layout import data_layout  # noqa: PLC0415
    from herness.store.ops import evidence  # noqa: PLC0415

    state = process_state()
    with _Cache.lock:
        held = _Cache.shared
        if held is not None and held.state is state and held.cfg is cfg:
            return held
        if held is not None:
            held.close()
        pool = WarehousePool(data_layout(cfg=cfg).warehouse, cfg.models.harness.sql)
        verifier = Verifier(evidence, pool, cfg.models.harness.verifier)
        _Cache.shared = _Shared(state, cfg, _Models(cfg), pool, verifier)
        return _Cache.shared


def make_deps(ctx: JobContext) -> EvalDeps:
    """The ``EvalDeps`` of one eval job (U11-66): ``ConfigError`` without ``eval`` settings."""
    from herness.core import time as clock  # noqa: PLC0415 - lazy (BT09-07)
    from herness.core.config import get_config  # noqa: PLC0415
    from herness.core.errors import ConfigError  # noqa: PLC0415
    from herness.eval.runner import EvalDeps  # noqa: PLC0415
    from herness.eval.scripted import load_scripts  # noqa: PLC0415

    cfg = get_config()
    if cfg.eval is None:
        msg = "eval settings are not configured"
        raise ConfigError(msg)
    mock_dir = ctx.job.payload.get("mock_llm_dir")
    book = load_scripts(Path(mock_dir)) if isinstance(mock_dir, str) else None
    shared = _shared(cfg)
    mock = book is not None
    if book is not None:
        for slot in shared.models.slots.values():
            slot.load(book)  # this job's fresh call counters
    root = _Root.path or Path.cwd()
    return EvalDeps(
        warehouse_for=lambda build_id: shared.pool.get(build_id).cursor(),
        swarm_for=lambda profile: shared.swarm(profile, mock=mock),
        chat_for=lambda profile: shared.chat(profile, mock=mock),
        judge_for=lambda: shared.judge(root, mock=mock),
        memory=None,  # unused by the runner on this base
        # T11-33 carry-over: seed_prior_run provider (HERNESS_ENV=test only)
        job=ctx, clock=clock.now, data_root=Path(cfg.paths.data), settings=cfg.eval,
        repo_root=root,
    )  # fmt: skip
