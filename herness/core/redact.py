"""Redaction: value types, the Redactor and the process-wide instance (U10-35 to U10-48).

``EntityType`` and ``DETECTION_ORDER`` are declared in ``redact_patterns`` (which this
module imports, spec §2 import order) and re-exported here as the public names. Importing
this module registers ``reset_redactor`` as a config reset hook and the key-id provider
that ``config_hash`` uses (U10-11).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import re
import sys
import threading
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from herness.core import config as _config
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError, FatalError, StoreBusy
from herness.core.logging import get_logger
from herness.core.redact_directory import NameDirectory
from herness.core.redact_patterns import (
    DETECTION_ORDER,
    TOKEN_PATTERN,
    Detector,
    EntityType,
    build_detectors,
    normalize_value,
)
from herness.core.secrets import resolve
from herness.core.settings import RedactionConfig

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = ["DETECTION_ORDER", "EntityType", "RedactionFailed", "RedactionResult", "Redactor"]
__all__ += ["Span", "get_redactor", "redact_table", "redact_text", "reset_redactor"]

MAX_TEXT_CHARS: Final = 4_000_000
_KEY_BYTES: Final = 32
_KEY_HEX: Final = re.compile(r"[0-9A-Fa-f]{64}")
_SECRET: Final = "[SECRET]"  # noqa: S105 - the replacement token, not a secret
_MASK: Final = "\x00"  # blanks held ranges in the detector view
_SECRET_TYPES: Final = frozenset({"CREDENTIAL", "URL_TOKEN"})
_log = get_logger("core.redact")


@dataclass(frozen=True, order=True)
class Span:
    """One detected span ``[start, end)`` with its replacement; orders by ``start`` first."""

    start: int
    end: int
    type: EntityType
    replacement: str


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text and per-type counts; ``counts`` holds only types with count >= 1."""

    text: str
    counts: dict[EntityType, int]


class RedactionFailed(FatalError):  # noqa: N818 - name fixed by impl 10 U10-48
    """Redaction of one text failed; the message names the entity type or ``text too long``."""


class _Taken:
    """Sorted, non-overlapping ``[start, end)`` ranges with an O(log n) overlap test."""

    __slots__ = ("_ends", "_starts")

    def __init__(self, ranges: Iterable[tuple[int, int]]) -> None:
        ordered = sorted(ranges)
        self._starts = [start for start, _ in ordered]
        self._ends = [end for _, end in ordered]

    def claim(self, start: int, end: int) -> bool:
        """Add ``[start, end)`` and return True when it overlaps no range held."""
        index = bisect_right(self._starts, start)
        if index and self._ends[index - 1] > start:
            return False
        if index < len(self._starts) and self._starts[index] < end:
            return False
        self._starts.insert(index, start)
        self._ends.insert(index, end)
        return True

    def masked(self, text: str) -> str:
        """``text`` with every held range blanked by NUL: same positions, matches no detector."""
        parts: list[str] = []
        pos = 0
        for start, end in zip(self._starts, self._ends, strict=True):
            parts += (text[pos:start], _MASK * (end - start))
            pos = end
        parts.append(text[pos:])
        return "".join(parts)


_FAILED_LOCK: Final = threading.Lock()
_failed = [0]  # herness_redact_records_total{result="failed"}; metric-sink wiring deferred


def _records_failed_total() -> int:
    """Records that ``redact_batch`` turned into ``None`` in this process."""
    with _FAILED_LOCK:
        return _failed[0]


class Redactor:
    """Detect and replace PII and secrets; immutable after construction (U10-40)."""

    __slots__ = ("_cfg", "_detectors", "_directory", "_key", "_ner", "_ner_lock", "key_id")

    def __init__(
        self, cfg: RedactionConfig, hmac_key: bytes, directory: NameDirectory | None = None
    ) -> None:
        if len(hmac_key) != _KEY_BYTES:
            msg = "redaction key must be 32 bytes"
            raise ConfigError(msg)
        self._cfg = cfg
        self._key = bytes(hmac_key)
        self.key_id: str = hashlib.sha256(self._key).hexdigest()[:8]
        names = NameDirectory.from_files(None, (), None) if directory is None else directory
        self._directory = names
        person = Detector(
            "PERSON", lambda _text: names.variant_count > 0, lambda text: iter(names.find(text))
        )
        by_type = {detector.type: detector for detector in build_detectors(cfg)}
        by_type["PERSON"] = person
        self._detectors = tuple(by_type[kind] for kind in DETECTION_ORDER)
        self._ner: Any = None
        self._ner_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Redactor(key_id={self.key_id!r})"

    def pseudonym(self, type: EntityType, value: str) -> str:  # noqa: A002 - spec signature
        """Stable ``[<TYPE>_<10 hex>]`` token: HMAC-SHA256 of the normalized value (U10-44)."""
        message = (type + ":" + normalize_value(type, value)).encode("utf-8")
        digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()[:10]
        return "[" + type + "_" + digest + "]"

    def _replacement(self, kind: EntityType, value: str) -> str:
        return _SECRET if kind in _SECRET_TYPES else self.pseudonym(kind, value)

    def _detect(self, detector: Detector, text: str, taken: _Taken) -> list[Span]:
        """Accepted spans of one detector; any exception becomes ``RedactionFailed(<type>)``."""
        try:
            if not detector.prefilter(text):
                return []
            return [
                Span(start, end, detector.type, self._replacement(detector.type, value))
                for start, end, value in detector.find(text)
                if taken.claim(start, end)
            ]
        except Exception as exc:
            raise RedactionFailed(detector.type) from exc

    def scan(self, text: str, *, ner: bool = False) -> list[Span]:
        """Sorted, non-overlapping spans that never cover an existing token (U10-41)."""
        if len(text) > MAX_TEXT_CHARS:
            msg = "text too long"
            raise RedactionFailed(msg)
        protected = [match.span() for match in TOKEN_PATTERN.finditer(text)]
        taken = _Taken(protected)
        spans: list[Span] = []
        view = taken.masked(text)  # a detector cannot swallow a token or accepted neighbour
        for detector in self._detectors:
            found = self._detect(detector, view, taken)
            if found:
                spans.extend(found)
                view = taken.masked(text)
        if ner and self._cfg.ner == "presidio":
            spans.extend(self._ner_spans(text, protected, spans, taken))
        spans.sort()
        return spans

    def _analyzer(self) -> Any:  # noqa: ANN401 - presidio is an optional, untyped extra
        with self._ner_lock:
            if self._ner is None:
                try:
                    module = importlib.import_module("presidio_analyzer")
                except ImportError as exc:
                    msg = "install the ner extra"
                    raise ConfigError(msg) from exc
                self._ner = module.AnalyzerEngine()
            return self._ner

    def _ner_spans(
        self, text: str, protected: list[tuple[int, int]], spans: list[Span], taken: _Taken
    ) -> list[Span]:
        """Presidio ``PERSON`` hits on the text with tokens and accepted spans blanked."""
        analyzer = self._analyzer()
        chars = list(text)
        for start, end in [*protected, *((span.start, span.end) for span in spans)]:
            chars[start:end] = " " * (end - start)
        try:
            hits = analyzer.analyze(text="".join(chars), entities=["PERSON"], language="en")
            found = [(int(hit.start), int(hit.end)) for hit in hits]
        except Exception as exc:
            raise RedactionFailed("PERSON") from exc  # noqa: EM101 - entity type is the message
        return [
            Span(start, end, "PERSON", self.pseudonym("PERSON", text[start:end]))
            for start, end in found
            if 0 <= start < end <= len(text) and taken.claim(start, end)
        ]

    def redact(self, text: str | None) -> RedactionResult | None:
        """Replace detected spans left to right and count them per type (U10-42)."""
        if text is None:
            return None
        spans = self.scan(text)
        parts: list[str] = []
        counts: dict[EntityType, int] = {}
        pos = 0
        for span in spans:
            if span.type == "IP" and not self._cfg.mask_ip:
                continue
            parts += (text[pos : span.start], span.replacement)
            pos = span.end
            counts[span.type] = counts.get(span.type, 0) + 1
        parts.append(text[pos:])
        return RedactionResult("".join(parts), counts)

    def redact_batch(self, texts: Sequence[str | None]) -> list[str | None]:
        """Redact in order; a failing item becomes ``None`` and is counted (U10-43)."""
        out: list[str | None] = []
        for item in texts:
            try:
                result = self.redact(item)
            except RedactionFailed:
                with _FAILED_LOCK:
                    _failed[0] += 1
                result = None
            out.append(None if result is None else result.text)
        return out


# --- U10-45 process-wide instance, U10-46 redact_text ------------------------------------------


class _State:
    redactor: Redactor | None = None


_RED_LOCK: Final = threading.Lock()


def _load_key(ref: str) -> bytes:
    """The 32-byte HMAC key behind ``ref``; ``ConfigError`` when missing or not 64 hex."""
    raw = resolve(ref).get_secret_value()
    if _KEY_HEX.fullmatch(raw) is None:
        msg = "redact.hmac_key must be 64 hex characters"
        raise ConfigError(msg)
    return bytes.fromhex(raw)


def _config_key_id(cfg: HernessConfig) -> str:
    """Key id for ``config_hash``; a missing secret propagates ``resolve``'s ConfigError."""
    return hashlib.sha256(_load_key(cfg.security.redaction.key)).hexdigest()[:8]


def get_redactor() -> Redactor:
    """The process-wide redactor, built from config on first use (U10-45)."""
    with _RED_LOCK:
        if _State.redactor is not None:
            return _State.redactor
        started = clock.monotonic()
        cfg = _config.get_config()
        red_cfg = cfg.security.redaction
        key = _load_key(red_cfg.key)
        directory = NameDirectory.from_files(
            red_cfg.directory_file,
            red_cfg.extra_names,
            cfg.paths.data / "cache/redact/display_names.txt",
        )
        _State.redactor = Redactor(red_cfg, key, directory)
        _log.info(
            "redact.directory.loaded",
            names=directory.size,
            variants=directory.variant_count,
            duration_ms=round((clock.monotonic() - started) * 1000),
        )
        return _State.redactor


def reset_redactor() -> None:
    """Drop the cached redactor (config reset hook, U10-10)."""
    with _RED_LOCK:
        _State.redactor = None


def redact_text(text: str | None) -> str | None:
    """Redacted ``text``; ``None`` (fail closed, logged without text) on failure (U10-46)."""
    if text is None:
        return None
    redactor = get_redactor()
    try:
        result = redactor.redact(text)
    except RedactionFailed as exc:
        # The message is the entity type or "text too long", never text (U10-48).
        _log.warning("redact.record.failed", error_type=type(exc).__name__, reason=exc.message)
        return None
    return None if result is None else result.text


_config._RESET_HOOKS.append(reset_redactor)
_config._KEY_ID_PROVIDER = _config_key_id


# --- U10-47 redact_table ---------------------------------------------------------------------


def redact_table(
    tbl: pa.Table, text_cols: Sequence[str], id_col: str = "record_id", workers: int | None = None
) -> pa.Table:
    """``(<id_col>, text)``: text columns redacted and joined per row, fail closed (U10-47)."""
    import pyarrow as pa  # noqa: PLC0415 - pyarrow loads only for table redaction

    from herness.core import _redact_pool as pool  # noqa: PLC0415 - cycle: it imports redact

    started = clock.monotonic()
    ids = pool.string_column(tbl, id_col)
    columns = [pool.string_column(tbl, name) for name in text_cols]
    count = pool.worker_count(workers)
    get_redactor()  # a missing key is a ConfigError here, never a crashed worker
    size = pool.CHUNK_ROWS

    def load(i: int) -> tuple[list[str | None], list[list[str | None]]]:
        return ids.slice(i * size, size).to_pylist(), [
            col.slice(i * size, size).to_pylist() for col in columns
        ]

    try:
        results = pool.redact_chunks(load, -(-tbl.num_rows // size), count)
    except BrokenProcessPool as exc:
        _log.error("redact.table.failed", error_type=type(exc).__name__)
        msg = "redaction worker crashed"
        raise StoreBusy(msg) from exc
    texts = [text for out, _ in results for text in out]
    failed_ids = [record_id for _, failed in results for record_id in failed]
    for record_id in failed_ids:
        _log.warning("redact.record.failed", record_id=record_id, error_type="RedactionFailed")
    failed_rows = len(failed_ids)
    # T08-05: herness_redact_records_total{result="ok"|"failed"} += rows - failed, failed_rows
    _log.info(
        "redact.table.completed",
        rows=tbl.num_rows,
        failed_rows=failed_rows,
        workers=count,
        duration_ms=round((clock.monotonic() - started) * 1000),
    )
    text_array = pa.array(texts, type=pa.string())
    return pa.Table.from_arrays([ids.cast(pa.string()), text_array], names=[id_col, "text"])


if __name__ == "__main__":
    from herness.core import redact_scan

    sys.exit(redact_scan.main())
