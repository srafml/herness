"""`results.jsonl`: one fsynced JSON line per question result, and the resume index (U11-63).

The log is append-only: a record is written with `open(path, "a")`, `flush` and `os.fsync`,
and the file is never rewritten or truncated. A crash mid-write leaves a truncated last line;
readers ignore it, and the next append starts on a fresh line so its record stays readable.
A crash just before the newline leaves a whole record unterminated: it counts as stored.
`(id, repeat)` is the idempotency key of a write (design 11 §5.3.1, resume F11-08).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from herness.core.errors import ConfigError
from herness.core.types import NumberRef
from herness.eval.grading import GradeResult, UnsupportedReport

__all__ = ["QuestionResult", "ResultsLog"]

type ResultStatus = Literal["passed", "failed", "error", "suite_error", "skipped"]


class QuestionResult(BaseModel):
    """One graded (question, repeat), as stored in `results.jsonl` (U11-63)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    repeat: int
    status: ResultStatus
    checks: list[GradeResult]
    reference: dict[str, Any]
    final_text: str
    numbers: list[NumberRef]
    unsupported: UnsupportedReport | None
    tokens_in: int
    tokens_out: int
    cost_usd: str
    latency_s: float
    retries: int
    fallbacks: int
    repairs: int
    pipeline_run_id: str | None
    draft_mode: Literal["full", "findings_only"] | None
    trace_path: str | None
    error: str | None

    @property
    def id_key(self) -> tuple[str, int]:
        """The idempotency key `(id, repeat)`."""
        return self.id, self.repeat


class ResultsLog:
    """Append-only `results.jsonl` of one eval run (U11-63); appends are serialised.

    One writer per file (the eval job): the stored keys are read once, at the first append,
    and kept in memory, so an append does not re-read the whole file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._keys: set[tuple[str, int]] | None = None

    @property
    def path(self) -> Path:
        """The `results.jsonl` file."""
        return self._path

    def _records(self) -> list[tuple[int, dict[str, Any]]]:
        """`(line number, JSON object)` of every complete line that parses, then of the
        unterminated tail when it is a whole valid `QuestionResult` (a crash just before its
        newline: the record is stored). A tail cut short is not JSON and is ignored, also
        once a later append has closed it with a newline."""
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            return []
        *complete, tail = data.split(b"\n")
        out = [(n, rec) for n, raw in enumerate(complete, 1) if (rec := _parse(raw)) is not None]
        last = _parse(tail)
        if last is not None and _valid(last):
            out.append((len(complete) + 1, last))
        return out

    def done_keys(self) -> set[tuple[str, int]]:
        """`(id, repeat)` of every stored result; a truncated last line is ignored."""
        return {key for _, record in self._records() if (key := _key(record)) is not None}

    def read_all(self) -> list[QuestionResult]:
        """Every stored result in file order, the first record per `(id, repeat)` only; a
        complete line that is not a valid `QuestionResult` is corruption (`ConfigError`)."""
        out: list[QuestionResult] = []
        seen: set[tuple[str, int]] = set()
        for number, record in self._records():
            key = _key(record)
            if key is not None and key in seen:
                continue
            try:
                out.append(QuestionResult.model_validate(record))
            except ValidationError as exc:
                msg = f"results line {number} is not a valid result: path={self._path.name}"
                raise ConfigError(msg) from exc
            seen.add(out[-1].id_key)
        return out

    def append(self, r: QuestionResult) -> None:
        """Append `r` as one fsynced line; an existing `(id, repeat)` → `ConfigError`.

        An unterminated tail is closed with a newline first, so the record starts fresh. When
        the write fails part-way the key cache is dropped and the next append re-reads the
        file, so a retry never stores a second record for a key whose bytes landed."""
        line = r.model_dump_json().encode("utf-8") + b"\n"
        with self._lock:
            keys = self._keys if self._keys is not None else self.done_keys()
            self._keys = keys
            if r.id_key in keys:
                msg = f"result already recorded: id={r.id} repeat={r.repeat}"
                raise ConfigError(msg)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._path.open("ab") as fh:
                    if fh.tell() > 0 and not self._ends_with_newline():
                        line = b"\n" + line
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                self._keys = None
                raise
            keys.add(r.id_key)

    def _ends_with_newline(self) -> bool:
        with self._path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"


def _parse(raw: bytes) -> dict[str, Any] | None:
    """The JSON object on one line, or None (blank, cut short or not an object)."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _key(record: dict[str, Any]) -> tuple[str, int] | None:
    qid, repeat = record.get("id"), record.get("repeat")
    if isinstance(qid, str) and isinstance(repeat, int):
        return qid, repeat
    return None


def _valid(record: dict[str, Any]) -> bool:
    try:
        QuestionResult.model_validate(record)
    except ValidationError:
        return False
    return True
