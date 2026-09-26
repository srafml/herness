"""Tests for herness.enrich.deciders.laya.LayaDecider (U03-57 ... U03-59, T03-14).

`laya` is not installed (V-11): a fake module from tests/support/fake_laya.py is put in
`sys.modules`. Settings force `device="cpu"`, and the fake agent's `to` only records, so
nothing touches CUDA.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
import torch
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version

from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.types import DecisionInput, Question, QuestionSet
from herness.enrich.deciders import laya as laya_decider
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.laya_models import write_current
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import LayaSettings

pytestmark = pytest.mark.unit

_V1 = "laya-20261004-1"
_INSTR = "Classify the ticket text."
_BOOL = Question(id="is_outage", type="bool", instructions=_INSTR, threshold=0.8)
_CHOICE = Question(
    id="severity",
    type="choice",
    instructions=_INSTR,
    options={"low": "Low impact", "mid": "Medium impact", "high": "High impact"},
    threshold=0.8,
)
_SCORE = Question(
    id="urgency",
    type="score",
    instructions=_INSTR,
    levels=("none", "some", "high", "critical"),
    threshold=0.8,
)
_WIDE = Question(
    id="team",
    type="choice",
    instructions=_INSTR,
    options={f"team_{i:02d}": f"Team number {i}" for i in range(30)},
    threshold=0.8,
)
_PAIR = Question(
    id="change_caused_pair",
    type="bool",
    instructions=_INSTR,
    threshold=0.8,
    scoring_use=False,
)
_CHANGE_Q = Question(
    id="change_risky", type="bool", instructions=_INSTR, threshold=0.8, applies_to=("change",)
)
_QS = QuestionSet(
    version="qs-2026-10-01.1", questions=(_BOOL, _CHOICE, _SCORE, _WIDE, _PAIR, _CHANGE_Q)
)


def _embed(texts: Sequence[str]) -> np.ndarray:
    return np.zeros((len(texts), 4), dtype=np.float32)


def _item(
    i: int, question_ids: tuple[str, ...] | None = None, entity: str = "incident"
) -> DecisionInput:
    return DecisionInput(
        record_id=f"INC{i:05d}",
        entity=entity,  # type: ignore[arg-type]
        content_hash=f"{i:032x}",
        text=f"ticket number {i} " * (1 + i % 3),
        question_ids=question_ids,
    )


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=tmp_path.resolve(),
        embedding_path="data/models/e",
        laya_current_file="data/models/laya/CURRENT",
    )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> FakeLayaAgent:
    fake = FakeLayaAgent()
    monkeypatch.setitem(sys.modules, "laya", fake_laya_module(fake))
    return fake


@pytest.fixture(autouse=True)
def _restore_hf_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """load() sets HF_HUB_OFFLINE=1; setenv first so monkeypatch restores the original."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.delenv("HF_HUB_OFFLINE")


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(laya_decider, "release_cuda", lambda: calls.append(1))
    monkeypatch.setattr("herness.enrich.gpu.release_cuda", lambda: calls.append(1))
    return calls


def _decider(paths: EnrichPaths, **kwargs: object) -> LayaDecider:
    settings = LayaSettings(device="cpu")
    return LayaDecider(settings, paths=paths, version=_V1, **kwargs)  # type: ignore[arg-type]


def _loaded(paths: EnrichPaths, **kwargs: object) -> LayaDecider:
    write_laya_version(paths.laya_root(), _V1)
    decider = _decider(paths, **kwargs)
    decider.load()
    return decider


# --- U03-57 construction and load -----------------------------------------------------


def test_ut03_55_bad_hash_load_raises_config_error(
    paths: EnrichPaths, agent: FakeLayaAgent
) -> None:
    """UT03-55 a model directory with a bad hash: load raises ConfigError, laya never loads."""
    directory = write_laya_version(paths.laya_root(), _V1)
    (directory / "rl_agent_config.json").write_bytes(b'{"head_max_len": 999}')
    with pytest.raises(ConfigError, match=f"{_V1}: weight hash mismatch"):
        _decider(paths).load()
    assert sys.modules["laya"].loads == []  # type: ignore[attr-defined]
    assert "HF_HUB_OFFLINE" not in os.environ


def test_ut03_55_load_sets_offline_and_moves_agent(
    paths: EnrichPaths, agent: FakeLayaAgent
) -> None:
    """UT03-55 a good load: HF_HUB_OFFLINE=1, local path, fast flag, bf16 on the device, once."""
    write_laya_version(paths.laya_root(), _V1)
    settings = LayaSettings(device="cpu", fast=True)
    decider = LayaDecider(settings, paths=paths, version=_V1)
    decider.load()
    decider.load()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert sys.modules["laya"].loads == [(str(paths.laya_dir(_V1)), True)]  # type: ignore[attr-defined]
    assert agent.to_calls == [("cpu", torch.bfloat16)]


def test_ut03_55_fp32_and_model_attribute_fallback(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-55 an agent without `.to` moves its `model` attribute; dtype fp32 maps to float32."""
    moved: list[tuple[object, object]] = []

    class _Model:
        def to(self, device: object, dtype: object) -> None:
            moved.append((device, dtype))

    class _Agent:
        model = _Model()

    module = fake_laya_module(FakeLayaAgent())
    module.load = lambda path, fast=False: _Agent()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "laya", module)
    write_laya_version(paths.laya_root(), _V1)
    LayaDecider(LayaSettings(device="cpu", dtype="fp32"), paths=paths, version=_V1).load()
    assert moved == [("cpu", torch.float32)]


def test_ut03_55_missing_laya_package_is_model_unavailable(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-55 `laya` not importable -> ModelUnavailable("laya load")."""
    monkeypatch.setitem(sys.modules, "laya", None)
    write_laya_version(paths.laya_root(), _V1)
    with pytest.raises(ModelUnavailable, match=r"^laya load$"):
        _decider(paths).load()


def test_ut03_55_load_failure_is_model_unavailable(
    paths: EnrichPaths, agent: FakeLayaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-55 an exception inside laya.load -> ModelUnavailable("laya load")."""

    def broken(path: str, fast: bool = False) -> object:
        msg = "weights corrupt"
        raise RuntimeError(msg)

    monkeypatch.setattr(sys.modules["laya"], "load", broken)
    write_laya_version(paths.laya_root(), _V1)
    with pytest.raises(ModelUnavailable, match=r"^laya load$"):
        _decider(paths).load()


def test_ut03_55_version_from_current_and_validation(paths: EnrichPaths) -> None:
    """UT03-55 version defaults to CURRENT; a malformed version argument -> ConfigError."""
    write_current(paths, "laya-20261004-2")
    decider = LayaDecider(LayaSettings(device="cpu"), paths=paths)
    assert (decider.name, decider.version) == ("laya", "laya-20261004-2")
    with pytest.raises(ConfigError):
        LayaDecider(LayaSettings(device="cpu"), paths=paths, version="../x")


def test_ut03_55_unload_releases_and_blocks_reload(
    paths: EnrichPaths, agent: FakeLayaAgent, released: list[int]
) -> None:
    """UT03-55 unload calls release_cuda; the instance never loads a second time."""
    decider = _loaded(paths)
    decider.unload()
    assert released == [1]
    with pytest.raises(ModelUnavailable, match="unloaded"):
        decider.load()
    with pytest.raises(ModelUnavailable, match="unloaded"):
        decider.decide([_item(1)], _QS)


# --- U03-58 decide ----------------------------------------------------------------------


def test_ut03_56_batched_calls_and_shortlist_path(paths: EnrichPaths, agent: FakeLayaAgent) -> None:
    """UT03-56 300 items, a 30-option question: predict_batch calls of <= 256, shortlist used."""
    decider = _loaded(paths, embed_fn=_embed)
    items = [_item(i) for i in range(300)]
    outputs = decider.decide(items, _QS)
    assert [call[0] for call in agent.batch_calls] == [256, 44]
    assert all(size <= 256 and bs == 64 and srt for size, bs, srt in agent.batch_calls)
    assert len(agent.shortlist_calls) == 300
    assert {(qid, len(labels), k) for qid, labels, k in agent.shortlist_calls} == {("team", 30, 16)}
    assert [o.record_id for o in outputs] == [i.record_id for i in items]
    first = outputs[0]
    assert first.error is None
    assert (first.decider, first.decider_version) == ("laya", _V1)
    assert list(first.answers) == ["is_outage", "severity", "urgency", "team"]
    assert first.answers["severity"].answer == "low"
    assert first.answers["team"].answer == "team_00"
    assert "change_caused_pair" not in first.answers


def test_ut03_56_missing_embed_fn_raises_config_error(
    paths: EnrichPaths, agent: FakeLayaAgent
) -> None:
    """UT03-56 a >20-option choice question without embed_fn -> ConfigError before any call."""
    decider = _loaded(paths)
    with pytest.raises(ConfigError, match="embed_fn"):
        decider.decide([_item(1)], _QS)
    assert agent.batch_calls == []


def test_ut03_56_groups_by_asked_questions_in_input_order(
    paths: EnrichPaths, agent: FakeLayaAgent
) -> None:
    """UT03-56 items are grouped by asked question ids; outputs keep input order."""
    decider = _loaded(paths)
    items = [
        _item(0, ("is_outage",)),
        _item(1, ("severity", "urgency")),
        _item(2, ("is_outage",)),
        _item(3, entity="change"),
        _item(4, ()),
    ]
    outputs = decider.decide(items, _QS)
    assert [o.record_id for o in outputs] == [i.record_id for i in items]
    assert [list(o.answers) for o in outputs] == [
        ["is_outage"],
        ["severity", "urgency"],
        ["is_outage"],
        ["change_risky"],
        [],
    ]
    assert sorted(call[0] for call in agent.batch_calls) == [1, 1, 2]
    assert decider.decide([], _QS) == []


def test_ut03_56_choice_without_probabilities_is_item_error(
    paths: EnrichPaths, agent: FakeLayaAgent
) -> None:
    """UT03-56 a choice answer without `probabilities` -> that item is an error output."""
    agent.with_probabilities = False
    decider = _loaded(paths)
    outputs = decider.decide([_item(1, ("is_outage", "severity"))], _QS)
    assert outputs[0].error == "OutputValidationError"
    assert outputs[0].answers == {}


@pytest.mark.parametrize(
    "result",
    [None, {"answers": []}, {"answers": {}}, {"answers": {"is_outage": {"noul": 2.0}}}],
)
def test_ut03_56_malformed_results_are_item_errors(
    paths: EnrichPaths, agent: FakeLayaAgent, result: object
) -> None:
    """UT03-56 a non-object, missing, incomplete or invalid result -> item error output."""
    agent.result_override = lambda state: result
    decider = _loaded(paths)
    outputs = decider.decide([_item(1, ("is_outage",))], _QS)
    assert outputs[0].error == "OutputValidationError"


def test_ut03_56_result_count_mismatch_is_model_unavailable(
    paths: EnrichPaths, agent: FakeLayaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-56 predict_batch returning the wrong number of results -> ModelUnavailable."""
    decider = _loaded(paths)
    monkeypatch.setattr(agent, "predict_batch", lambda *a, **k: [])
    with pytest.raises(ModelUnavailable, match="result count"):
        decider.decide([_item(1, ("is_outage",))], _QS)


def test_ut03_56_oom_is_retried_by_backoff(
    paths: EnrichPaths, agent: FakeLayaAgent, released: list[int]
) -> None:
    """UT03-56 a CUDA OOM inside the timeout thread reaches the backoff helper unchanged."""
    agent.fail_with = torch.cuda.OutOfMemoryError("simulated oom")
    decider = _loaded(paths)
    outputs = decider.decide([_item(i, ("is_outage",)) for i in range(300)], _QS)
    assert [call[0] for call in agent.batch_calls] == [256, 256, 44]
    assert released == [1]
    assert all(o.error is None for o in outputs)


def test_ut03_56_timeout_is_model_unavailable(
    paths: EnrichPaths, agent: FakeLayaAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-56 a predict_batch call exceeding the timeout -> ModelUnavailable."""
    gate = threading.Event()
    decider = _loaded(paths)
    monkeypatch.setattr(laya_decider, "_CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(agent, "predict_batch", lambda *a, **k: gate.wait(5))
    try:
        with pytest.raises(ModelUnavailable, match="timed out"):
            decider.decide([_item(1, ("is_outage",))], _QS)
    finally:
        gate.set()


def test_ut03_56_decide_loads_on_first_use(paths: EnrichPaths, agent: FakeLayaAgent) -> None:
    """UT03-56 decide before an explicit load() loads the verified model first."""
    write_laya_version(paths.laya_root(), _V1)
    outputs = _decider(paths).decide([_item(1, ("is_outage",))], _QS)
    assert outputs[0].answers["is_outage"].answer == "true"


def test_ut03_56_call_with_timeout_contract() -> None:
    """UT03-56 _call_with_timeout returns values, re-raises unchanged, times out, checks args."""
    assert laya_decider._call_with_timeout(lambda: 42, 1.0) == 42
    error = ValueError("boom")

    def fail() -> None:
        raise error

    with pytest.raises(ValueError, match="boom") as info:
        laya_decider._call_with_timeout(fail, 1.0)
    assert info.value is error
    gate = threading.Event()
    try:
        with pytest.raises(ModelUnavailable, match=r"^call timed out after 0.05s$"):
            laya_decider._call_with_timeout(lambda: gate.wait(5), 0.05)
    finally:
        gate.set()
    with pytest.raises(ConfigError):
        laya_decider._call_with_timeout(lambda: 1, 0)


# --- U03-59 health ----------------------------------------------------------------------


def test_ut03_57_health(paths: EnrichPaths) -> None:
    """UT03-57 missing CURRENT and candidate status -> ModelUnavailable; accepted -> ok."""
    decider = _decider(paths)
    with pytest.raises(ModelUnavailable, match=r"^laya: laya CURRENT missing or invalid$"):
        decider.health()
    write_laya_version(paths.laya_root(), "laya-20261004-2", status="candidate")
    write_current(paths, "laya-20261004-2")
    with pytest.raises(ModelUnavailable, match="status not allowed"):
        decider.health()
    write_laya_version(paths.laya_root(), _V1)
    write_current(paths, _V1)
    assert decider.health() is None
