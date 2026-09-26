"""Laya student backend on the local GPU (impl 03 U03-57 ... U03-59, design 03 §3.3).

`laya` and `torch` are imported lazily inside methods: importing this module loads
neither. The `laya` package is not a dependency yet (verification item V-11); every call
into it is marked `# V-11:`. Loading is local-only (`HF_HUB_OFFLINE=1`) from a directory
that passed `verify_model_dir` (TH03-05). Registration as `("decider", "laya")` is T03-16's.
Concurrency: one GPU owner thread per instance.
"""

from __future__ import annotations

import functools
import importlib
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import numpy as np

from herness.core.errors import ConfigError, ModelUnavailable, OutputValidationError
from herness.core.logging import get_logger
from herness.core.types import Answer, DecisionInput, DecisionOutput, Question, QuestionSet
from herness.enrich.deciders.jev_wire import parse_wire_answers, to_wire_questions
from herness.enrich.gpu import release_cuda, run_batches_with_oom_backoff
from herness.enrich.laya_models import read_current, verify_model_dir
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import PAIR_QUESTIONS
from herness.enrich.settings import LayaSettings

__all__ = ["LayaDecider"]

EmbedFn = Callable[[Sequence[str]], np.ndarray]

_CALL_TIMEOUT_S: Final = 30.0
_SHORTLIST_ABOVE: Final = 20  # choice questions with more options take the shortlist path
_SHORTLIST_K: Final = 16
_ACCEPTED: Final = frozenset({"accepted"})

_log = get_logger("enrich.decider")


def _call_with_timeout[T](fn: Callable[[], T], timeout_s: float) -> T:
    """Run `fn` in a daemon thread and wait at most `timeout_s` seconds (U08-32 steps 1-5).

    T08-07: replace with herness.core.resilience.call_with_timeout.
    Raises ModelUnavailable on timeout (the stuck thread dies with the job's process),
    re-raises `fn`'s exception unchanged, ConfigError when `timeout_s <= 0`.
    """
    if timeout_s <= 0:
        msg = f"timeout_s must be > 0, got {timeout_s}"
        raise ConfigError(msg)
    values: list[T] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            values.append(fn())
        except BaseException as exc:  # noqa: BLE001 - handed to the caller unchanged
            errors.append(exc)

    thread = threading.Thread(target=runner, name="herness-timeout", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        msg = f"call timed out after {timeout_s}s"
        raise ModelUnavailable(msg)
    if errors:
        raise errors[0]
    return values[0]


def _asked(item: DecisionInput, questions: QuestionSet) -> tuple[Question, ...]:
    """Questions asked for `item` (impl 03 §3.9 shared rules)."""
    if item.question_ids is not None:
        return tuple(questions.get(qid) for qid in item.question_ids)
    subset = questions.for_entity(item.entity).questions
    return tuple(q for q in subset if q.id not in PAIR_QUESTIONS)


def _is_wide(question: Question) -> bool:
    return question.type == "choice" and len(question.options or {}) > _SHORTLIST_ABOVE


def _answers_of(result: object) -> Mapping[str, object]:
    answers = result.get("answers") if isinstance(result, Mapping) else None
    if not isinstance(answers, Mapping):
        msg = "laya result: answers must be an object"
        raise OutputValidationError(msg)
    return answers


def _parse(parts: Sequence[object], asked: Sequence[Question]) -> dict[str, Answer]:
    """Merge the result parts of one state and parse them strictly against `asked`."""
    merged: dict[str, object] = {}
    for part in parts:
        merged.update(_answers_of(part))
    answers = parse_wire_answers(merged, asked)
    if len(answers) != len(asked):
        msg = "laya result: missing answers"
        raise OutputValidationError(msg)
    return answers


def _to_device(agent: object, device: str, dtype_name: str) -> object:
    """Move the agent to `device` in `dtype` (V-11: exact Laya call not verified yet)."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    dtype = torch.bfloat16 if dtype_name == "bf16" else torch.float32
    move = getattr(agent, "to", None)
    if callable(move):
        moved = move(device, dtype)  # V-11: agent-level move when the agent has `.to`
        return agent if moved is None else moved
    wrapped: Any = agent
    wrapped.model.to(device, dtype)  # V-11: else move the wrapped model
    return agent


class LayaDecider:
    """Laya student inference (U03-57): verified local weights, batched on the GPU.

    `version` is the argument or `read_current(paths)`. The model is loaded at most once
    per instance: `load()` is idempotent while loaded, and after `unload()` (which calls
    `release_cuda()`) this instance refuses to load again. `decide` loads on first use.
    """

    name: str = "laya"
    version: str

    def __init__(
        self,
        settings: LayaSettings,
        *,
        paths: EnrichPaths,
        version: str | None = None,
        embed_fn: EmbedFn | None = None,
    ) -> None:
        self._settings = settings
        self._paths = paths
        self.version = version if version is not None else read_current(paths)
        paths.laya_dir(self.version)  # validates the version pattern (TH03-08)
        self._embed_fn = embed_fn
        self._agent: Any = None
        self._laya: Any = None
        self._unloaded = False

    def load(self) -> None:
        """Verify, then load the model on `settings.device` (U03-57).

        Raises ConfigError when verification fails and ModelUnavailable("laya load") when
        the `laya` package is missing or loading fails.
        """
        if self._unloaded:
            msg = "laya decider was unloaded"
            raise ModelUnavailable(msg)
        if self._agent is not None:
            return
        verify_model_dir(self._paths, self.version)
        os.environ["HF_HUB_OFFLINE"] = "1"
        directory = str(self._paths.laya_dir(self.version))
        try:
            laya = importlib.import_module("laya")  # V-11: package name and repo path
            agent = laya.load(directory, fast=self._settings.fast)  # V-11: load signature
            agent = _to_device(agent, self._settings.device, self._settings.dtype)
        except Exception as exc:
            msg = "laya load"
            raise ModelUnavailable(msg) from exc
        self._laya, self._agent = laya, agent
        _log.info("enrich.decider.laya_loaded", version=self.version, fast=self._settings.fast)

    def unload(self) -> None:
        """Drop the model and free CUDA memory (U03-57 invariant)."""
        self._agent = None
        self._laya = None
        self._unloaded = True
        release_cuda()

    def health(self) -> None:
        """Check that `CURRENT` names an accepted, untampered version (U03-59).

        Raises ModelUnavailable("laya: <reason>") on any failure.
        """
        try:
            current = read_current(self._paths)
            verify_model_dir(self._paths, current, require_status=_ACCEPTED)
        except ConfigError as exc:
            msg = f"laya: {exc.message}"
            raise ModelUnavailable(msg) from exc

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """Batched Laya inference (U03-58): one output per input, in input order.

        Raises ConfigError for a >20-option choice question without `embed_fn`,
        FatalError on OOM at batch 1 and ModelUnavailable on a timeout.
        """
        self.load()
        asked = [_asked(item, questions) for item in items]
        if self._embed_fn is None and any(_is_wide(q) for qs in asked for q in qs):
            msg = "laya: choice questions with more than 20 options need embed_fn"
            raise ConfigError(msg)  # checked before any model call
        groups: dict[tuple[str, ...], list[int]] = {}
        for index, qs in enumerate(asked):
            groups.setdefault(tuple(q.id for q in qs), []).append(index)
        outputs: dict[int, DecisionOutput] = {}
        for indices in groups.values():
            group_asked = asked[indices[0]]
            parts = self._run_group([items[i].text for i in indices], group_asked)
            for index, state_parts in zip(indices, parts, strict=True):
                outputs[index] = self._output(items[index], state_parts, group_asked)
        return [outputs[index] for index in range(len(items))]

    def _run_group(self, states: list[str], asked: Sequence[Question]) -> list[list[object]]:
        """Raw result parts per state: one batched result plus one per shortlisted question."""
        wide = [q for q in asked if _is_wide(q)]
        parts: list[list[object]] = [[] for _ in states]
        narrow = [q for q in asked if not _is_wide(q)]
        if narrow:
            for state_parts, result in zip(parts, self._predict(states, narrow), strict=True):
                state_parts.append(result)
        for question in wide:
            for state_parts, result in zip(parts, self._shortlist(states, question), strict=True):
                state_parts.append(result)
        return parts

    def _predict(self, states: list[str], asked: Sequence[Question]) -> list[object]:
        wire = to_wire_questions(asked)
        agent, batch_size = self._agent, self._settings.batch_size

        def call(batch: Sequence[str]) -> list[object]:
            results = _call_with_timeout(
                # V-11: predict_batch returns results in input order, shaped like predict
                lambda: agent.predict_batch(
                    list(batch), wire, batch_size=batch_size, sort_by_length=True
                ),
                _CALL_TIMEOUT_S,
            )
            results = list(results)
            if len(results) != len(batch):
                msg = "laya: predict_batch result count does not match the batch"
                raise ModelUnavailable(msg)
            return results

        start = self._settings.call_batch
        return run_batches_with_oom_backoff(
            states, call, start_batch=start, fault_name="decider.batch"
        )

    def _shortlist(self, states: list[str], question: Question) -> list[object]:
        q_wire = to_wire_questions([question])
        agent, laya, embed_fn = self._agent, self._laya, self._embed_fn

        def call(batch: Sequence[str]) -> list[object]:
            # V-11: predict_shortlist(agent, state, questions, embed_fn=..., k=...)
            shortlist = functools.partial(
                laya.predict_shortlist, agent, embed_fn=embed_fn, k=_SHORTLIST_K
            )
            return [
                _call_with_timeout(functools.partial(shortlist, state, [q_wire]), _CALL_TIMEOUT_S)
                for state in batch
            ]

        return run_batches_with_oom_backoff(states, call, start_batch=1, fault_name="decider.batch")

    def _output(
        self, item: DecisionInput, parts: Sequence[object], asked: Sequence[Question]
    ) -> DecisionOutput:
        try:
            answers = _parse(parts, asked) if asked else {}
        except OutputValidationError as exc:
            _log.warning("enrich.decider.invalid_output", decider=self.name, rule=exc.message)
            return self._make(item, {}, error=type(exc).__name__)
        return self._make(item, answers, error=None)

    def _make(
        self, item: DecisionInput, answers: dict[str, Answer], *, error: str | None
    ) -> DecisionOutput:
        return DecisionOutput(
            record_id=item.record_id,
            content_hash=item.content_hash,
            decider="laya",
            decider_version=self.version,
            answers=answers,
            error=error,
        )
