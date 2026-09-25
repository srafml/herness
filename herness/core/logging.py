"""Public logging API: configure, get a logger, bind context IDs, reset (design 00 §8).

structlog hands every event to the standard-library logging module, so structlog events
and third-party records share one JSON-lines formatter chain (impl 00 §3.4).
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from typing import Final, Literal

import structlog
from structlog.typing import Processor

from herness.core._log_pipeline import (
    CONTEXT_ID_KEYS,
    EVENT_NAME_RE,
    EXC_RENDERER,
    FILE_RETRY_S,
    LOG_FILE_PREFIX,
    MAX_DEPTH,
    MAX_FIELD_CHARS,
    MAX_LINE_BYTES,
    OMITTED,
    REQUIRED_KEYS,
    SECRET_KEYS,
    TEXT_KEYS,
    DailyJsonlHandler,
    SafeStreamHandler,
    add_component,
    add_timestamp,
    check_event_name,
    clean_early,
    guard_sensitive,
    limit_sizes,
    normalize_values,
    render_json,
)
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import IdKind, is_valid_build_id, is_valid_id

__all__ = [
    "COMPONENT_RE",
    "CONTEXT_ID_KEYS",
    "EVENT_NAME_RE",
    "FILE_RETRY_S",
    "LOG_FILE_PREFIX",
    "MAX_DEPTH",
    "MAX_FIELD_CHARS",
    "MAX_LINE_BYTES",
    "NOISY_LOGGERS",
    "OMITTED",
    "REQUIRED_KEYS",
    "SECRET_KEYS",
    "TEXT_KEYS",
    "LogLevel",
    "bind_ids",
    "configure_logging",
    "get_logger",
    "reset_logging",
]

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
COMPONENT_RE: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
_MAX_COMPONENT_CHARS: Final = 64
NOISY_LOGGERS: Final = (
    "httpx",
    "httpcore",
    "urllib3",
    "asyncio",
    "filelock",
    "huggingface_hub",
    "snowflake.connector",
    "pymongo",
    "openai",
    "anthropic",
)
_ID_VALIDATORS: Final[Mapping[str, Callable[[object], bool]]] = types.MappingProxyType(
    {
        "run_id": lambda value: is_valid_id(IdKind.RUN, value),
        "task_id": lambda value: is_valid_id(IdKind.TASK, value),
        "job_id": lambda value: is_valid_id(IdKind.JOB, value),
        "build_id": is_valid_build_id,
    }
)


class _State:
    """Handlers installed by this module (accepted ENG §2.3 exception, impl 00 §13.3)."""

    def __init__(self) -> None:
        self.handlers: list[logging.Handler] = []


_STATE: Final = _State()
_CONFIG_LOCK: Final = threading.Lock()


def _formatter(scrubber: Processor | None) -> logging.Formatter:
    steps: list[Processor] = [
        add_component,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.stdlib.add_log_level,
        add_timestamp,
        EXC_RENDERER,
        normalize_values,
        guard_sensitive,
    ]
    if scrubber is not None:
        steps.append(scrubber)
    steps += [limit_sizes, render_json]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.contextvars.merge_contextvars], processors=steps
    )


def _remove_handlers() -> None:
    root = logging.getLogger()
    for handler in _STATE.handlers:
        root.removeHandler(handler)
        handler.close()
    _STATE.handlers = []


def _install(
    lvl: str, handlers: list[logging.Handler], strict: bool, scrubber: Processor | None
) -> None:
    root = logging.getLogger()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(lvl)
    noisy = max(logging.getLevelNamesMapping()[lvl], logging.WARNING)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            check_event_name(strict),
            clean_early(scrubber),  # record.msg is clean for foreign root handlers too
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    _STATE.handlers = handlers


def configure_logging(
    level: str = "INFO",
    *,
    log_dir: pathlib.Path | None = None,
    scrubber: Processor | None = None,
    stderr: bool = True,
    strict_event_names: bool = False,
) -> None:
    """Configure process-wide JSON-lines logging for structlog and standard logging.

    Raises ConfigError (level) for a bad level, ConfigError when log_dir is given without a
    scrubber, and ConfigError (path) when log_dir cannot be created.
    """
    lvl = level.upper()
    if lvl not in _LEVELS:
        msg = "invalid log level"
        raise ConfigError(msg, level=level[:20])
    if log_dir is not None and scrubber is None:
        msg = "file logging requires a scrubber"
        raise ConfigError(msg)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = "cannot create log directory"
            raise ConfigError(msg, path=str(log_dir)) from exc
    with _CONFIG_LOCK:
        _remove_handlers()
        formatter = _formatter(scrubber)
        handlers: list[logging.Handler] = [logging.NullHandler()]
        if stderr:
            handlers.append(SafeStreamHandler())
        if log_dir is not None:
            handlers.append(DailyJsonlHandler(log_dir))
        for handler in handlers:
            handler.setFormatter(formatter)
        _install(lvl, handlers, strict_event_names, scrubber)
    get_logger("core.logging").info(
        "core.logging.configured",
        level=lvl,
        log_dir=None if log_dir is None else log_dir.as_posix(),
        scrubber=scrubber is not None,
        strict_event_names=strict_event_names,
    )


@contextlib.contextmanager
def bind_ids(**ids: str) -> Iterator[None]:
    """Attach run/task/job/build IDs to every line in this block (thread or task only).

    Raises SchemaViolation (key) for an unknown key or an invalid ID; values are not echoed.
    """
    for key, value in ids.items():
        validator = _ID_VALIDATORS.get(key)
        if validator is None:
            msg = "unknown log context key"
            raise SchemaViolation(msg, key=key[:40])
        if not validator(value):
            msg = "invalid id for log context"
            raise SchemaViolation(msg, key=key)
    tokens = structlog.contextvars.bind_contextvars(**ids)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Return a logger whose lines carry ``component``. Raises SchemaViolation (component).

    The logger stays lazy, so one created at import time follows later configuration.
    """
    if len(component) > _MAX_COMPONENT_CHARS or COMPONENT_RE.fullmatch(component) is None:
        msg = "invalid log component"
        raise SchemaViolation(msg, component=component[:_MAX_COMPONENT_CHARS])
    return structlog.stdlib.get_logger("herness." + component, component=component)


def reset_logging() -> None:
    """Undo configure_logging: remove and close handlers, restore defaults, clear context.

    After this call structlog's own defaults apply (not Herness's) until configure_logging
    runs again.
    """
    with _CONFIG_LOCK:
        _remove_handlers()
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
