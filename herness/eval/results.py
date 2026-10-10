"""`results.jsonl`: one fsynced JSON line per question result, and the resume index (U11-63).

The log is append-only: a record is written with `open(path, "a")`, `flush` and `os.fsync`,
and the file is never rewritten or truncated. A crash mid-write leaves a truncated last line;
readers ignore it, and the next append starts on a fresh line so its record stays readable.
`(id, repeat)` is the idempotency key of a write (design 11 §5.3.1, resume F11-08).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
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

    def _lines(self) -> Iterator[tuple[int, str]]:
        """`(line number, text)` of every complete, non-blank line; the unterminated tail
        (a write cut short by a crash) is never yielded."""
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            return
        for number, raw in enumerate(data.split(b"\n")[:-1], start=1):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                yield number, text

    def _records(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """Complete lines that parse as JSON objects; an earlier truncated tail, which the
        next append closed with a newline, is not JSON and is skipped."""
        for number, text in self._lines():
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield number, record

    def done_keys(self) -> set[tuple[str, int]]:
        """`(id, repeat)` of every stored result; a truncated last line is ignored."""
        keys: set[tuple[str, int]] = set()
        for _, record in self._records():
            qid, repeat = record.get("id"), record.get("repeat")
            if isinstance(qid, str) and isinstance(repeat, int):
                keys.add((qid, repeat))
        return keys

    def read_all(self) -> list[QuestionResult]:
        """Every stored result in file order; a complete line that is not a valid
        `QuestionResult` is corruption (`ConfigError`), a truncated tail is skipped."""
        out: list[QuestionResult] = []
        for number, record in self._records():
            try:
                out.append(QuestionResult.model_validate(record))
            except ValidationError as exc:
                msg = f"results line {number} is not a valid result: path={self._path.name}"
                raise ConfigError(msg) from exc
        return out

    def append(self, r: QuestionResult) -> None:
        """Append `r` as one fsynced line; an existing `(id, repeat)` → `ConfigError`."""
        line = r.model_dump_json().encode("utf-8") + b"\n"
        key = (r.id, r.repeat)
        with self._lock:
            if self._keys is None:
                self._keys = self.done_keys()
            if key in self._keys:
                msg = f"result already recorded: id={r.id} repeat={r.repeat}"
                raise ConfigError(msg)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as fh:
                if fh.tell() > 0 and not self._ends_with_newline():
                    line = b"\n" + line  # close a truncated tail; the record starts fresh
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._keys.add(key)

    def _ends_with_newline(self) -> bool:
        with self._path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
