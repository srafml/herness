"""R-46 exit codes (impl 09 U09-87): ``EXIT_CODES``, ``exit_code_for`` and
``exit_code_for_class_name``; private sibling of ``herness._cli.output``, which re-exports them.
"""

from __future__ import annotations

import importlib
import types
from collections.abc import Mapping
from typing import Final

from typer._click.exceptions import UsageError

from herness.core import errors as e
from herness.reports.rules import UserInputError

__all__ = ["EXIT_CODES", "exit_code_for", "exit_code_for_class_name"]

EXIT_CODES: Final[Mapping[int, str]] = types.MappingProxyType({
    0: "Success, including a --no-wait enqueue",
    1: "General failure",
    2: "Usage error",
    3: "Configuration error",
    4: "Source auth or availability failure after retries",
    5: "Build blocked (DQ error or SQL failure)",
    6: "Finished with dead tasks or a partial run (report still written)",
    7: "Not found (run, job, item, memory, build, record)",
    8: "Store busy or another writer holds the lease",
    9: "Model or GPU unavailable after the fallback chain",
    10: "Budget exceeded",
    11: "Permission denied",
    12: "Report contract violation",
    13: "Off-network call blocked",
    14: "An eval gate failed",
    130: "Interrupted by Ctrl+C (detached; the job keeps running)",
})  # fmt: skip

# U09-87 rows 3-13 in table order; CircuitOpen is placed by its key (_circuit_code).
_TABLE: Final[tuple[tuple[tuple[type[e.HernessError], ...], int], ...]] = (
    ((e.ConfigError,), 3),
    ((e.AuthError, e.SourceUnavailable, e.RateLimited), 4),
    ((e.SchemaViolation,), 5),
    ((e.NotFound,), 7),
    ((e.StoreBusy,), 8),
    ((e.ModelUnavailable, e.ModelRefused), 9),
    ((e.BudgetExceeded,), 10),
    ((e.PermissionDenied,), 11),
    ((e.ReportContractError,), 12),
    ((e.EgressBlocked,), 13),
)
_USAGE: Final = (UsageError, UserInputError)


def _circuit_code(key: str | None) -> int:
    return 9 if key is not None and key.startswith(("model:", "decider:")) else 4


def _code_of_class(cls: type, key: str | None) -> int:
    if issubclass(cls, e.CircuitOpen):
        return _circuit_code(key)
    return next((code for classes, code in _TABLE if issubclass(cls, classes)), 1)


def exit_code_for(exc: BaseException) -> int:
    """R-46 exit code of ``exc``; 2 only for the two usage classes (U09-87)."""
    if isinstance(exc, KeyboardInterrupt):
        return 130
    if isinstance(exc, _USAGE):
        return 2
    return _code_of_class(type(exc), getattr(exc, "key", None))


# Modules defining taxonomy subclasses that a job's ``last_error["class"]`` may name; imported
# on first lookup so the table does not depend on what the process happened to import (I-1).
_TAXONOMY_MODULES: Final = (
    "herness.connectors.files",
    "herness.connectors.http",
    "herness.harness.memory.types",
    "herness.model.errors",
    "herness.store.errors",
)


def _taxonomy() -> dict[str, type]:
    """Every HernessError subclass by name (a job's ``last_error["class"]``)."""
    for module in _TAXONOMY_MODULES:
        importlib.import_module(module)
    found: dict[str, type] = {}
    pending: list[type] = [e.HernessError]
    while pending:
        cls = pending.pop()
        found.setdefault(cls.__name__, cls)
        pending.extend(cls.__subclasses__())
    return found


def exit_code_for_class_name(name: str, *, key: str | None = None) -> int:
    """The U09-87 table applied to a class name (``key`` for ``CircuitOpen``); unknown -> 1."""
    cls = _taxonomy().get(name)
    return 1 if cls is None else _code_of_class(cls, key)
