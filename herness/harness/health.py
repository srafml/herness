"""Health check for `herness doctor` (U05-73; design 05 ENG §4, spec 10).

No HTTP call of its own: client checks go through `LLMRegistry.health()` (T05-10), whose
values are already sanitised. Errors: none escape (each check catches `Exception`).
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.harness.llm.registry import LLMRegistry
from herness.harness.warehouse import BUILD_ID_RE, open_warehouse

__all__ = ["harness_health"]

_log = get_logger("harness.health")
_RANK: Final = {"ok": 0, "degraded": 1, "down": 2}
_ROUTES: Final = [(r, d) for r in ("analyst", "chat") for d in ("fast", "standard", "deep")]
_CURRENT_MAX_BYTES: Final = 65  # 64 + 1: bounded read, enough to detect a garbage file


def harness_health(
    registry: LLMRegistry, *, traces_dir: Path, warehouse_dir: Path
) -> dict[str, JsonValue]:
    """Health result for `herness doctor` (U05-73 algorithm); never raises."""
    clients, checks = _client_checks(registry)
    checks.append(_traces_check(traces_dir))
    checks.append(_warehouse_check(warehouse_dir))
    status = "ok"
    reasons: list[str] = []
    for check_status, reason in checks:
        status = check_status if _RANK[check_status] > _RANK[status] else status
        if reason:
            reasons.append(reason)
    return {"status": status, "reason": "; ".join(reasons), "clients": clients}


def _log_failure(check: str, exc: Exception) -> None:
    with contextlib.suppress(Exception):
        _log.warning("harness.health.check_failed", check=check, error_type=type(exc).__name__)


def _client_checks(registry: LLMRegistry) -> tuple[dict[str, JsonValue], list[tuple[str, str]]]:
    try:
        clients = registry.health()
    except Exception as exc:  # noqa: BLE001 - U05-73 "Errors: none escape"
        _log_failure("clients", exc)
        return {}, [("degraded", "client health unavailable")]

    down = {name for name, value in clients.items() if value.startswith("down")}
    route: set[str] = set()
    for role, depth in _ROUTES:
        with contextlib.suppress(ConfigError):
            route.update(registry.chain_for(role, depth))

    checks: list[tuple[str, str]] = []
    if route and route <= down:
        checks.append(("down", f"{'/'.join(sorted(route))} clients down"))
        down -= route
    checks.extend(("degraded", f"client {name} down") for name in sorted(down))
    return dict(clients), checks


def _traces_check(traces_dir: Path) -> tuple[str, str]:
    try:
        with tempfile.NamedTemporaryFile(dir=traces_dir):
            pass
    except Exception as exc:  # noqa: BLE001 - U05-73 "Errors: none escape"
        _log_failure("traces", exc)
        return "down", "traces dir not writable"
    return "ok", ""


def _warehouse_check(warehouse_dir: Path) -> tuple[str, str]:
    try:
        with (warehouse_dir / "CURRENT").open("rb") as current:
            build_id = current.read(_CURRENT_MAX_BYTES).decode("utf-8").strip()
        if BUILD_ID_RE.fullmatch(build_id) is None:
            msg = "invalid build id in CURRENT"
            raise ValueError(msg)  # noqa: TRY301 - folded into this check's own failure path
        sql = get_config().models.harness.sql
        open_warehouse(build_id, warehouse_dir=warehouse_dir, sql=sql).close()
    except Exception as exc:  # noqa: BLE001 - U05-73 "Errors: none escape"
        _log_failure("warehouse", exc)
        return "degraded", "current warehouse unavailable"
    return "ok", ""
