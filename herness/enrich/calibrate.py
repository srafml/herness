"""Temperature scaling, ECE, cross-fit and calibration files (U03-42 ... U03-47; design 03 §5.6).

Calibration is the input of the acceptance gate (TH03-17): a question with fewer than 100 gold
rows, an empty fold or a failed fit is ``uncalibrated`` and keeps T = 1.0.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scipy import optimize  # type: ignore[import-untyped]  # scipy ships no type information

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.ids import canonical_json
from herness.core.types import QuestionType
from herness.enrich.layout import EnrichPaths

_T_BOUNDS: Final = (0.05, 10.0)
_MIN_ROWS: Final = 100
_MAX_FILE_BYTES: Final = 1_048_576  # 1 MB
_LAYA_FILE: Final = "calibration.json"
_TABLE_SCHEMA: Final = pa.schema(
    [
        ("decider", pa.string()),
        ("decider_version", pa.string()),
        ("question", pa.string()),
        ("t", pa.float64()),
        ("uncalibrated", pa.bool_()),
    ]
)


def apply_temperature(probs: np.ndarray, t: float, qtype: QuestionType) -> np.ndarray:
    """Calibrate ``probs`` (n, K) with temperature ``t`` (U03-42); rows sum to 1, argmax kept."""
    if not math.isfinite(t) or t <= 0:
        msg = "calibration temperature must be finite and > 0"
        raise ConfigError(msg)
    arr = np.asarray(probs, dtype=np.float64)
    if arr.ndim != 2 or (qtype == "bool" and arr.shape[1] != 2):  # noqa: PLR2004
        msg = f"calibration input must be an (n, K) matrix, (n, 2) for bool; got {arr.shape}"
        raise ConfigError(msg)
    if qtype == "bool":
        p = np.clip(arr[:, 0], 1e-9, 1 - 1e-9)
        calibrated = 1.0 / (1.0 + np.exp(-(np.log(p) - np.log1p(-p)) / t))
        return np.stack([calibrated, 1.0 - calibrated], axis=1)
    z = np.log(arr + 1e-9) / t
    e = np.exp(z - z.max(axis=1, keepdims=True))
    out: np.ndarray = e / e.sum(axis=1, keepdims=True)
    return out


def _fit(probs: np.ndarray, labels: np.ndarray, qtype: QuestionType) -> float | None:
    """Minimize the NLL over T in [0.05, 10]; None when the optimizer reports failure."""
    rows = np.arange(len(labels))
    idx = np.asarray(labels, dtype=np.int64)

    def nll(t: float) -> float:
        return float(-np.mean(np.log(apply_temperature(probs, t, qtype)[rows, idx] + 1e-12)))

    res = optimize.minimize_scalar(
        nll, bounds=_T_BOUNDS, method="bounded", options={"xatol": 1e-4, "maxiter": 500}
    )
    if not res.success:
        return None
    return float(min(max(float(res.x), _T_BOUNDS[0]), _T_BOUNDS[1]))


def fit_temperature(probs: np.ndarray, labels: np.ndarray, qtype: QuestionType) -> float:
    """Fit T by minimizing NLL (U03-43); returns 1.0 when the optimizer fails."""
    t = _fit(probs, labels, qtype)
    return 1.0 if t is None else t


def ece(probs: np.ndarray, labels: np.ndarray, n_bins: int = 15) -> float:
    """Top-label expected calibration error over equal-mass bins (U03-44)."""
    arr = np.asarray(probs, dtype=np.float64)
    conf, pred = arr.max(axis=1), arr.argmax(axis=1)  # argmax ties -> lowest index
    correct = (pred == np.asarray(labels)).astype(np.float64)
    order = np.argsort(conf, kind="stable")
    n = len(conf)
    total = 0.0
    for chunk in np.array_split(order, min(n_bins, n)):
        total += len(chunk) / n * abs(float(correct[chunk].mean()) - float(conf[chunk].mean()))
    return min(max(total, 0.0), 1.0)


@dataclasses.dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Calibration of one question for one decider version (U03-45)."""

    temperature: float
    ece: float  # cross-fit mean, after T
    ece_raw: float  # T = 1, all rows
    accuracy: float  # top-label accuracy on all rows
    n: int
    uncalibrated: bool

    def __post_init__(self) -> None:
        if self.uncalibrated and self.temperature != 1.0:
            msg = "an uncalibrated result must have temperature 1.0"
            raise ConfigError(msg)


def cross_fit(
    probs: np.ndarray, labels: np.ndarray, folds: np.ndarray, qtype: QuestionType
) -> CalibrationResult:
    """2-fold cross-fitted calibration on gold (U03-46; design 03 §5.6)."""
    probs, labels, folds = np.asarray(probs), np.asarray(labels), np.asarray(folds)
    n = len(labels)
    raw = ece(probs, labels)
    accuracy = float(np.mean(probs.argmax(axis=1) == labels))
    uncalibrated = CalibrationResult(1.0, raw, raw, accuracy, n, uncalibrated=True)
    in0, in1 = folds == 0, folds == 1
    if n < _MIN_ROWS or not in0.any() or not in1.any():
        return uncalibrated
    t0 = _fit(probs[in0], labels[in0], qtype)
    t1 = _fit(probs[in1], labels[in1], qtype)
    t = _fit(probs, labels, qtype)
    if t0 is None or t1 is None or t is None:
        return uncalibrated
    e1 = ece(apply_temperature(probs[in1], t0, qtype), labels[in1])
    e0 = ece(apply_temperature(probs[in0], t1, qtype), labels[in0])
    return CalibrationResult(t, (e0 + e1) / 2, raw, accuracy, n, uncalibrated=False)


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    temperature: float = Field(ge=_T_BOUNDS[0], le=_T_BOUNDS[1])
    ece: float = Field(ge=0, le=1)
    ece_raw: float = Field(ge=0, le=1)
    accuracy: float = Field(ge=0, le=1)
    n: int = Field(ge=0)
    uncalibrated: bool


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decider: str
    decider_version: str
    question_set_version: str
    fitted_at: str
    questions: dict[str, _Entry]


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to a temp file next to ``path``, fsync it and ``os.replace`` it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class CalibrationStore:
    """Read and write per-question temperatures (U03-47; impl 03 §4.3 file layouts).

    Single writer; readers tolerate the atomic replace. Reads are memoized per path and mtime.
    """

    def __init__(self, paths: EnrichPaths) -> None:
        self._paths = paths
        self._memo: dict[Path, tuple[int, _File]] = {}

    def _path(self, decider: str, decider_version: str, qsv: str) -> Path:
        if decider == "laya":
            return self._paths.laya_dir(decider_version) / _LAYA_FILE
        return self._paths.calibration_file(decider, decider_version, qsv)

    def _read(self, path: Path) -> _File | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        memo = self._memo.get(path)
        if memo is not None and memo[0] == stat.st_mtime_ns:
            return memo[1]
        if stat.st_size > _MAX_FILE_BYTES:
            msg = f"calibration file {path.name} is larger than 1 MB"
            raise ConfigError(msg, path=str(path))
        try:
            doc = _File.model_validate(json.loads(path.read_bytes()))
        except (ValueError, ValidationError) as exc:
            msg = f"calibration file {path.name} is malformed"
            raise ConfigError(msg, path=str(path)) from exc
        self._memo[path] = (stat.st_mtime_ns, doc)
        return doc

    def _results(self, doc: _File, path: Path) -> dict[str, CalibrationResult]:
        try:
            return {qid: CalibrationResult(**e.model_dump()) for qid, e in doc.questions.items()}
        except ConfigError as exc:
            msg = f"calibration file {path.name} is malformed: {exc}"
            raise ConfigError(msg, path=str(path)) from exc

    def load(self, decider: str, decider_version: str, qsv: str) -> dict[str, CalibrationResult]:
        """Entries of the file for (decider, version, qsv); ``{}`` when there is none."""
        path = self._path(decider, decider_version, qsv)
        doc = self._read(path)
        if doc is None:
            return {}
        if doc.question_set_version != qsv and decider == "laya":
            return {}  # a Laya file fitted on another question set counts as missing
        if (doc.decider, doc.decider_version, doc.question_set_version) != (
            decider,
            decider_version,
            qsv,
        ):
            msg = f"calibration file {path.name} names another decider, version or qsv"
            raise ConfigError(msg, path=str(path))
        return self._results(doc, path)

    def temperature(
        self, decider: str, decider_version: str, qsv: str, qid: str
    ) -> tuple[float, bool]:
        """(T, uncalibrated) for one question; ``(1.0, True)`` without a file or entry."""
        result = self.load(decider, decider_version, qsv).get(qid)
        return (1.0, True) if result is None else (result.temperature, result.uncalibrated)

    def save(
        self,
        decider: str,
        decider_version: str,
        qsv: str,
        results: Mapping[str, CalibrationResult],
    ) -> Path:
        """Merge ``results`` into the file and write it atomically; returns its path."""
        path = self._path(decider, decider_version, qsv)
        merged = {**self.load(decider, decider_version, qsv), **results}
        doc = {
            "decider": decider,
            "decider_version": decider_version,
            "question_set_version": qsv,
            "fitted_at": clock.format_utc(clock.now()),
            "questions": {qid: dataclasses.asdict(r) for qid, r in sorted(merged.items())},
        }
        _write_atomic(path, canonical_json(doc) + "\n")
        self._memo.pop(path, None)
        return path

    def as_table(self, entries: Sequence[tuple[str, str]], qsv: str) -> pa.Table:
        """One row per (decider, version) x question: decider, decider_version, question, t,
        uncalibrated."""
        rows = [
            {
                "decider": decider,
                "decider_version": version,
                "question": qid,
                "t": result.temperature,
                "uncalibrated": result.uncalibrated,
            }
            for decider, version in entries
            for qid, result in sorted(self.load(decider, version, qsv).items())
        ]
        return pa.Table.from_pylist(rows, schema=_TABLE_SCHEMA)
