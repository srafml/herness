"""Composition root of the dashboard process (impl 09 U09-53; ENG §2.2, R-04, R-71).

The only place in ``app/`` that constructs concrete adapters. ``get_services`` runs once per
Streamlit process (``st.cache_resource``): socket guard, config, logging, port binding, start-up
validation (the same owner validators as the CLI, ``herness.cli.run_startup_validation``), then
the memory store, the chat service and the roster. Upper-layer modules are imported only after
the socket guard is installed (TH09-28).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import streamlit as st

from app.common.auth import Roster, build_roster
from app.common.wh import NamedQuery, check_forbidden_columns, load_named_queries
from herness.core import time as clock
from herness.core.config import init_config
from herness.core.config_sources import load_bootstrap
from herness.core.egress_socket import install_socket_guard
from herness.core.errors import ConfigError
from herness.core.logging import configure_logging
from herness.core.resilience import bind_chain_registry
from herness.core.secrets import scrub_secrets
from herness.reports.rules import load_user_ref_key
from herness.store.ops.resilience import bind_core_backends

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.config_sources import ProfileName
    from herness.harness.memory import MemoryStore
    from herness.harness.pipelines.chat import ChatService

__all__ = ["AppServices", "get_services", "load_queries"]

_GUARD_LOCK: Final = threading.Lock()  # ENG §2.3 exception: the guard is installed once
_guard_installed: list[bool] = []
QUERIES_FILE: Final = Path(__file__).with_name("queries.sql.yaml")


@dataclass(frozen=True)
class AppServices:
    """The process-wide services shared by every session (thread-safe, config immutable)."""

    cfg: HernessConfig
    memory: MemoryStore
    chat: ChatService
    roster: Roster
    queries: dict[str, NamedQuery]
    started_at: datetime


def _overrides(raw: str) -> list[str]:
    """``HERNESS_SET_OVERRIDES``: a JSON list of strings, else ``ConfigError``."""
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = "HERNESS_SET_OVERRIDES must be a JSON list of strings"
        raise ConfigError(msg, hint="Start the dashboard with `herness ui`.")
    return value


def load_queries(path: Path, *, page_row_limit: int) -> dict[str, NamedQuery]:
    """U09-53 step 10: load the named queries, then the raw-text guard (TH09-11)."""
    queries = load_named_queries(path, page_row_limit=page_row_limit)
    if violations := check_forbidden_columns(queries):
        msg = f"named query {violations[0].split(':', 1)[0]} reads raw ticket text"
        raise ConfigError(msg)
    return queries


def _install_guard_once(profile: ProfileName | None, config_dir: Path) -> None:
    with _GUARD_LOCK:
        if not _guard_installed:
            install_socket_guard(load_bootstrap(profile, config_dir))
            _guard_installed.append(True)


def build_services() -> AppServices:
    """U09-53 steps 1-10 (uncached; ``get_services`` caches it per process)."""
    env = os.environ
    config_dir = Path(env.get("HERNESS_CONFIG_DIR") or "config")
    profile = cast("ProfileName | None", env.get("HERNESS_PROFILE") or None)  # loader validates
    overrides = _overrides(env.get("HERNESS_SET_OVERRIDES") or "[]")
    if data_dir := env.get("HERNESS_DATA_DIR"):
        overrides.append(f"paths.data={Path(data_dir).as_posix()}")
    _install_guard_once(profile, config_dir)
    cfg = init_config(profile, overrides, config_dir, dict(env))  # + the get_config cache
    configure_logging(
        cfg.logging.level, log_dir=cfg.paths.logs, scrubber=scrub_secrets, stderr=True
    )
    bind_core_backends()  # R-04: before the start-up hook
    from herness.cli import run_startup_validation  # noqa: PLC0415 - after the guard
    from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415
    from herness.harness.memory import get_memory_store  # noqa: PLC0415
    from herness.harness.pipelines.chat import ChatService  # noqa: PLC0415

    run_startup_validation(cfg, config_dir=config_dir)  # R-71; an error issue -> ConfigError
    memory = get_memory_store()
    llms = LLMRegistry(cfg.models, profile=cfg.profile)
    bind_chain_registry(llms)  # R-04 port; the Overview's status_snapshot reads chat_policy
    chat = ChatService(cfg.pipelines, llms, memory)
    roster = build_roster(cfg.security.ui.roles, load_user_ref_key())
    queries = load_queries(QUERIES_FILE, page_row_limit=cfg.app.app.page_row_limit)
    return AppServices(cfg, memory, chat, roster, queries, clock.now())


@st.cache_resource(show_spinner=False)
def get_services() -> AppServices:
    """The process ``AppServices`` (built once per Streamlit process)."""
    return build_services()
