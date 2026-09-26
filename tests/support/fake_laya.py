"""Tiny Laya-shaped fake agent and model-directory builder (impl 03 §11, T03-14).

`FakeLayaAgent` mimics the documented `laya` 0.3.x surface (design 03 §3.3):
`predict_batch(states, questions, *, batch_size, sort_by_length)` returns one result per
state, in input order, shaped `{"answers": {qid: {...}}}` in the Jev wire shape (choice
answers carry `probabilities`), and `predict_shortlist(...)` answers one wide choice
question over its top-k labels. Every call is recorded so tests can assert batch sizes and
shortlist use. `fake_laya_module` wraps an agent in a module object exposing `load` and
`predict_shortlist`, to be installed with `monkeypatch.setitem(sys.modules, "laya", ...)`.
No torch model is involved.

`write_laya_version` writes a minimal, hash-consistent version directory with a manifest.

Training (T03-31, verification item V-11): `trainable_fake_laya` builds a tiny torch module
(byte `EmbeddingBag` encoder + one linear head per question) exposing the training-mode call
frozen for `SoftLabelSftTrainer`:
`question_logits(states, questions, *, max_len) -> {qid: Tensor[len(states), K]}` where
`questions` is Jev wire shape and K is 1 for `noul` (logit of "true") or the number of
`criteria` in wire order. Encoder parameters are named `encoder.*`, head ones `head.*`.
`fake_laya_train_module(factory)` is a `laya` stand-in whose `load` builds a fresh agent per
call (a new process after a kill).
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WireQuestions = Mapping[str, Mapping[str, object]]

WEIGHT_FILES: dict[str, bytes] = {
    "model.safetensors": b"\x08\x00\x00\x00\x00\x00\x00\x00{}      " + bytes(range(64)),
    "rl_agent_config.json": b'{"head_max_len": 192}',
    "tokenizer.json": b'{"version": "1.0"}',
    "tokenizer_config.json": b'{"model_max_length": 512}',
}


def _answer(spec: Mapping[str, object], *, with_probabilities: bool) -> dict[str, object]:
    kind = spec["type"]
    if kind == "noul":
        return {"noul": 0.8}
    criteria = spec["criteria"]
    labels = list(criteria) if isinstance(criteria, Mapping) else ["0", "1", "2", "3"]
    first = 0.5 + 0.5 / len(labels)
    rest = (1.0 - first) / (len(labels) - 1)
    probs = {label: (first if i == 0 else rest) for i, label in enumerate(labels)}
    answer: dict[str, object] = {"confidence": 0.5}
    if kind == "choice":
        answer["choice"] = labels[0]
    if with_probabilities:
        answer["probabilities"] = probs
    return answer


@dataclass
class FakeLayaAgent:
    """Records `predict_batch`, `predict_shortlist` and `to` calls; answers deterministically."""

    with_probabilities: bool = True
    fail_with: BaseException | None = None
    batch_calls: list[tuple[int, int, bool]] = field(default_factory=list)
    shortlist_calls: list[tuple[str, tuple[str, ...], int]] = field(default_factory=list)
    to_calls: list[tuple[object, object]] = field(default_factory=list)
    result_override: Callable[[str], object] | None = None

    def to(self, device: object, dtype: object) -> FakeLayaAgent:
        self.to_calls.append((device, dtype))
        return self

    def predict_batch(
        self,
        states: Sequence[str],
        questions: WireQuestions,
        *,
        batch_size: int = 64,
        sort_by_length: bool = True,
    ) -> list[object]:
        self.batch_calls.append((len(states), batch_size, sort_by_length))
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        return [self._result(state, questions) for state in states]

    def _result(self, state: str, questions: WireQuestions) -> object:
        if self.result_override is not None:
            return self.result_override(state)
        answers = {
            qid: _answer(spec, with_probabilities=self.with_probabilities)
            for qid, spec in questions.items()
        }
        return {"answers": answers}

    def predict_shortlist(
        self,
        state: str,
        questions: Sequence[WireQuestions],
        *,
        embed_fn: Callable[[Sequence[str]], object],
        k: int = 16,
    ) -> dict[str, object]:
        answers: dict[str, object] = {}
        for wire in questions:
            for qid, spec in wire.items():
                criteria = spec["criteria"]
                assert isinstance(criteria, Mapping)
                labels = list(criteria)
                embed_fn([state, *labels])
                self.shortlist_calls.append((qid, tuple(labels), k))
                top = labels[:k]
                probs = {label: (1.0 / len(top) if label in top else 0.0) for label in labels}
                answers[qid] = {"choice": top[0], "probabilities": probs, "confidence": 0.1}
        return {"answers": answers}


def fake_laya_module(agent: FakeLayaAgent) -> types.ModuleType:
    """A stand-in `laya` module whose `load` returns `agent` and records its arguments."""
    module = types.ModuleType("laya")
    loads: list[tuple[str, bool]] = []

    def load(path: str, fast: bool = False) -> FakeLayaAgent:
        loads.append((path, fast))
        return agent

    def predict_shortlist(
        agent_: FakeLayaAgent,
        state: str,
        questions: Sequence[WireQuestions],
        *,
        embed_fn: Callable[[Sequence[str]], object],
        k: int = 16,
    ) -> dict[str, object]:
        return agent_.predict_shortlist(state, questions, embed_fn=embed_fn, k=k)

    module.load = load  # type: ignore[attr-defined]
    module.predict_shortlist = predict_shortlist  # type: ignore[attr-defined]
    module.loads = loads  # type: ignore[attr-defined]
    return module


def manifest_dict(version: str, weights: Mapping[str, str], *, status: str) -> dict[str, object]:
    """A valid `manifest.json` payload for `version` with the given hashes and status."""
    accepted = status == "accepted"
    return {
        "version": version,
        "parent_version": None,
        "base_checkpoint": "convaiinnovations/laya-typed-decisions",
        "teacher": "openjev",
        "teacher_version": "openjev-0.4.0/openjev-latest",
        "question_set_version": "qs-2026-10-01.1",
        "train_data_sha256": "0" * 64,
        "n_train": 1000,
        "hyperparams": {"trainer": "laya", "seed": 7, "round": 1, "round_kind": "full"},
        "weights_sha256": dict(weights),
        "accepted_questions": ["root_cause"],
        "status": status,
        "created_at": datetime(2026, 10, 4, 3, 0, tzinfo=UTC).isoformat(),
        "accepted_by": "admin" if accepted else None,
        "accepted_at": datetime(2026, 10, 4, 4, 0, tzinfo=UTC).isoformat() if accepted else None,
    }


def write_laya_version(laya_root: Path, version: str, *, status: str = "accepted") -> Path:
    """Write weights, tokenizer files and a matching manifest under `laya_root/version`."""
    directory = laya_root / version
    directory.mkdir(parents=True)
    weights: dict[str, str] = {}
    for name, content in WEIGHT_FILES.items():
        (directory / name).write_bytes(content)
        weights[name] = hashlib.sha256(content).hexdigest()
    (directory / "calibration.json").write_text("{}", encoding="utf-8")
    payload = manifest_dict(version, weights, status=status)
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def trainable_fake_laya(
    questions: WireQuestions,
    *,
    dim: int = 8,
    seed: int = 0,
    fail_with: BaseException | None = None,
) -> Any:
    """A tiny trainable Laya-shaped torch module answering `question_logits` (V-11 fixture)."""
    import torch  # noqa: PLC0415 - keep torch out of imports of this helper module

    class TrainableFakeLaya(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            with torch.random.fork_rng(devices=[]):
                torch.manual_seed(seed)
                self.encoder = torch.nn.EmbeddingBag(256, dim, mode="mean")
                self.head = torch.nn.ModuleDict(
                    {
                        qid: torch.nn.Linear(dim, 1 if spec["type"] == "noul" else _width(spec))
                        for qid, spec in questions.items()
                    }
                )
            self.head_checkpointing = False
            self.checkpointing_enabled = False
            self.logit_calls: list[tuple[int, tuple[str, ...], bool]] = []
            self.fail_with = fail_with

        def gradient_checkpointing_enable(self) -> None:
            self.checkpointing_enabled = True

        def question_logits(
            self, states: Sequence[str], questions: WireQuestions, *, max_len: int
        ) -> dict[str, Any]:
            self.logit_calls.append((len(states), tuple(questions), self.training))
            if self.fail_with is not None:
                raise self.fail_with
            ids = [list(state.encode("utf-8")[:max_len]) or [0] for state in states]
            offsets = torch.tensor([0, *[len(x) for x in ids[:-1]]]).cumsum(0)
            flat = torch.tensor([b for x in ids for b in x])
            embedded = self.encoder(flat, offsets)
            return {qid: self.head[qid](embedded) for qid in questions}

    return TrainableFakeLaya()


def _width(spec: Mapping[str, object]) -> int:
    criteria = spec["criteria"]
    assert isinstance(criteria, Mapping | list)
    return len(criteria)


def fake_laya_train_module(factory: Callable[[], Any]) -> types.ModuleType:
    """A stand-in `laya` module whose `load` returns a fresh `factory()` agent per call."""
    module = types.ModuleType("laya")
    loaded: list[Any] = []

    def load(path: str, fast: bool = False) -> Any:
        agent = factory()
        loaded.append(agent)
        return agent

    module.load = load  # type: ignore[attr-defined]
    module.loaded = loaded  # type: ignore[attr-defined]
    return module
