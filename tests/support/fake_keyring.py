"""In-memory ``keyring`` backend fixture (impl 10 §11 ``fake_keyring``).

Tests that touch secrets use it so nothing reaches the real Windows Credential Manager.
"""

from __future__ import annotations

from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    """Dict-backed keyring; set ``error`` to make every call raise it (fault tests)."""

    priority = 1  # type: ignore[assignment]  # plain value instead of the classproperty

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}
        self.error: Exception | None = None

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    def get_password(self, service: str, username: str) -> str | None:
        self._check()
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._check()
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._check()
        if self.store.pop((service, username), None) is None:
            msg = "not found"
            raise PasswordDeleteError(msg)


@pytest.fixture
def fake_keyring() -> Iterator[MemoryKeyring]:
    """Install a fresh ``MemoryKeyring`` for the test; restore the previous backend after."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)
