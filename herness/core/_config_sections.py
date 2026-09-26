"""Composite owner sections of the root config and the memory patterns step (impl 10 U10-08).

Private sibling of ``herness.core.config`` (size-forced split, T10-03b): it holds the two
composite file models of U10-08 and the ``injection_patterns.txt`` step of U10-16 that must go
through the memory owner's parser. Like ``config.py`` it may import the ``settings.py`` module
of any package (settings exception, R-03); nothing outside ``herness.core.config`` imports it.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from herness.connectors.settings import SourcesConfig
from herness.core import config_sources as cs
from herness.enrich.settings import DecidersSettings
from herness.harness.llm.settings import ModelsConfig
from herness.harness.memory.settings import parse_injection_patterns
from herness.model.settings import BuildSettings, DqSettings

__all__ = ["PATTERNS_FILE", "ModelsFileConfig", "SourcesFileConfig", "memory_with_patterns"]

PATTERNS_FILE: Final = "injection_patterns.txt"
_MAX_PATTERNS_BYTES: Final = 1_048_576  # U10-16 step 4 bound, already enforced by the source


class SourcesFileConfig(SourcesConfig):
    """``sources.yaml``: connector sections (impl 01) plus ``dq`` and ``build`` (impl 02, R-69)."""

    dq: DqSettings = DqSettings()
    build: BuildSettings = BuildSettings()


class ModelsFileConfig(ModelsConfig):
    """``models.yaml``: ``models`` and ``harness`` (impl 05) plus ``deciders`` (impl 03, R-76).

    ``_PREFIX = None`` leaves a root-level error of this file a ``ValidationError`` so that
    ``load_config`` can name ``models.yaml`` in the ``ConfigError``.
    """

    _PREFIX: ClassVar[str | None] = None  # type: ignore[assignment]  # owner narrowed it to str
    deciders: DecidersSettings


def _file_layer(text: str) -> list[str]:
    """The list ``FilesYamlSource`` step 4 stores: stripped, non-empty, non-comment lines."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def memory_with_patterns(value: object) -> object:
    """Put the owner-parsed ``injection_patterns.txt`` into the raw ``memory`` section.

    ``FilesYamlSource`` (U10-16 step 4, frozen) stores stripped lines, which loses the trailing
    spaces U07-19 keeps and the line numbers of its errors. The file is therefore read again
    from the active load context's config dir (the source already checked it exists, is at most
    1 MiB and is UTF-8) and passed whole to ``parse_injection_patterns`` (T07-02 ruling): a bad
    pattern raises its ``ConfigError`` with the 1-based physical line number of the file. The
    parsed tuple replaces the file layer's list; a list set by a higher layer (profile, env,
    ``--set``) is left for ``MemoryConfig`` to validate.
    """
    ctx = cs.current_load_context()
    if ctx is None or not isinstance(value, dict):
        return value
    with (ctx.config_dir / PATTERNS_FILE).open("rb") as handle:
        text = handle.read(_MAX_PATTERNS_BYTES + 1).decode("utf-8-sig")
    parsed = parse_injection_patterns(text)
    section: dict[str, Any] = dict(value)
    if section.get("injection_patterns") == _file_layer(text):
        section["injection_patterns"] = parsed
    return section
