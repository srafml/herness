"""Shared helpers for the OpenJev decider tests (UT03-50 ... UT03-53, ST03-16, FT03-02).

`StubDeciderServer` and the recorded fixtures of impl 11 do not exist yet: per the program
ruling the tests use hand-built Jev-shape payloads (T03-11 precedent) served by
`ScriptedNet`, a `MockNet` (tests/support/egress_mock.py) whose answer is computed per
request, so the loopback transport of `loopback_http_client` still runs for real.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from tests.support.config_tree import write_full_config
from tests.support.egress_mock import MockNet
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.core.types import DecisionInput, Question, QuestionSet
from herness.store.ops.resilience import SqliteResilienceBackend

type Reply = tuple[int, bytes, dict[str, str]]
type Handler = Callable[[dict[str, Any]], Reply]

INSTR = "Classify the ticket text."
BOOL = Question(id="is_outage", type="bool", instructions=INSTR, threshold=0.8)
CHOICE = Question(
    id="severity",
    type="choice",
    instructions=INSTR,
    options={"low": "Low impact", "mid": "Medium impact", "high": "High impact"},
    threshold=0.8,
)
SCORE = Question(
    id="urgency",
    type="score",
    instructions=INSTR,
    levels=("none", "some", "high", "critical"),
    threshold=0.8,
)
PAIR = Question(
    id="change_caused_pair", type="bool", instructions=INSTR, threshold=0.8, scoring_use=False
)
QS = QuestionSet(version="qs-2026-10-01.1", questions=(BOOL, CHOICE, SCORE, PAIR))
JSON = {"content-type": "application/json"}


def item(i: int, question_ids: tuple[str, ...] | None = None) -> DecisionInput:
    return DecisionInput(
        record_id=f"INC{i:05d}",
        entity="incident",
        content_hash=f"{i:032x}",
        text=f"ticket number {i}",
        question_ids=question_ids,
    )


def answers_for(questions: dict[str, dict[str, Any]]) -> dict[str, object]:
    """A valid Jev-shape `answers` object for the wire `questions` of a request."""
    out: dict[str, object] = {}
    for qid, wire in questions.items():
        if wire["type"] == "noul":
            out[qid] = {"noul": 0.9}
        elif wire["type"] == "choice":
            labels = list(wire["criteria"])
            rest = 0.2 / (len(labels) - 1)
            probs = {label: 0.8 if n == 0 else rest for n, label in enumerate(labels)}
            out[qid] = {"choice": labels[0], "probabilities": probs, "confidence": 0.7}
        else:
            out[qid] = {"score": 1.2, "probabilities": [0.1, 0.6, 0.2, 0.1]}
    return out


def ok(body: dict[str, Any]) -> Reply:
    """200 with valid answers for every question of the request `body`."""
    payload = {"model": body["model"], "answers": answers_for(body["questions"])}
    return 200, json.dumps(payload).encode(), JSON


class ScriptedNet(MockNet):
    """`MockNet` whose answer comes from `handler(request JSON)`; every request is kept."""

    def __init__(self, handler: Handler) -> None:
        super().__init__()
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(req.content) for req in self.requests if req.content]

    def _sync(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        status, content, headers = self.handler(body)
        return httpx2.Response(status, headers=headers, content=iter([content]))


def install(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> ScriptedNet:
    """Answer every `httpx2` pool transport built from now on with `handler`."""
    net = ScriptedNet(handler)
    net.install(monkeypatch)
    return net


@pytest.fixture
def jev_env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProcessState]:
    """Migrated ops store bound as the resilience backend, the full test config (policy
    `decider_local`: 3 attempts), a fixed-key redactor and a fresh process state."""
    del ops_store, fake_keyring
    bind_ops_backend(SqliteResilienceBackend())
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    c.reset_config()
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    yield reset_process_state
    c.reset_config()
