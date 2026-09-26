"""Config issues, secret-masking view and hash input (helper of ``herness.core.config``).

Split out of ``config.py`` for the ENG §2.4 size limit (impl 10 §1). Works on plain dicts and
pydantic errors only, so it imports no settings module; ``herness.core.config`` re-exports
``ConfigIssue`` and builds ``effective_dict`` and ``config_hash`` on these functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Final, Literal

from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json, sha256_hex

SECRET_KEYS: Final = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "token",
        "secret",
        "credentials",
        "client_secret",
        "private_key",
    }
)
HASH_EXCLUDED: Final = (
    ("logging",),
    ("paths",),
    ("backup",),
    ("security", "ui"),
    ("deploy", "service"),
)
_ROOT_SECTIONS: Final = frozenset({"paths", "security", "logging", "retention", "backup", "deploy"})
_MAX_SHOWN, _MAX_MESSAGE, _CHUNK = 20, 300, 1_048_576


@dataclass(frozen=True)
class ConfigIssue:
    """One validation finding (U10-14); messages hold key paths and names, never values."""

    severity: Literal["error", "warn"]
    path: str
    message: str
    file: str | None

    def __str__(self) -> str:
        return f"{self.severity} {self.path} {self.file or '-'}: {self.message}"


def _file_of(section: str, file_stems: Iterable[str]) -> str | None:
    if section in _ROOT_SECTIONS:
        return "herness.yaml"
    return f"{section}.yaml" if section in {*file_stems, "eval"} else None


def validation_error(exc: ValidationError, file_stems: Iterable[str]) -> ConfigError:
    """One ``ConfigError`` listing the errors without pydantic's input values (U10-09 step 4)."""
    stems = tuple(file_stems)
    issues: list[ConfigIssue] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = [str(part) for part in err["loc"]]
        message = err["msg"].removeprefix("Value error, ")[:_MAX_MESSAGE]
        issues.append(ConfigIssue("error", ".".join(loc), message, _file_of(loc[0], stems)))
    shown = "; ".join(f"{i.path} ({i.file or '-'}): {i.message}" for i in issues[:_MAX_SHOWN])
    msg = f"invalid config ({len(issues)} issues, first {_MAX_SHOWN} listed): {shown}"
    return ConfigError(msg, issues=issues, hint="herness config validate")


def posix_paths(python: Any, json_tree: Any) -> Any:  # noqa: ANN401 - JSON-shaped tree
    """Return ``json_tree`` with every ``Path`` leaf of the parallel ``python`` dump as POSIX text.

    ``model_dump(mode="json")`` renders paths with ``str()``, which differs between Windows
    and Linux; the hash must not (U10-11 postcondition, R-14).
    """
    if isinstance(python, PurePath):
        return python.as_posix()
    if isinstance(python, dict) and isinstance(json_tree, dict):
        pairs = zip(python.values(), json_tree.items(), strict=True)
        return {key: posix_paths(value, item) for value, (key, item) in pairs}
    if isinstance(python, list | tuple) and isinstance(json_tree, list):
        return [posix_paths(a, b) for a, b in zip(python, json_tree, strict=True)]
    return json_tree


def mask_secrets(node: Any) -> Any:  # noqa: ANN401 - JSON-shaped tree
    """Replace plain values under secret-named keys with ``***`` (U10-12 step 2)."""
    if isinstance(node, list):
        return [mask_secrets(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: "***"
        if key.lower() in SECRET_KEYS and isinstance(value, str) and not value.startswith("secret:")
        else mask_secrets(value)
        for key, value in node.items()
    }


def directory_sha256(path: Path | None) -> str | None:
    """SHA-256 hex of the name directory file, streamed in 1 MiB chunks; ``None`` if absent."""
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        msg = "cannot read directory_file"
        raise ConfigError(msg) from None
    return digest.hexdigest()


def hash_effective(data: dict[str, Any], *, key_id: str, directory: str | None) -> str:
    """``cfg_`` + 16 hex of the canonical JSON of ``data`` minus excluded subtrees (U10-11)."""
    for *parents, leaf in HASH_EXCLUDED:
        node = data
        for part in parents:
            node = node.get(part, {})
        node.pop(leaf, None)
    data["_inputs"] = {"redact_key_id": key_id, "directory_sha256": directory}
    return "cfg_" + sha256_hex(canonical_json(data))[:16]
