"""Aho-Corasick person-name directory and the display-names cache (U10-38, U10-39).

``NameDirectory`` is the ``PERSON`` matcher of design 10 §5.3: it holds lower-cased name
variants (full names, ``Last, First`` and ``last,first``, plus single tokens explicitly
listed in ``extra_names``) mapped to a canonical name, built once from the directory CSV,
extra names and the display-names cache, then queried read-only. The directory is personal
data (ACL-protected, §7(f)); nothing here logs its content, only a missing-file warning.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

import ahocorasick

from herness.core.errors import ConfigError, StoreBusy
from herness.core.logging import get_logger
from herness.core.redact_patterns import normalize_value

__all__ = ["NameDirectory", "update_display_names"]

_logger = get_logger("core.redact_directory")

MAX_DIRECTORY_BYTES: Final = 200 * 1024 * 1024
_MIN_DISPLAY_TOKENS, _MAX_DISPLAY_TOKENS = 2, 8
_MIN_DISPLAY_CHARS, _MAX_DISPLAY_CHARS = 3, 128
_DISPLAY_NAMES_REL: Final = Path("cache") / "redact" / "display_names.txt"


def _read_csv_rows(path: Path) -> list[dict[str, str | None]]:
    """Rows of the directory CSV; a missing file warns and yields none (U10-38 step 1)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_DIRECTORY_BYTES + 1)
    except FileNotFoundError:
        _logger.warning("redact.directory.missing")
        return []
    except OSError as exc:
        msg = "cannot read directory_file"
        raise ConfigError(msg) from exc
    if len(data) > MAX_DIRECTORY_BYTES:
        msg = "cannot read directory_file"
        raise ConfigError(msg)
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _read_lines(path: Path | None) -> list[str]:
    """Non-blank stripped lines of ``path``; ``None`` or absent yields none."""
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        msg = "cannot read directory_file"
        raise ConfigError(msg) from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def _add_display_entry(automaton: ahocorasick.Automaton, name: str) -> str | None:
    """Add the 3 variants of a >= 2 token display name; return its canonical (step 2)."""
    tokens = name.split()
    if len(tokens) < _MIN_DISPLAY_TOKENS:
        return None
    canonical = normalize_value("PERSON", name)
    last = tokens[-1]
    first = " ".join(tokens[:-1])
    for key in (name.lower(), f"{last}, {first}".lower(), f"{last},{first}".lower()):
        automaton.add_word(key, (len(key), canonical))
    return canonical


def _lower_with_map(text: str) -> tuple[str, list[int]]:
    """Per-character lower-case of ``text`` with an index back to the source character."""
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        lowered = ch.lower()
        chars.append(lowered)
        index_map.extend([i] * len(lowered))
    return "".join(chars), index_map


def _resolve_overlaps(
    candidates: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Keep non-overlapping matches, longest first then leftmost (U10-38 find step 4)."""
    ordered = sorted(candidates, key=lambda c: (-(c[1] - c[0]), c[0]))
    accepted: list[tuple[int, int, str]] = []
    for start, end, canonical in ordered:
        if all(end <= s or start >= e for s, e, _ in accepted):
            accepted.append((start, end, canonical))
    accepted.sort()
    return accepted


class NameDirectory:
    """Immutable ``PERSON`` matcher over directory, alt and extra names (U10-38)."""

    __slots__ = ("_automaton", "size", "variant_count")

    def __init__(self, automaton: ahocorasick.Automaton, size: int, variant_count: int) -> None:
        self._automaton = automaton
        self.size = size
        self.variant_count = variant_count

    @classmethod
    def from_files(
        cls,
        directory_file: Path | None,
        extra_names: Sequence[str],
        display_names_file: Path | None,
    ) -> NameDirectory:
        """Build the automaton from the directory CSV, extra names and display names."""
        automaton = ahocorasick.Automaton()
        rows = _read_csv_rows(directory_file) if directory_file is not None else []
        for row in rows:
            name = (row.get("display_name") or "").strip()
            canonical = _add_display_entry(automaton, name)
            if canonical is None:
                continue
            for alt in (row.get("alt_names") or "").split(";"):
                alt = alt.strip()  # noqa: PLW2901 - reused as the cleaned value
                if alt:
                    automaton.add_word(alt.lower(), (len(alt), canonical))
        for extra in extra_names:
            extra = extra.strip()  # noqa: PLW2901 - reused as the cleaned value
            if extra:
                automaton.add_word(extra.lower(), (len(extra), normalize_value("PERSON", extra)))
        for line in _read_lines(display_names_file):
            _add_display_entry(automaton, line)
        automaton.make_automaton()
        size = len({canonical for _, canonical in automaton.values()})
        return cls(automaton, size, len(automaton))

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """Word-bounded matches ``(start, end, canonical_name)``, longest-then-leftmost."""
        if self.variant_count == 0:  # an empty trie is never converted by make_automaton()
            return []
        low = text.lower()
        index_map: list[int] | None = None
        if len(low) != len(text):
            low, index_map = _lower_with_map(text)
        candidates: list[tuple[int, int, str]] = []
        for end_idx, (key_len, canonical) in self._automaton.iter(low):
            start_idx = end_idx - key_len + 1
            if index_map is None:
                start, end = start_idx, end_idx + 1
            else:
                start, end = index_map[start_idx], index_map[end_idx] + 1
            before_ok = start == 0 or not text[start - 1].isalnum()
            after_ok = end == len(text) or not text[end].isalnum()
            if before_ok and after_ok:
                candidates.append((start, end, canonical))
        return _resolve_overlaps(candidates)


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by replace, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".display_names-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def update_display_names(names: Iterable[str], *, data_dir: Path) -> int:
    """Add ``names`` to the redaction display-names cache; return the count added (U10-39)."""
    path = data_dir / _DISPLAY_NAMES_REL
    try:
        try:
            existing = set(path.read_text(encoding="utf-8-sig").splitlines())
        except FileNotFoundError:
            existing = set()
        before = len(existing)
        for name in names:
            cleaned = name.strip()
            tokens = cleaned.split()
            if (
                _MIN_DISPLAY_TOKENS <= len(tokens) <= _MAX_DISPLAY_TOKENS
                and _MIN_DISPLAY_CHARS <= len(cleaned) <= _MAX_DISPLAY_CHARS
            ):
                existing.add(cleaned)
        added = len(existing) - before
        if added:
            _write_atomic(path, "".join(name + "\n" for name in sorted(existing)))
    except OSError as exc:
        msg = "display_names write failed"
        raise StoreBusy(msg) from exc
    return added
