"""Terminal chat test helpers (T09-25): ``herness chat`` through Typer's ``CliRunner``.

``chat_cli_env`` combines ``cli_env`` (config tree, OS user ``alice``, user-ref key) with the
migrated ``ops_store``. It registers the command groups (``herness chat`` among them) on the
real root app (lists restored afterwards; registration is idempotent, so ``main`` sees ``chat``
once), replaces the chat service and memory store with a ``FakeChatService`` that writes the
assistant row like spec 06 and a ``FakeMemoryStore``, masks e-mail addresses in place of the keyed
redaction, fixes the chat policy at ``env.mode[0]`` and records the ``clock.sleep`` calls of the
``defer`` poll.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from tests.support.cli_env import CliEnv, preserved_app
from tests.support.fake_chat import FakeChatService
from tests.support.fake_memory import FakeMemoryStore
from tests.support.ops_store import OpsStoreHandle
from typer.testing import CliRunner, Result

from herness import cli
from herness._cli import _chat_turn, cmd_chat
from herness.core import time as clock
from herness.core.types import ChatMode
from herness.reports import actions

__all__ = ["ChatCli", "chat_cli_env", "mask_email"]

BUILD_ID = "20260101-000000-ABCDEF"


def mask_email(text: str | None) -> str | None:
    """The redaction double: e-mail addresses become ``[EMAIL]``."""
    return None if text is None else re.sub(r"\S+@\S+", "[EMAIL]", text)


@dataclass
class ChatCli:
    """The environment of one terminal chat test."""

    cli: CliEnv
    store: OpsStoreHandle
    chat: FakeChatService = field(default_factory=lambda: FakeChatService(write_rows=True))
    memory: FakeMemoryStore = field(default_factory=FakeMemoryStore)
    mode: list[ChatMode] = field(default_factory=lambda: ["live"])
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[], None] | None = None

    def run(self, *args: str, lines: tuple[str, ...] = ()) -> Result:
        """``herness --config-dir <dir> <args>`` with ``lines`` typed at the prompt."""
        text = "".join(line + "\n" for line in lines)
        argv = ["--config-dir", str(self.cli.config_dir), *args]
        return CliRunner().invoke(cli.app, argv, input=text, standalone_mode=False)

    def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        con = sqlite3.connect(str(self.store.db_path))
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def write(self, sql: str, *params: object) -> None:
        con = sqlite3.connect(str(self.store.db_path))
        try:
            with con:
                con.execute(sql, params)
        finally:
            con.close()

    def add_evidence(self, query_id: str, sql: str, sample: object, params: object = None) -> None:
        """One ops ``evidence`` row (no hash check on the read path the chat uses)."""
        self.write(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 5)",
            query_id, BUILD_ID, sql, json.dumps(params or {}), "0" * 64, 2,
            json.dumps(sample), "2026-01-01T00:00:00.000000Z",
        )  # fmt: skip


@contextmanager
def chat_cli_env(
    cli_env: CliEnv, ops_store: OpsStoreHandle, mp: pytest.MonkeyPatch
) -> Iterator[ChatCli]:
    """See the module docstring."""
    cli_env.write_config()
    env = ChatCli(cli_env, ops_store)
    mp.setattr(actions, "redact_text", mask_email)
    mp.setattr(cmd_chat, "build_services", lambda cfg: (env.chat, env.memory))

    def policy(now: datetime) -> tuple[ChatMode, datetime | None]:
        mode = env.mode[0]
        return mode, (now + timedelta(hours=2) if mode == "defer" else None)

    def sleep(seconds: float) -> None:
        env.sleeps.append(seconds)
        if env.on_sleep is not None:
            env.on_sleep()

    mp.setattr(_chat_turn, "policy", policy)
    mp.setattr(clock, "sleep", sleep)
    with preserved_app():
        cli._register_groups(cli.app)  # idempotent: a later `main` adds nothing twice
        yield env
