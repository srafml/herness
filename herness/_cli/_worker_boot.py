"""Worker composition root body (impl 09 U09-104, T09-27; private sibling of ``herness.cli``).

``herness.cli.worker_bootstrap`` calls ``boot``: socket guard, config, logging, L0 ports,
start-up validation and every job handler, each registered through its owner's public function.
Every owner module is imported inside a function, after the guard is installed (TH09-28), and
``herness --help`` never loads this module (BT09-07).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState, process_state

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.config_sources import BootstrapConfig, ProfileName
    from herness.core.jobs.handlers import Handler
    from herness.enrich.distill import LlmFactory

__all__ = [
    "boot",
    "cached_handlers",
    "llm_factory",
    "memory_purge",
    "overrides_from_env",
    "owner_steps",
]

_ROLES: Final = ("enrich_decider", "cluster_namer")
_HANDLERS: dict[str, Handler] = {}  # built once per process: register_handler needs identity


class _Booted:
    """The ``ProcessState`` this process has been bootstrapped for; a fresh state resets it."""

    state: ProcessState | None = None


def overrides_from_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """``HERNESS_SET_OVERRIDES``: a JSON list of ``--set`` strings; absent means none."""
    raw = env.get("HERNESS_SET_OVERRIDES")
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        msg = "HERNESS_SET_OVERRIDES is not valid JSON"
        raise ConfigError(msg) from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        msg = "HERNESS_SET_OVERRIDES must be a JSON list of strings"
        raise ConfigError(msg)
    return tuple(cast("list[str]", parsed))


def memory_purge(record_id: str) -> int:
    """The ``memory_purge`` bound for ``herness.admin`` (R-54): ``MemoryStore.purge`` by record.

    One module-level function, so ``register_handlers`` sees the same object on every call.
    """
    from herness.harness.memory import get_memory_store  # noqa: PLC0415 - lazy (BT09-07)

    return get_memory_store().purge(record_id=record_id)


def llm_factory(
    role: Literal["enrich_decider", "cluster_namer"],
) -> tuple[object, str, int]:
    """``LlmFactory`` (R-05): the client, model name and concurrency of ``role`` for the
    loaded profile. Clients are built here, at call time, never at bootstrap."""
    from herness.core.config import get_config  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415

    cfg = get_config()
    registry = LLMRegistry(cfg.models, profile=cfg.profile)
    key = registry.model_for(role, "standard")
    client_cfg = registry.config(key)
    return registry.client(key), client_cfg.model, client_cfg.max_concurrency


def cached_handlers() -> dict[str, Handler]:
    """The ``build_pipeline`` and ``distill`` handlers, each built once per process."""
    if not _HANDLERS:
        from herness.enrich.distill import make_distill_handler  # noqa: PLC0415 - lazy
        from herness.model.build import make_build_pipeline_handler  # noqa: PLC0415

        factory = cast("LlmFactory", llm_factory)
        _HANDLERS["build_pipeline"] = make_build_pipeline_handler(llm_factory=factory)
        _HANDLERS["distill"] = make_distill_handler(factory)
    return dict(_HANDLERS)


def _configure(
    bootstrap_of: Callable[[Path, str | None], BootstrapConfig],
) -> tuple[HernessConfig, Path]:
    """Steps 1 to 3: socket guard, config from the environment, logging."""
    from herness.core.config import init_config  # noqa: PLC0415 - lazy (BT09-07)
    from herness.core.egress_socket import install_socket_guard  # noqa: PLC0415
    from herness.core.logging import configure_logging  # noqa: PLC0415
    from herness.core.secrets import scrub_secrets  # noqa: PLC0415

    env = os.environ
    config_dir = Path(env.get("HERNESS_CONFIG_DIR") or "config")
    profile = env.get("HERNESS_PROFILE") or None
    overrides = overrides_from_env(env)
    if env.get("HERNESS_DATA_DIR"):
        overrides += (f"paths.data={Path(env['HERNESS_DATA_DIR']).as_posix()}",)
    install_socket_guard(bootstrap_of(config_dir, profile))  # TH09-28: before any owner import
    cfg = init_config(cast("ProfileName | None", profile), overrides, config_dir)
    configure_logging(
        cfg.logging.level, log_dir=cfg.paths.logs, scrubber=scrub_secrets, stderr=True
    )
    return cfg, config_dir


def owner_steps(config_dir: Path) -> tuple[tuple[str, tuple[str, ...], Callable[[], None]], ...]:
    """Step 6: ``(owner, kinds, register)`` for every owner; ``eval`` with its deps (T09-24)."""
    import herness.admin  # noqa: PLC0415 - lazy: after the socket guard (TH09-28)
    import herness.connectors.jobs  # noqa: PLC0415
    import herness.harness.memory  # noqa: PLC0415
    import herness.harness.swarm.handler  # noqa: PLC0415
    from herness._cli import _eval_deps  # noqa: PLC0415
    from herness.core.jobs.handlers import register_handler  # noqa: PLC0415
    from herness.harness.pipelines.chat import chat_job_handler  # noqa: PLC0415
    from herness.harness.tools import tool_registry  # noqa: PLC0415

    def pipelines() -> None:
        for kind, handler in cached_handlers().items():
            register_handler(cast("Literal['build_pipeline', 'distill']", kind), handler)

    def admin() -> None:
        herness.admin.register_handlers(memory_purge=memory_purge)

    def memory() -> None:
        herness.harness.memory.register_memory_components(
            tool_registry(), herness.harness.memory.get_memory_store()
        )

    return (
        ("admin", ("maintenance",), admin),
        ("connectors", ("sync", "reconcile"), herness.connectors.jobs.register_job_handlers),
        ("memory", ("outcome_measure", "memory_maintenance"), memory),
        ("pipelines", ("build_pipeline", "distill"), pipelines),
        ("swarm", ("review",), herness.harness.swarm.handler.register_handlers),
        ("chat", ("chat",), lambda: register_handler("chat", chat_job_handler)),
        ("eval", ("eval",), lambda: _eval_deps.bind(config_dir)),  # set_deps_factory, register
    )


def _register_all(config_dir: Path) -> None:
    for _owner, _kinds, register in owner_steps(config_dir):
        register()


def boot(
    bootstrap_of: Callable[[Path, str | None], BootstrapConfig],
    validate: Callable[..., object],
) -> None:
    """U09-104: bootstrap this process once; a second call in the same process does nothing."""
    state = process_state()
    if _Booted.state is state:
        return
    cfg, config_dir = _configure(bootstrap_of)
    from herness.store.ops.resilience import bind_core_backends  # noqa: PLC0415 - after guard

    bind_core_backends()  # R-04
    validate(cfg, config_dir=config_dir)  # U09-106, R-71
    _register_all(config_dir)
    _Booted.state = state
