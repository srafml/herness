"""CLI test environment (impl 09 §11, T09-20): config tree with roles, OS user, isolation.

``cli_env`` gives a test a temporary working directory, an in-memory keyring holding
``ui_user_ref_key``, a patched ``getpass.getuser`` and a ``write_config`` helper that writes
a loadable ``config/`` tree (``tests.support.config_tree.write_full_config``) with
``security.ui.roles``. It removes the offline environment variables the socket guard sets and
undoes ``configure_logging`` and the bound ports after the test.
"""

from __future__ import annotations

import getpass
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import egress_socket
from herness.core.logging import reset_logging
from herness.core.resilience import ProcessState

USER_REF_KEY = "cli-unit-test-key-not-secret"  # pragma: allowlist secret


@dataclass
class CliEnv:
    """The test's working directory, OS user name and config directory."""

    root: Path
    user: str = "alice"
    config_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.config_dir = self.root / "config"

    def write_config(
        self,
        *,
        admins: Sequence[str] = (),
        reviewers: Sequence[str] = (),
        default_role: str = "viewer",
    ) -> Path:
        """Write the config tree with ``security.ui.roles``; return the config directory."""
        cfg = write_full_config(self.root)
        path = cfg / "herness.yaml"
        roles = (
            f"  ui: {{roles: {{admins: [{', '.join(admins)}], "
            f"reviewers: [{', '.join(reviewers)}], default_role: {default_role}}}}}\n"
        )
        text = path.read_text(encoding="utf-8").replace("security:\n", "security:\n" + roles, 1)
        path.write_text(text, encoding="utf-8")
        return cfg


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
) -> Iterator[CliEnv]:
    """Isolated CLI environment; see the module docstring."""
    del reset_process_state  # requested for its reset of the bound ports
    for name in egress_socket._ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HERNESS_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    fake_keyring.store[("herness", "ui_user_ref_key")] = USER_REF_KEY
    env = CliEnv(tmp_path)
    monkeypatch.setattr(getpass, "getuser", lambda: env.user)
    try:
        yield env
    finally:
        reset_logging()
