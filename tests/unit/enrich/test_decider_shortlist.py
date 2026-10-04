"""Call-site tests for the per-record shortlist in the LLM and OpenJev deciders (T03-21b).

A choice question above 255 options is cut to 64 inside `decide`, before the decider schema
or wire body is built (U03-19, TH03-09); at most 255 options pass unchanged and never call
`embed_fn`; every failure is raised before any client or HTTP call (fail closed).
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
from pydantic import SecretStr
from tests.unit.enrich._fake_llm import FakeLLMClient
from tests.unit.enrich._openjev_support import BOOL, CHOICE, QS, install, jev_env, ok

from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState
from herness.core.types import DecisionInput, Question, QuestionSet
from herness.enrich.deciders.llm import LlmDecider
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_WIDE = Question(
    id="owning_team", type="choice", options_source="core.team", applies_to=("incident",),
    instructions="Which team owns this?", threshold=0.6, scoring_use=False,
)  # fmt: skip


def _wide(n: int) -> QuestionSet:
    options = {f"team_{i:04d}": f"Team {i}" for i in range(n)}
    question = _WIDE.model_copy(update={"options": options})
    return QuestionSet(version="qs-2026-10-04", questions=(question,))


class _Embed:
    """Deterministic unit vectors seeded by the text; records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: Sequence[str]) -> np.ndarray:
        self.calls.append(list(texts))
        rows = [np.random.default_rng(zlib.crc32(t.encode())).standard_normal(1024) for t in texts]
        return np.stack([(r / np.linalg.norm(r)).astype(np.float32) for r in rows])


def _item(*qids: str, text: str = "Team 7 cannot log in") -> DecisionInput:
    return DecisionInput(
        record_id="INC1", entity="incident", content_hash="1" * 32, text=text, question_ids=qids
    )


def _llm(client: FakeLLMClient, embed: Any = None) -> LlmDecider:
    return LlmDecider(
        client, version="local/qwen-test", votes=1, temperature=0.0, max_concurrency=1,
        embed_fn=embed,
    )  # fmt: skip


def _enum(req: Any, qid: str) -> list[str]:
    return list(req.response_schema["properties"][qid]["properties"]["answer"]["enum"])


def _first(req: Any) -> dict[str, object]:
    props = req.response_schema["properties"]
    return {q: {"answer": s["properties"]["answer"]["enum"][0]} for q, s in props.items()}


def _openjev(embed: Any = None) -> OpenJevDecider:
    return OpenJevDecider(
        OpenJevSettings(), api_key=SecretStr("k"), image_tag="0.4.0", samples=None, embed_fn=embed
    )


def test_st03_11_llm_schema_lists_64_shortlisted_options(jev_env: ProcessState) -> None:
    """ST03-11 > 255 options: the LLM vote schema enum holds the 64 options closest to the
    record text (shortlist before `vote_schema`); option vectors are embedded once."""
    del jev_env
    embed, client = _Embed(), FakeLLMClient(_first)
    qs = _wide(300)
    items = [_item("owning_team", text="a"), _item("owning_team", text="b")]
    outs = _llm(client, embed).decide(items, qs)
    enums = [_enum(r, "owning_team") for r in client.requests]
    assert [len(e) for e in enums] == [64, 64]
    assert all(o.error is None for o in outs)
    assert sum(len(c) == 300 for c in embed.calls) == 1
    assert set(enums[0]) <= set(qs.get("owning_team").options or {})
    assert set(enums[0]) != set(enums[1])


def test_st03_11_llm_at_most_255_options_unchanged_without_embed(jev_env: ProcessState) -> None:
    """ST03-11 255 options: all 255 stay in the schema and `embed_fn` is never called."""
    del jev_env
    embed, client = _Embed(), FakeLLMClient(_first)
    _llm(client, embed).decide([_item("owning_team")], _wide(255))
    assert len(_enum(client.requests[0], "owning_team")) == 255
    assert embed.calls == []


def test_st03_11_llm_wide_question_without_embed_fn_fails_closed(jev_env: ProcessState) -> None:
    """ST03-11 > 255 options and no `embed_fn`: ConfigError before any model call."""
    del jev_env
    client = FakeLLMClient(_first)
    with pytest.raises(ConfigError, match="more than 255 options"):
        _llm(client).decide([_item("owning_team")], _wide(300))
    assert client.requests == []


def test_st03_11_llm_embed_failure_fails_closed(jev_env: ProcessState) -> None:
    """ST03-11 `embed_fn` raising: the error propagates before any model call."""
    del jev_env

    def boom(texts: Sequence[str]) -> np.ndarray:
        msg = "encoder down"
        raise RuntimeError(msg)

    client = FakeLLMClient(_first)
    with pytest.raises(RuntimeError, match="encoder down"):
        _llm(client, boom).decide([_item("owning_team")], _wide(300))
    assert client.requests == []


def test_st03_11_openjev_wire_carries_64_criteria_per_record(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-11 > 255 options: each OpenJev request carries its own 64-criteria shortlist
    (records with different texts are not grouped onto one wire)."""
    del jev_env
    net = install(monkeypatch, ok)
    items = [_item("owning_team", text="a"), _item("owning_team", text="b")]
    outs = _openjev(_Embed()).decide(items, _wide(300))
    assert all(o.error is None for o in outs)
    crit = [set(b["questions"]["owning_team"]["criteria"]) for b in net.bodies()]
    assert [len(c) for c in crit] == [64, 64]
    assert crit[0] != crit[1]


def test_st03_11_openjev_at_most_255_unchanged_and_wide_fails_closed(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-11 255 options go on the wire whole without `embed_fn`; 300 with no `embed_fn`
    is a ConfigError and no HTTP request is made."""
    del jev_env
    net = install(monkeypatch, ok)
    embed = _Embed()
    _openjev(embed).decide([_item("owning_team")], _wide(255))
    assert len(net.bodies()[0]["questions"]["owning_team"]["criteria"]) == 255
    assert embed.calls == []
    with pytest.raises(ConfigError, match="more than 255 options"):
        _openjev().decide([_item("owning_team")], _wide(300))
    assert len(net.requests) == 1


def test_st03_11_narrow_questions_never_touch_embed_fn(jev_env: ProcessState) -> None:
    """ST03-11 bool and small choice questions: `embed_fn` is not called."""
    del jev_env
    embed, client = _Embed(), FakeLLMClient(_first)
    _llm(client, embed).decide([_item(BOOL.id, CHOICE.id)], QS)
    assert embed.calls == []
    assert len(client.requests) == 1
