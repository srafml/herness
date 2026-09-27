"""Deep-mode ensemble: log-linear pooling of calibrated members (impl 03 U03-65 ... U03-67).

Design 03 §5.9. Runs no model and calls no client: it reads members' cached rows, calibrates
each with the member's T and pools them with gold-accuracy weights; the pool is raw (`T_ens` is
applied at resolve). Iteration is sorted and argmax ties go to the lowest label index.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow.dataset as ds

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json
from herness.core.types import Answer, DecisionInput, DecisionOutput, Question, QuestionSet
from herness.enrich.cache import DecisionCache, io_error
from herness.enrich.calibrate import CalibrationStore, apply_temperature
from herness.enrich.deciders.openjev import _asked
from herness.enrich.questions import question_fingerprint

__all__ = ["EnsembleDecider", "EnsembleMember", "ensemble_version", "pool_log_linear"]

type EnsembleMember = tuple[str, str]  # (decider, decider_version)

type _Dist = dict[str, float]

_EPS: Final = 1e-6
# bool column 0 is p(true), as `apply_temperature` expects
_FIXED_LABELS: Final = {"bool": ("true", "false"), "score": ("0", "1", "2", "3")}
_COLUMNS: Final = ["content_hash", "question", "question_fingerprint", "distribution",
                   "decided_at", "decider", "decider_version"]  # fmt: skip


def pool_log_linear(
    dists: Mapping[str, np.ndarray], weights: Mapping[str, float]
) -> tuple[np.ndarray, float]:
    """Weighted log-linear pool of calibrated K-vectors (U03-65): `(pooled q, agreement)`.

    Weights renormalize over present members (absent or all zero: equal); `agreement` is the
    share of members whose argmax equals argmax(q). Raises ConfigError for no member, vectors
    of unequal length or a negative or non-finite weight.
    """
    if not dists:
        msg = "ensemble pool needs at least one member"
        raise ConfigError(msg)
    names = sorted(dists)
    vectors = [np.asarray(dists[n], dtype=np.float64) for n in names]
    shape = vectors[0].shape
    if len(shape) != 1 or shape[0] == 0 or any(v.shape != shape for v in vectors):
        msg = "ensemble member distributions must be non-empty vectors of one length"
        raise ConfigError(msg)
    stack = np.stack(vectors)
    raw = np.array([float(weights.get(n, 0.0)) for n in names])
    if not np.all(np.isfinite(raw)) or np.any(raw < 0):
        msg = "ensemble weights must be finite and >= 0"
        raise ConfigError(msg)
    total = raw.sum()
    w = raw / total if total > 0 else np.full(len(names), 1.0 / len(names))
    log_q = w @ np.log(stack + _EPS)
    e = np.exp(log_q - log_q.max())
    pooled: np.ndarray = e / e.sum()
    top = int(np.argmax(pooled))
    agreement = float(np.mean(np.argmax(stack, axis=1) == top))
    return pooled, agreement


def ensemble_version(
    members: Sequence[tuple[str, str]], weights: Mapping[tuple[str, str], float]
) -> str:
    """12 hex of the sha256 of the canonical ensemble configuration (U03-66)."""
    doc = {
        "members": sorted([decider, version] for decider, version in members),
        "weights": {f"{d}:{q}": format(w, ".6f") for (d, q), w in weights.items()},
    }
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()[:12]


def _labels(question: Question, dists: Iterable[_Dist]) -> tuple[str, ...]:
    """Label order: fixed for bool and score; options, then any other seen key sorted."""
    fixed = _FIXED_LABELS.get(question.type)
    if fixed is not None:
        return fixed
    base = tuple(question.options or ())
    seen = {key for dist in dists for key in dist}
    return base + tuple(sorted(seen - set(base)))


def _vector(dist: _Dist, labels: Sequence[str]) -> np.ndarray:
    vec = np.array([dist.get(label, 0.0) for label in labels], dtype=np.float64)
    total = vec.sum()
    return vec / total if total > 0 else np.full(len(labels), 1.0 / len(labels))


class EnsembleDecider:
    """Pools cached member outputs (U03-67); runs no model. Single thread."""

    name = "ensemble"

    def __init__(
        self,
        cache: DecisionCache,
        calibration: CalibrationStore,
        *,
        members: Sequence[tuple[str, str]],
        weights: Mapping[tuple[str, str], float],
        qsv: str,
    ) -> None:
        self._versions = dict(members)
        if len(self._versions) != len(members):
            msg = "ensemble members must name distinct deciders"
            raise ConfigError(msg)
        self._cache = cache
        self._calibration = calibration
        self._weights = dict(weights)
        self._qsv = qsv
        self._temps: dict[tuple[str, str], float] = {}  # per `decide` call
        self.version = ensemble_version(members, weights)

    def health(self) -> None:
        """No-op: the ensemble has no backend (U03-67)."""

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """One output per input, in input order; answers only where a member row exists.

        Raises StoreBusy (or FatalError) when the cache cannot be read.
        """
        rows = self._rows(sorted({item.content_hash for item in items}), questions)
        self._temps = {}
        outputs: list[DecisionOutput] = []
        for item in items:
            asked = [(q, rows.get((item.content_hash, q.id))) for q in _asked(item, questions)]
            answers = {q.id: self._pool(q, member_rows) for q, member_rows in asked if member_rows}
            outputs.append(DecisionOutput(record_id=item.record_id, content_hash=item.content_hash,
                                          decider="ensemble", decider_version=self.version,
                                          answers=answers))  # fmt: skip
        return outputs

    def _rows(
        self, hashes: list[str], questions: QuestionSet
    ) -> dict[tuple[str, str], dict[str, _Dist]]:
        """Latest member distribution per (hash, question, decider); current fingerprints only."""
        if not hashes or not self._versions:
            return {}
        fingerprints = {q.id: q.fingerprint or question_fingerprint(q) for q in questions.questions}
        condition = (
            ds.field("content_hash").isin(hashes)
            & ds.field("question_fingerprint").isin(sorted(set(fingerprints.values())))
            & ds.field("decider").isin(sorted(self._versions))
            & ds.field("decider_version").isin(sorted(set(self._versions.values())))
        )
        try:
            dataset = self._cache.dataset()
            table = None if dataset is None else dataset.to_table(_COLUMNS, filter=condition)
        except OSError as exc:
            raise io_error(exc, "cannot read decision cache", decider=self.name) from exc
        latest: dict[tuple[str, str, str], tuple[datetime, list[tuple[str, float]]]] = {}
        for row in [] if table is None else table.to_pylist():
            current = fingerprints.get(row["question"]) == row["question_fingerprint"]
            if not current or self._versions[row["decider"]] != row["decider_version"]:
                continue
            key = (row["content_hash"], row["question"], row["decider"])
            stamp = (row["decided_at"], sorted(row["distribution"] or []))  # ties: by content
            if key not in latest or stamp > latest[key]:
                latest[key] = stamp
        out: dict[tuple[str, str], dict[str, _Dist]] = {}
        for (content_hash, qid, decider), (_, dist) in sorted(latest.items()):
            out.setdefault((content_hash, qid), {})[decider] = dict(dist)
        return out

    def _temperature(self, decider: str, qid: str) -> float:
        if (decider, qid) not in self._temps:
            version = self._versions[decider]
            try:
                t, _ = self._calibration.temperature(decider, version, self._qsv, qid)
            except OSError as exc:
                raise io_error(exc, "cannot read calibration", decider=self.name) from exc
            self._temps[(decider, qid)] = t
        return self._temps[(decider, qid)]

    def _pool(self, question: Question, member_rows: Mapping[str, _Dist]) -> Answer:
        labels, qid, qtype = _labels(question, member_rows.values()), question.id, question.type
        calibrated = {
            d: apply_temperature(_vector(p, labels)[None, :], self._temperature(d, qid), qtype)[0]
            for d, p in sorted(member_rows.items())
        }
        keys = [(decider, question.id) for decider in calibrated]
        weighted = all(key in self._weights for key in keys)  # missing accuracy: equal weights
        weights = {d: self._weights[(d, q)] for d, q in keys} if weighted else {}
        pooled, agreement = pool_log_linear(calibrated, weights)
        distribution = {label: float(p) for label, p in zip(labels, pooled, strict=True)}
        top = labels[int(np.argmax(pooled))]
        return Answer(answer=top, probability=distribution[top], distribution=distribution,
                      backend_confidence=agreement)  # fmt: skip
