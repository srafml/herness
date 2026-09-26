"""Secret references, the keyring and dotenv backends, resolution (impl 10 U10-27 to U10-34).

Config holds ``secret:<name>`` references only (R-72); values are resolved at the point of use
and remembered in ``_KNOWN`` so the log scrubber and the audit field check can find them.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from pydantic import AfterValidator, SecretStr

from herness.core import config as _config
from herness.core.audit import audit
from herness.core.config import HernessConfig, effective_dict
from herness.core.config_sources import _DOTENV_LINE, _MAX_DOTENV_BYTES, _read_text
from herness.core.errors import ConfigError
from herness.core.logging import get_logger

__all__ = ["SECRET_NAME", "SecretNameStr", "SecretRef", "SecretRefStr", "delete_secret"]
__all__ += ["exists", "known_values", "referenced_secret_names", "resolve", "resolve_json"]
__all__ += ["set_secret"]

SECRET_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
_PREFIX: Final = "secret:"
_SERVICE: Final = "herness"
_CHUNK: Final = 1200  # Credential Manager blob limit 2,560 bytes as UTF-16 (U10-33)
_HEADER: Final = "chunked:v1:"
_HEADER_RE: Final = re.compile(r"chunked:v1:([1-9][0-9]{0,2})")
_MIN_VALUE, _MAX_VALUE = 8, 16_384
_BAD_CHARS: Final = frozenset("\r\n\x00")
_HINT: Final = "run as the account that owns the credential"
_READ_ONLY: Final = "dotenv backend is read-only"
_ALWAYS_REFERENCED: Final = frozenset({"redact.hmac_key", "ui_user_ref_key"})
_log = get_logger("core.secrets")

_KNOWN: set[str] = set()  # secret values resolved by this process (U10-32, X-1)
_KNOWN_LOCK: Final = threading.Lock()


class SecretRef(str):
    """A secret reference; the string value is the lower-cased bare name (U10-27)."""

    __slots__ = ()

    @classmethod
    def parse(cls, value: str) -> SecretRef:
        """Accept ``secret:<name>`` or a bare name; ``ConfigError`` without echoing the value."""
        if isinstance(value, SecretRef):
            return value
        bare = value.removeprefix(_PREFIX)
        if SECRET_NAME.fullmatch(bare) is None:
            msg = "invalid secret name"
            raise ConfigError(msg)
        return cls(bare.lower())

    @property
    def name(self) -> str:
        """The lower-cased secret name."""
        return str(self)


def _checker(prefix: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.startswith(prefix) or SECRET_NAME.fullmatch(value[len(prefix) :]) is None:
            msg = f"must be {prefix}<name>" if prefix else "invalid secret name"
            raise ValueError(msg)
        return value

    return check


SecretRefStr = Annotated[str, AfterValidator(_checker(_PREFIX))]
SecretNameStr = Annotated[str, AfterValidator(_checker(""))]


def known_values() -> frozenset[str]:
    """Snapshot of the secret values resolved by this process (U10-32)."""
    with _KNOWN_LOCK:
        return frozenset(_KNOWN)


def _remember(*values: str) -> None:
    with _KNOWN_LOCK:
        _KNOWN.update(v for v in values if v)


class _SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


def _keyring_call(fn: Callable[..., Any], *args: str) -> Any:  # noqa: ANN401 - keyring API
    """Call a ``keyring`` function; backend failures become ``ConfigError`` with the hint."""
    try:
        return fn(_SERVICE, *args)
    except PasswordDeleteError:
        return None  # the entry is already gone: deleting a missing name is a no-op
    except (KeyringError, RuntimeError) as exc:
        _log.error("secrets.backend.unavailable", backend="keyring", error_type=type(exc).__name__)
        msg = "secret backend unavailable: keyring"
        raise ConfigError(msg, hint=_HINT) from None


class _KeyringBackend:
    """Windows Credential Manager via ``keyring``; long values are chunked (U10-33)."""

    @staticmethod
    def _entry(user: str) -> tuple[str | None, int]:
        """The stored entry and its chunk count (0 when stored directly or absent)."""
        raw: str | None = _keyring_call(keyring.get_password, user)
        match = _HEADER_RE.fullmatch(raw) if raw is not None else None
        return raw, int(match.group(1)) if match else 0

    @staticmethod
    def _drop(user: str, keep: int, old: int) -> None:
        for i in range(keep + 1, old + 1):
            _keyring_call(keyring.delete_password, f"{user}#{i}")

    def get(self, name: str) -> str | None:
        user = name.lower()
        raw, count = self._entry(user)
        if not count:
            return raw
        parts = [_keyring_call(keyring.get_password, f"{user}#{i}") for i in range(1, count + 1)]
        return None if None in parts else "".join(parts)  # a lost chunk reads as not found

    def set(self, name: str, value: str) -> None:
        user, chunked = name.lower(), len(value) > _CHUNK or value.startswith(_HEADER)
        old = self._entry(user)[1]
        parts = [value[i : i + _CHUNK] for i in range(0, len(value), _CHUNK)] if chunked else []
        for i, part in enumerate(parts, start=1):
            _keyring_call(keyring.set_password, f"{user}#{i}", part)
        _keyring_call(keyring.set_password, user, f"{_HEADER}{len(parts)}" if parts else value)
        self._drop(user, len(parts), old)  # stale chunks of a longer previous value

    def delete(self, name: str) -> None:
        user = name.lower()
        self._drop(user, 0, self._entry(user)[1])
        _keyring_call(keyring.delete_password, user)


@functools.lru_cache(maxsize=4)
def _dotenv_values(path: Path) -> Mapping[str, str]:
    """``.env`` variables by upper-cased name, U10-18 grammar; read once per path."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for number, raw in enumerate(_read_text(path, _MAX_DOTENV_BYTES).splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE.fullmatch(line)
        if match is None:
            msg = f".env line {number}: malformed"
            raise ConfigError(msg)
        quoted, plain = match.group(2, 3)
        out[match.group(1).upper()] = plain if quoted is None else quoted
    return out


class _DotenvBackend:
    """Read-only ``HERNESS_SECRET__*`` variables of ``<repo root>/.env``, dev and synth only."""

    def __init__(
        self, profile: str, *, env: Mapping[str, str] | None = None, root: Path | None = None
    ) -> None:
        env = os.environ if env is None else env
        if env.get("HERNESS_ENV") != "dev" and profile != "synth":
            msg = "dotenv secrets backend is refused outside dev and synth"
            raise ConfigError(msg)
        self._values = _dotenv_values(((root or Path.cwd()) / ".env").resolve())

    def get(self, name: str) -> str | None:
        key = "HERNESS_SECRET__" + name.replace(".", "_").replace("-", "_").upper()
        return self._values.get(key) or None  # an empty value (``.env.example``) is unset

    def set(self, name: str, value: str) -> None:
        raise ConfigError(_READ_ONLY)

    def delete(self, name: str) -> None:
        raise ConfigError(_READ_ONLY)


def _backend() -> _SecretBackend:
    """The configured backend; ``keyring`` while no config is cached (no load is triggered)."""
    cfg = _config._Cache.config
    if cfg is None or cfg.security.secrets.backend == "keyring":
        return _KeyringBackend()
    return _DotenvBackend(cfg.profile)


def _reset() -> None:
    with _KNOWN_LOCK:
        _KNOWN.clear()
    _dotenv_values.cache_clear()


_config._RESET_HOOKS.append(_reset)


def resolve(ref: SecretRef | str) -> SecretStr:
    """Resolve a secret value at the point of use; the value joins ``known_values`` (U10-28)."""
    name = SecretRef.parse(ref).name
    value = _backend().get(name)
    if value is None:
        msg = f"secret not found: {name}"
        raise ConfigError(msg)
    _remember(value)
    return SecretStr(value)


def resolve_json(ref: SecretRef | str) -> dict[str, SecretStr]:
    """Resolve a compound credential stored as a JSON object of strings (U10-29)."""
    name = SecretRef.parse(ref).name
    raw = resolve(name).get_secret_value()
    msg = f"secret {name} is not a JSON object of strings"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError(msg) from None
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ConfigError(msg)
    _remember(*data.values())
    return {key: SecretStr(value) for key, value in data.items()}


def exists(name: str) -> bool:
    """Presence check; the value is discarded and not remembered (U10-30)."""
    return _backend().get(SecretRef.parse(name).name) is not None


def _writable() -> _SecretBackend:
    backend = _backend()
    if isinstance(backend, _DotenvBackend):
        raise ConfigError(_READ_ONLY)
    return backend


def set_secret(name: str, value: str, *, actor: str) -> None:
    """Store a secret: validate, audit ``secret_set`` by name, then write (U10-31)."""
    ref = SecretRef.parse(name)
    if not _MIN_VALUE <= len(value) <= _MAX_VALUE or not _BAD_CHARS.isdisjoint(value):
        msg = "secret value rejected: length or characters"
        raise ConfigError(msg)
    backend = _writable()
    audit("admin_action", actor, action="secret_set", target=ref.name)
    backend.set(ref.name, value)


def delete_secret(name: str, *, actor: str) -> None:
    """Remove a secret after auditing ``secret_rotate``; a missing name is a no-op (U10-31)."""
    ref = SecretRef.parse(name)
    backend = _writable()
    audit("admin_action", actor, action="secret_rotate", target=ref.name)
    backend.delete(ref.name)


def _collect(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("enabled") is False:
            return
        for value in node.values():
            _collect(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect(value, out)
    elif isinstance(node, str) and node.startswith(_PREFIX):
        out.add(node[len(_PREFIX) :].lower())


def referenced_secret_names(cfg: HernessConfig) -> list[str]:
    """Sorted, lower-cased secret names the config references, skipping disabled subtrees."""
    names = set(_ALWAYS_REFERENCED)
    _collect(effective_dict(cfg), names)
    return sorted(names)
