"""ST03-02 (TH03-02) spy decider and spy encoder half: every model input is redacted text.

Completes the carry-over of `test_enrich_text_security.py` (text-stage half). Spec 11's
`tiny_build` is not in the tree: the stand-in is the T03-28 small build (`_planted`) with
known emails planted in the `core.*` text fields, running the real `run_enrichment` stages
through impl 02's `build_pipeline` handler. Spies record every model input: the tiny encoder
(`SpyEncoder`), the fake Laya agent (`predict_batch`, `predict_shortlist`), the loopback
OpenJev stub (request bodies) and the scripted LLM client (requests). Every ticket input
must equal an `enrich.text_redacted` value or a pair text of two of them; the mapping
encoder texts (U03-113) are built from redacted names and redacted snippets only. The
distillation round (T03-32) is checked the same way: teacher and candidate inputs are
`text_redacted` values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final

import pytest
from tests.integration.enrich._pipeline_env import PipelineEnv
from tests.integration.enrich.security._planted import (
    EMAIL_RE,
    PLANTED_EMAILS,
    PLANTED_RAW,
    SENTINEL,
    _planted,
    pipeline_env,
    planted_env,
    scan,
)
from tests.support.ops_store import OpsStoreHandle
from tests.support.stub_http import StubRequest, StubResponse
from tests.unit.enrich._distill_support import FakeCtx, build_env
from tests.unit.enrich._openjev_support import jev_env

from herness.core import redact
from herness.core.resilience import ProcessState
from herness.core.types import DecisionInput, DecisionOutput, JobOutcome, LLMRequest, QuestionSet
from herness.enrich.distill import run_distill
from herness.enrich.text import normalize_text, pair_text

pytestmark = pytest.mark.integration

__all__ = ["_planted", "jev_env", "pipeline_env", "planted_env"]  # fixtures used by name

_PAYLOAD: Final[dict[str, Any]] = {"stages": ["build", "enrich"], "depth": "deep"}
_BLOCK_RE: Final = re.compile(r"<untrusted_data [^>]*>(.*?)</untrusted_data>", re.DOTALL)
_SIDE: Final = 5_990  # link_changes._SIDE_MAX_CHARS: each pair side is cut to it


class _Spies:
    """Every model input of one run, by model."""

    def __init__(self) -> None:
        self.laya: list[str] = []
        self.openjev: list[str] = []

    def wrap_laya(self, env: PipelineEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = env.agent
        batch, shortlist = agent.predict_batch, agent.predict_shortlist

        def predict_batch(states: Sequence[str], questions: Any, **kw: Any) -> list[object]:
            self.laya.extend(states)
            return batch(states, questions, **kw)

        def predict_shortlist(state: str, questions: Any, **kw: Any) -> dict[str, object]:
            self.laya.append(state)
            return shortlist(state, questions, **kw)

        monkeypatch.setattr(agent, "predict_batch", predict_batch)
        monkeypatch.setattr(agent, "predict_shortlist", predict_shortlist)

    def wrap_openjev(self, env: PipelineEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        respond = env.stub.respond

        def spy(request: StubRequest) -> StubResponse:
            if request.method == "POST" and request.body:
                self.openjev.append(request.body.decode("utf-8"))
            return respond(request)

        monkeypatch.setattr(env.stub, "respond", spy)


def _texts(env: PipelineEnv, build_id: str) -> tuple[set[str], set[str]]:
    """(`text_redacted` values, every pair text of an incident and a change text)."""
    rows = env.query(build_id, "SELECT entity, text FROM enrich.text_redacted")
    texts = {str(t) for _, t in rows}
    incidents = [str(t) for e, t in rows if e == "incident"]
    changes = [str(t) for e, t in rows if e == "change"]
    pairs = {pair_text(i[:_SIDE], c[:_SIDE]) for i in incidents for c in changes}
    return texts, pairs


def _llm_bodies(req: LLMRequest) -> list[str]:
    parts = [p.text for m in req.messages for p in m.parts if p.type == "text"]
    return [b.replace("&lt;/", "</") for text in parts for b in _BLOCK_RE.findall(text)]


def _mapping_allowed(env: PipelineEnv, build_id: str, texts: set[str]) -> Callable[[str], bool]:
    """U03-113 texts: `<redacted name>: <redacted snippet>; ...` or `<option label>: <redacted
    description>`; every piece is redacted (or an id)."""
    rows = env.query(build_id, "SELECT team_id, name FROM core.team UNION ALL "
                     "SELECT service_id, name FROM core.service")  # fmt: skip
    names = {str(name) for _, name in rows if name}
    ids = {str(key) for key, _ in rows}
    pieces = {normalize_text(redact.redact_text(n) or "") for n in names | PLANTED_RAW}
    snippets = {normalize_text(t) for t in texts}  # 200-char prefixes, whitespace folded

    def ok_piece(piece: str) -> bool:
        piece = piece.strip()
        return (not piece or any(piece in p for p in pieces)
                or any(t.startswith(piece) for t in snippets))  # fmt: skip

    def allowed(text: str) -> bool:
        head, sep, tail = (text[:-1], ":", "") if text.endswith(":") else text.partition(": ")
        parts = tail.split("; ") if head not in ids else [tail]
        return bool(sep) and all(ok_piece(p) for p in (head if head not in ids else "", *parts))

    return allowed


def _clean(kind: str, inputs: Iterable[str]) -> int:
    n = 0
    for text in inputs:
        n += 1
        assert not scan(text), (kind, scan(text))
    return n


def test_st03_02_every_model_input_is_redacted_text_or_a_pair_text(
    planted_env: PipelineEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-02 spy encoder, Laya, OpenJev and LLM on the small build with raw emails in
    `core.*` text: no input holds a raw value or an email; ticket inputs equal a
    `text_redacted` value or a pair text; mapping texts are built from redacted parts."""
    env, spies = planted_env, _Spies()
    spies.wrap_laya(env, monkeypatch)
    spies.wrap_openjev(env, monkeypatch)
    outcome, _ = env.job(_PAYLOAD)
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "done"
    build_id = str(outcome.result["build_id"])
    # the planted raw values reached core.* (the scan is not vacuous)
    core = env.query(build_id, "SELECT short_description, description FROM core.incident")
    core_text = json.dumps(core)
    assert len({e for e in PLANTED_EMAILS if e in core_text}) >= 20
    texts, pairs = _texts(env, build_id)
    assert any(SENTINEL in t for t in texts)  # redaction kept the non-personal words
    assert not [t for t in texts if scan(t)]
    tickets = texts | pairs
    # encoder: ticket texts (stage embed) and redacted mapping texts (U03-113)
    mapping = _mapping_allowed(env, build_id, texts)
    assert _clean("encoder", env.encoder.inputs) > 0
    assert not [t for t in env.encoder.inputs if t not in texts and not mapping(t)]
    assert texts & set(env.encoder.inputs)
    # Laya, OpenJev, LLM: every state / untrusted block is a ticket text
    assert _clean("laya", spies.laya) > 0
    assert set(spies.laya) <= texts
    states = [json.loads(body)["state"] for body in spies.openjev]
    assert _clean("openjev body", spies.openjev) > 0
    assert set(states) <= tickets
    assert set(states) & pairs, "no pair text reached the teacher"
    assert _clean("llm request", (r.model_dump_json() for r in env.llm.requests)) > 0
    blocks = [b for r in env.llm.requests for b in _llm_bodies(r)]
    assert blocks
    assert set(blocks) <= tickets


def _spy_teacher(teacher: Any, seen: list[str]) -> None:
    decide = teacher.decide

    def spy(items: list[DecisionInput], questions: QuestionSet) -> list[DecisionOutput]:
        seen.extend(item.text for item in items)
        out: list[DecisionOutput] = decide(items, questions)
        return out

    teacher.decide = spy


def test_st03_02_distill_teacher_and_candidate_inputs_are_text_redacted(
    jev_env: ProcessState, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-02 distillation round: every teacher input and every candidate Laya input is an
    `enrich.text_redacted` value; none holds an email."""
    del jev_env
    env = build_env(ops_store.data_root, monkeypatch)
    teacher_inputs: list[str] = []
    _spy_teacher(env.teacher, teacher_inputs)
    laya_inputs: list[str] = []
    batch = env.agent.predict_batch

    def predict_batch(states: Sequence[str], questions: Any, **kw: Any) -> list[object]:
        laya_inputs.extend(states)
        return batch(states, questions, **kw)

    monkeypatch.setattr(env.agent, "predict_batch", predict_batch)
    report = run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())
    assert report.version is not None
    texts = {str(r[0]) for r in env.wh.execute("SELECT text FROM enrich.text_redacted").fetchall()}
    assert teacher_inputs
    assert laya_inputs
    assert set(teacher_inputs) <= texts
    assert set(laya_inputs) <= texts
    assert not [t for t in (*teacher_inputs, *laya_inputs) if EMAIL_RE.search(t)]
