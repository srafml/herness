"""Loopback OpenJev wire stub with oracle and hash answers and faults (U11-46; TH11-10).

``StubDeciderServer`` speaks the spec 03 §3.3 wire protocol (``GET /v1/models``,
``POST /v1/systemone``) on 127.0.0.1 only (``StubHTTPServer``). It is an HTTP stub, not a
``Decider`` and not registered in ``herness.core.registry``; ``service_name="openjev"``
names it in the stub service file for ``kill_service:openjev`` (FT11-03). Answers are pure
functions of ``(content_hash, question)``, so the same state gets byte-identical responses
from any instance. ``probabilities`` is a label-keyed object until spec 03 Q1 is frozen.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

import pyarrow.parquet as pq
from tests.support.stub_http import StubFault, StubHTTPServer, StubRequest, StubResponse

from herness.enrich.deciders.jev_wire import MAX_BODY_BYTES

__all__ = [
    "MODELS_PATH",
    "MODEL_ID",
    "SYSTEMONE_PATH",
    "StubDeciderServer",
    "StubFault",
    "content_hash_of",
]

type StubDeciderMode = Literal["oracle", "hash"]
type Labels = Mapping[tuple[str, str], str]

MODEL_ID: Final = "openjev-latest"
MODELS_PATH: Final = "/v1/models"
SYSTEMONE_PATH: Final = "/v1/systemone"
_MODES: Final = frozenset({"oracle", "hash"})
_BOOL_LABELS: Final = ("true", "false")
_SCORE_LABELS: Final = ("0", "1", "2", "3")
_MALFORMED_BODY: Final = b'{"answers": '
_JSON: Final = {"Content-Type": "application/json"}
_LABEL_COLUMNS: Final = ["content_hash", "question", "answer"]


class _BadRequestError(ValueError):
    """The request body is not a Jev-shape ``/v1/systemone`` request (answered 400)."""


def content_hash_of(state: str) -> str:
    """SHA-256 hex[:32] of ``state`` (the spec 03 U03-26 ``content_hash``)."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()[:32]


def _load_labels(path: Path) -> Labels:
    """``truth_labels.parquet`` as a read-only ``{(content_hash, question): answer}`` map."""
    columns = pq.read_table(path, columns=_LABEL_COLUMNS).to_pydict()
    rows = zip(*(columns[name] for name in _LABEL_COLUMNS), strict=True)
    return MappingProxyType({(str(h), str(q)): str(a) for h, q, a in rows})


def _labels_for(qid: str, spec: object) -> tuple[str, ...]:
    """The K answer labels of one wire question: noul true/false, choice keys, score 0..3."""
    if not isinstance(spec, Mapping):
        msg = f"question {qid} is not an object"
        raise _BadRequestError(msg)
    kind, criteria = spec.get("type"), spec.get("criteria")
    if kind == "noul":
        return _BOOL_LABELS
    if kind == "score":
        return _SCORE_LABELS
    if kind == "choice" and isinstance(criteria, Mapping) and criteria:
        return tuple(str(label) for label in criteria)
    msg = f"question {qid} has an unknown type or no criteria"
    raise _BadRequestError(msg)


def _hash_pick(content_hash: str, qid: str, k: int) -> tuple[int, float]:
    """Hash mode: ``(answer index, top probability)`` from SHA-256 of ``hash:qid``."""
    digest = hashlib.sha256(f"{content_hash}:{qid}".encode()).hexdigest()
    return int(digest[0:8], 16) % k, 0.5 + 0.5 * int(digest[8:16], 16) / 0xFFFFFFFF


def _spread(labels: Sequence[str], index: int, top: float) -> dict[str, float]:
    """``top`` on ``labels[index]``, the rest spread evenly over the other labels."""
    if len(labels) == 1:
        return {labels[0]: 1.0}
    rest = (1.0 - top) / (len(labels) - 1)
    return {label: top if n == index else rest for n, label in enumerate(labels)}


def _confidence(distribution: Mapping[str, float]) -> float:
    """``1 - H(p) / ln K`` clamped to [0, 1]; 1 for a single label."""
    if len(distribution) < 2:
        return 1.0
    entropy = -math.fsum(p * math.log(p) for p in distribution.values() if p > 0)
    return min(1.0, max(0.0, 1.0 - entropy / math.log(len(distribution))))


def _wire_answer(
    kind: object, answer: str, distribution: dict[str, float], criteria: object
) -> dict[str, object]:
    """The Jev-shape answer object for one question (the fields ``parse_wire_answers`` takes)."""
    if kind == "noul":
        return {"noul": distribution["true"]}
    if kind == "choice":
        return {
            "choice": answer,
            "probabilities": distribution,
            "confidence": _confidence(distribution),
        }
    score = math.fsum(int(level) * p for level, p in distribution.items())
    legend = list(criteria) if isinstance(criteria, list | tuple) else []
    return {
        "score": score,
        "legend": legend,
        "probabilities": distribution,
        "confidence": _confidence(distribution),
    }


def _decode(body: bytes) -> tuple[str, Mapping[str, object]]:
    """``(state, questions)`` of a request body; raises ``_BadRequestError``."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        msg = "body is not valid JSON"
        raise _BadRequestError(msg) from exc
    if not isinstance(payload, dict):
        msg = "body is not a JSON object"
        raise _BadRequestError(msg)
    state, questions = payload.get("state"), payload.get("questions")
    if not isinstance(state, str) or not isinstance(questions, dict):
        msg = "body needs a string state and a questions object"
        raise _BadRequestError(msg)
    return state, questions


class StubDeciderServer(StubHTTPServer):
    """Spec 03 §3.3 wire-compatible decider stub (U11-46) on a loopback ``StubHTTPServer``.

    ``oracle`` answers the truth label of ``(content_hash, qid)`` with probability
    ``1 - noise`` (``noise / (K - 1)`` on each other label) and falls back to ``hash``
    when there is no label (or the label is not one of the question's options). Faults
    apply by the 0-based index of ``POST /v1/systemone`` requests (every one takes an
    index, even a rejected body); ``GET /v1/models`` and other routes take none.
    """

    def __init__(
        self,
        mode: StubDeciderMode = "oracle",
        truth_labels: Path | None = None,
        noise: float = 0.0,
        *,
        port: int = 0,
        faults: Sequence[StubFault] = (),
        service_name: str | None = "openjev",
    ) -> None:
        if mode not in _MODES:
            msg = "mode must be oracle or hash"
            raise ValueError(msg)
        if mode == "oracle" and truth_labels is None:
            msg = "oracle mode requires truth_labels"
            raise ValueError(msg)
        if not 0.0 <= noise < 1.0:  # NaN fails too
            msg = "noise must satisfy 0 <= noise < 1"
            raise ValueError(msg)
        self.mode: StubDeciderMode = mode
        self.noise = noise
        self.faults = tuple(faults)
        self._labels: Labels = (  # loaded once, never mutated
            _load_labels(truth_labels) if truth_labels is not None else MappingProxyType({})
        )
        self._calls = 0
        self._calls_lock = threading.Lock()
        super().__init__(port=port, service_name=service_name)

    @property
    def calls(self) -> int:
        """``POST /v1/systemone`` requests received so far (the fault index of the next)."""
        with self._calls_lock:
            return self._calls

    def respond(self, request: StubRequest) -> StubResponse:
        """``GET /v1/models``, ``POST /v1/systemone`` (faults first), else 404."""
        if request.method == "GET" and request.path == MODELS_PATH:
            return StubResponse.json(200, {"object": "list", "data": [{"id": MODEL_ID}]})
        if request.method != "POST" or request.path != SYSTEMONE_PATH:
            return super().respond(request)
        with self._calls_lock:
            index = self._calls
            self._calls += 1
        fault = next((f for f in self.faults if f.covers(index)), None)
        if fault is not None:
            return self._fault(fault)
        if len(request.body) > MAX_BODY_BYTES:
            return StubResponse.json(413, {"error": "request body exceeds 1 MB"})
        try:
            return StubResponse.json(200, self._answer(request.body))
        except _BadRequestError as exc:
            return StubResponse.json(400, {"error": str(exc)})

    def _fault(self, fault: StubFault) -> StubResponse:
        if fault.kind == "http_429":
            return StubResponse.json(429, {"error": "rate limited"}, **{"Retry-After": "1"})
        if fault.kind == "http_529":
            return StubResponse.json(529, {"error": "overloaded"})
        if fault.kind == "malformed_json":
            return StubResponse(200, _MALFORMED_BODY, dict(_JSON))
        self.kill()  # "kill": listener and every connection closed; this call gets no answer
        return StubResponse(0, disconnect=True)

    def _answer(self, body: bytes) -> dict[str, object]:
        """The ``/v1/systemone`` response for one request body; raises ``_BadRequestError``."""
        state, questions = _decode(body)
        chash = content_hash_of(state)
        answers: dict[str, object] = {}
        for qid, spec in questions.items():
            labels = _labels_for(qid, spec)  # also checks that `spec` is an object
            wire = cast("Mapping[str, object]", spec)
            index, top = self._pick(chash, qid, labels)
            distribution = _spread(labels, index, top)
            answers[qid] = _wire_answer(
                wire.get("type"), labels[index], distribution, wire.get("criteria")
            )
        usage = {"input_tokens": len(state) // 4, "output_tokens": 0}
        return {"model": MODEL_ID, "answers": answers, "usage": usage}

    def _pick(self, chash: str, qid: str, labels: Sequence[str]) -> tuple[int, float]:
        """Oracle: the truth label at ``1 - noise``; hash mode (and oracle fallback) else."""
        truth = self._labels.get((chash, qid)) if self.mode == "oracle" else None
        if truth is not None and truth in labels:
            return labels.index(truth), 1.0 - self.noise
        return _hash_pick(chash, qid, len(labels))
