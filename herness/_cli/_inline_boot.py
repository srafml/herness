"""Bootstrap of a CLI process for ``--inline`` (impl 09 U09-103, T09-29; private sibling).

The CLI's ``_run`` has already installed the socket guard and ``opts.config()`` loaded the
config from the CLI options, so this entry never reads the environment (unlike
``_worker_boot.boot``): it binds the L0 ports and registers every owner's handlers, with the
same handler objects as the worker. ``herness --help`` never loads this module (BT09-07).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.resilience import ProcessState, process_state

__all__ = ["boot_inline"]

_log = get_logger("cli")


class _Booted:
    """The ``ProcessState`` this process was inline-bootstrapped for."""

    state: ProcessState | None = None


def _register_tolerant(owner: str, kinds: tuple[str, ...], register: Callable[[], None]) -> None:
    """One owner: a kind already bound in this process keeps its handler, and a ConfigError
    (the ``local`` profile has no model client, T07-23 note 4) leaves the owner's kinds unbound."""
    state = process_state()
    with state.lock:
        held = dict(state.handlers)
        state.handlers.clear()  # the owner registers into an empty table: no conflicts
    try:
        register()
    except ConfigError as exc:
        with state.lock:
            state.handlers.clear()
        _log.warning(
            "cli.inline.handler_unavailable",
            owner=owner,
            kinds=list(kinds),
            error=type(exc).__name__,  # class only: a message could carry a secret
        )
    finally:
        with state.lock:
            state.handlers.update(held)


def boot_inline(config_dir: Path) -> None:
    """Bind the L0 ports and register every owner's handlers; once per process state."""
    state = process_state()
    if _Booted.state is state:
        return
    from herness._cli._worker_boot import owner_steps  # noqa: PLC0415 - lazy (BT09-07)
    from herness.store.ops.resilience import bind_core_backends  # noqa: PLC0415

    bind_core_backends()  # R-04
    for owner, kinds, register in owner_steps(config_dir):
        _register_tolerant(owner, kinds, register)
    _Booted.state = state
