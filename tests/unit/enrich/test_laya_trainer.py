"""Tests for herness.enrich.laya_trainer (U03-130 ... U03-134; T03-31).

Training runs on CPU with the tiny trainable fake Laya of `tests/support/fake_laya.py`
installed as the `laya` module; CUDA is present on the dev box, so every trainer is built
with `device="cpu"`.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
import torch
from safetensors.torch import load_file
from structlog.testing import capture_logs
from tests.support.fake_laya import WEIGHT_FILES, fake_laya_train_module, trainable_fake_laya

from herness.core import time as clock
from herness.core.errors import ConfigError, FatalError
from herness.core.ids import sha256_hex
from herness.core.jobs import JobContext
from herness.core.types import Question, QuestionSet
from herness.enrich import _sft_loop, _train_ckpt, gpu, laya_trainer
from herness.enrich.deciders.jev_wire import to_wire_questions
from herness.enrich.gpu import YieldRequested
from herness.enrich.labels import HUMAN_SCHEMA, TEACHER_SCHEMA
from herness.enrich.laya_trainer import (
    RlcdTrainer,
    SoftLabelSftTrainer,
    TrainHyper,
    TrainingSet,
    TrainResult,
    build_training_set,
    select_trainer,
)
from herness.enrich.questions import question_fingerprint

pytestmark = pytest.mark.unit

_QS = QuestionSet(
    version="qs-2026-10-01.1",
    questions=(
        Question(id="is_outage", type="bool", instructions="Was the service down?", threshold=0.7),
        Question(
            id="root_cause",
            type="choice",
            instructions="Pick the root cause category.",
            options={"network": "Network fault", "disk": "Disk fault", "code": "Code defect"},
            threshold=0.7,
        ),
    ),
)
_FP = {q.id: question_fingerprint(q) for q in _QS.questions}
_TS = datetime(2026, 10, 1, tzinfo=UTC)


def _hash(i: int) -> str:
    return f"{i:032x}"


def _is_val(content_hash: str) -> bool:
    return int(sha256_hex(content_hash)[-1], 16) < 2


def _teacher_row(h: str, qid: str, dist: dict[str, float], **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "content_hash": h, "record_id": f"r-{h[-4:]}", "question": qid,
        "question_fingerprint": _FP[qid], "answer": max(dist, key=dist.__getitem__, default="true"),
        "distribution": list(dist.items()), "decider": "openjev",
        "decider_version": "openjev-0.4.0", "round": 1, "stratum": "s", "purpose": "sample",
    }  # fmt: skip
    return {**row, **extra}


def _human_row(h: str, qid: str, answer: str, fp: str | None = None) -> dict[str, Any]:
    return {
        "content_hash": h, "record_id": f"r-{h[-4:]}", "question": qid,
        "question_fingerprint": fp or _FP[qid], "answer": answer, "labeled_by": "rev",
        "labeled_at": _TS, "item_id": f"it-{h[-4:]}-{qid}",
    }  # fmt: skip


def _dist(i: int, qid: str) -> dict[str, float]:
    if qid == "is_outage":
        p = 0.9 if i % 2 else 0.2
        return {"true": p, "false": 1 - p}
    labels = ["network", "disk", "code"]
    top = labels[i % 3]
    return {label: (0.8 if label == top else 0.1) for label in labels}


def _data(n: int = 48) -> TrainingSet:
    rows = [_teacher_row(_hash(i), qid, _dist(i, qid)) for i in range(n) for qid in _FP]
    teacher = pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA)
    human = HUMAN_SCHEMA.empty_table()
    texts = {_hash(i): f"ticket {i} {'db down' if i % 2 else 'slow ui'} {i % 3}" for i in range(n)}
    return build_training_set(teacher, human, texts=texts, questions=_QS, gold=set())


# ---- UT03-126 build_training_set -------------------------------------------------------


def test_ut03_126_correction_gold_and_validation_rule() -> None:
    """UT03-126 teacher rows, a human correction, gold hashes: weight 3, no gold, val rule."""
    hashes = [_hash(i) for i in range(40)]
    gold = {hashes[0], hashes[1]}
    rows = [_teacher_row(h, "root_cause", _dist(i, "root_cause")) for i, h in enumerate(hashes)]
    rows.append(_teacher_row(hashes[5], "is_outage", {"true": 0.6, "false": 0.4}))
    rows.append(_teacher_row(hashes[6], "is_outage", {"true": 0.6, "false": 0.4},
                             question_fingerprint="f" * 16))  # fmt: skip
    rows.append(_teacher_row(hashes[7], "is_outage", {}))
    teacher = pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA)
    human = pa.Table.from_pylist(
        [
            _human_row(hashes[3], "root_cause", "disk"),
            _human_row(hashes[4], "root_cause", "code", fp="e" * 16),
            _human_row(hashes[0], "root_cause", "disk"),
        ],
        schema=HUMAN_SCHEMA,
    )
    texts = {h: f"text {h}" for h in hashes if h != hashes[9]}

    data = build_training_set(teacher, human, texts=texts, questions=_QS, gold=gold)

    both = pa.concat_tables([data.train, data.val]).to_pylist()
    by_key = {(r["content_hash"], r["question"]): r for r in both}
    assert data.train.schema.equals(laya_trainer.TRAIN_SCHEMA)
    assert not gold & {r["content_hash"] for r in both}
    corrected = by_key[hashes[3], "root_cause"]
    assert corrected["weight"] == 3.0
    assert dict(corrected["target"]) == {"disk": 1.0}
    stale_human = by_key[hashes[4], "root_cause"]
    assert stale_human["weight"] == 1.0
    assert dict(stale_human["target"]) == _dist(4, "root_cause")
    assert (hashes[6], "is_outage") not in by_key  # stale teacher fingerprint
    assert (hashes[7], "is_outage") not in by_key  # empty distribution
    assert (hashes[9], "root_cause") not in by_key  # no text
    assert by_key[hashes[5], "is_outage"]["text"] == texts[hashes[5]]
    assert all(_is_val(h) for h in data.val.column("content_hash").to_pylist())
    assert not any(_is_val(h) for h in data.train.column("content_hash").to_pylist())
    assert data.val.num_rows > 0
    assert data.train.num_rows + data.val.num_rows == 38  # 40 - 2 gold - 1 without text + 1 outage
    assert data.questions is _QS


def test_ut03_126_latest_round_wins_and_digest_is_order_free() -> None:
    """UT03-126 duplicate teacher rows: latest round wins; sha256 ignores row order."""
    h = _hash(1)
    old = _teacher_row(h, "root_cause", {"network": 1.0}, round=1)
    new = _teacher_row(h, "root_cause", {"code": 1.0}, round=2)
    unknown = _teacher_row(_hash(2), "root_cause", {"disk": 1.0}, round=None)
    rows = [new, unknown, old]
    kwargs: dict[str, Any] = {
        "texts": {h: "a", _hash(2): "b"}, "questions": _QS, "gold": frozenset(),
    }  # fmt: skip
    human = HUMAN_SCHEMA.empty_table()
    first = build_training_set(pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA), human, **kwargs)
    second = build_training_set(
        pa.Table.from_pylist(rows[::-1], schema=TEACHER_SCHEMA), human, **kwargs
    )
    both = pa.concat_tables([first.train, first.val]).to_pylist()
    assert {r["content_hash"]: dict(r["target"]) for r in both}[h] == {"code": 1.0}
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64


# ---- UT03-127 trainers -----------------------------------------------------------------


@dataclass
class _Ctx:
    """Minimal JobContext: `should_yield` fires on the `yield_at`-th check."""

    yield_at: int | None = None
    kill_at: int | None = None
    checks: int = 0
    heartbeats: list[str | None] = field(default_factory=list)

    def should_yield(self) -> bool:
        self.checks += 1
        return self.yield_at is not None and self.checks >= self.yield_at

    def heartbeat(self, note: str | None = None) -> None:
        self.heartbeats.append(note)
        if self.kill_at is not None and len(self.heartbeats) >= self.kill_at:
            raise KeyboardInterrupt  # simulated process kill

    @contextmanager
    def gpu_scope(self, cls: str) -> Iterator[None]:
        yield


def _ctx(**kwargs: Any) -> Any:
    return cast(JobContext, _Ctx(**kwargs))


_HYPER = TrainHyper(epochs=2, micro_batch=4, accumulation=2, bf16=False, seed=7, lr_head=0.05)
_WIRE = to_wire_questions(_QS.questions)


@pytest.fixture
def laya(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a `laya` stand-in whose every `load` builds a fresh tiny trainable model."""
    module = fake_laya_train_module(lambda: trainable_fake_laya(_WIRE))
    monkeypatch.setitem(sys.modules, "laya", module)
    return module


@pytest.fixture
def init_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "base"
    directory.mkdir()
    for name, content in WEIGHT_FILES.items():
        (directory / name).write_bytes(content)
    return directory


def _train(out: Path, init: Path, ctx: Any, hyper: TrainHyper = _HYPER) -> TrainResult:
    trainer = SoftLabelSftTrainer(device="cpu", check_every=1)
    return trainer.train(_data(), init_dir=init, out_dir=out, hyper=hyper, ctx=ctx)


def _micro_batches(n: int, hyper: TrainHyper) -> int:
    return -(-n // hyper.micro_batch)


def test_ut03_127_sft_two_epochs_checkpoint_per_epoch(
    laya: types.ModuleType, init_dir: Path, tmp_path: Path
) -> None:
    """UT03-127 SFT 2 epochs on the fake model: checkpoint per epoch, outputs, heartbeats."""
    out = tmp_path / "v1"
    ctx = _ctx()
    result = _train(out, init_dir, ctx)

    assert result.epochs_run == 2
    assert result.best_val_nll > 0
    names = sorted(p.name for p in (out / "checkpoints").iterdir())
    assert names == ["epoch-1", "epoch-2"]
    for name in names:
        files = sorted(p.name for p in (out / "checkpoints" / name).iterdir())
        assert files == ["model.safetensors", "optimizer.safetensors", "state.json"]
    assert {p.name for p in result.files} == set(WEIGHT_FILES)
    assert all(p.parent == out for p in result.files)
    assert not [p for p in out.rglob("*") if p.suffix in {".pt", ".bin", ".pkl"}]
    agent = laya.loaded[0]
    assert agent.head_checkpointing is True
    assert agent.checkpointing_enabled is True
    assert set(ctx.heartbeats) == {"train"}
    assert any(training for _, _, training in agent.logit_calls)
    assert any(not training for _, _, training in agent.logit_calls)
    weights = load_file(str(out / "model.safetensors"))
    assert set(weights) == set(agent.state_dict())


def _weights(path: Path) -> dict[str, torch.Tensor]:
    return load_file(str(path / "model.safetensors"))


def test_ut03_127_yield_mid_epoch_then_resume_matches_uninterrupted(
    laya: types.ModuleType, init_dir: Path, tmp_path: Path
) -> None:
    """UT03-127 kill by yield mid-epoch 2 -> mid-epoch checkpoint; resume continues from it."""
    reference = _train(tmp_path / "ref", init_dir, _ctx())
    out = tmp_path / "v1"
    steps_per_epoch = -(-_micro_batches(_data().train.num_rows, _HYPER) // _HYPER.accumulation)
    with pytest.raises(YieldRequested) as info:
        _train(out, init_dir, _ctx(yield_at=steps_per_epoch + 2))
    assert info.value.stage == "train"
    names = sorted(p.name for p in (out / "checkpoints").iterdir())
    assert names[0] == "epoch-1"
    assert names[1].startswith("epoch-2-step-")

    resumed_agent_index = len(laya.loaded)
    result = _train(out, init_dir, _ctx())
    assert result.epochs_run == 2
    assert sorted(p.name for p in (out / "checkpoints").iterdir()) == ["epoch-1", "epoch-2"]
    calls = laya.loaded[resumed_agent_index].logit_calls
    trained_rows = sum(n for n, _, training in calls if training)
    assert trained_rows < _data().train.num_rows  # only the rest of epoch 2 was trained
    assert result.best_val_nll == pytest.approx(reference.best_val_nll)
    for key, value in _weights(tmp_path / "ref").items():
        assert torch.allclose(value, _weights(out)[key])


def test_ut03_127_kill_after_epoch_one_resumes_from_epoch_checkpoint(
    laya: types.ModuleType, init_dir: Path, tmp_path: Path
) -> None:
    """UT03-127 process killed in epoch 2 (no yield): resume redoes epoch 2 from epoch-1."""
    reference = _train(tmp_path / "ref", init_dir, _ctx())
    out = tmp_path / "v1"
    steps_per_epoch = -(-_micro_batches(_data().train.num_rows, _HYPER) // _HYPER.accumulation)
    with pytest.raises(KeyboardInterrupt):
        _train(out, init_dir, _ctx(kill_at=steps_per_epoch + 1))
    assert [p.name for p in (out / "checkpoints").iterdir()] == ["epoch-1"]
    result = _train(out, init_dir, _ctx())
    assert result.epochs_run == 2
    assert result.best_val_nll == pytest.approx(reference.best_val_nll)
    again = _train(out, init_dir, _ctx())  # finished run: outputs rewritten, no training
    assert again == result
    assert not any(training for _, _, training in laya.loaded[-1].logit_calls)


def test_ut03_127_early_stop_and_wall_clock_cap(
    laya: types.ModuleType, init_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-127 no val improvement stops after patience 1; the wall-clock cap stops and logs."""
    frozen = replace(_HYPER, epochs=4, lr_head=0.0, lr_encoder=0.0)
    result = _train(tmp_path / "flat", init_dir, _ctx(), frozen)
    assert result.epochs_run == 2
    state = _train_ckpt.read_state(tmp_path / "flat" / "checkpoints" / "epoch-2")
    assert state["best_epoch"] == 1

    ticks = iter(range(0, 10**6, 10_000))
    monkeypatch.setattr(clock, "monotonic", lambda: float(next(ticks)))
    capped = replace(_HYPER, epochs=4, wall_clock_cap_s=5_000.0)
    with capture_logs() as logs:
        result = _train(tmp_path / "cap", init_dir, _ctx(), capped)
    assert result.epochs_run == 1
    events = [e for e in logs if e["event"] == "enrich.distill.wall_clock_cap"]
    assert events == [
        {"event": "enrich.distill.wall_clock_cap", "log_level": "warning", "epochs_run": 1,
         "component": "enrich.distill",
         "best_epoch": 1},
    ]  # fmt: skip


def test_ut03_127_cuda_oom_at_micro_batch_one_is_fatal(
    monkeypatch: pytest.MonkeyPatch, init_dir: Path, tmp_path: Path
) -> None:
    """UT03-127 CUDA OOM down to micro batch 1 -> FatalError."""
    monkeypatch.setattr(gpu, "release_cuda", lambda: None)
    oom = torch.cuda.OutOfMemoryError("simulated oom")
    module = fake_laya_train_module(lambda: trainable_fake_laya(_WIRE, fail_with=oom))
    monkeypatch.setitem(sys.modules, "laya", module)
    with pytest.raises(FatalError, match="cuda oom at batch 1"):
        _train(tmp_path / "v1", init_dir, _ctx())


def test_ut03_127_config_errors(
    laya: types.ModuleType, init_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-127 empty validation, missing config file, non-torch agent, laya missing."""
    data = _data()
    trainer = SoftLabelSftTrainer(device="cpu")
    no_val = replace(data, val=data.val.slice(0, 0))
    with pytest.raises(ConfigError, match="validation rows"):
        trainer.train(no_val, init_dir=init_dir, out_dir=tmp_path / "a", hyper=_HYPER, ctx=_ctx())
    (init_dir / "rl_agent_config.json").unlink()
    with pytest.raises(ConfigError, match=r"rl_agent_config\.json"):
        trainer.train(data, init_dir=init_dir, out_dir=tmp_path / "b", hyper=_HYPER, ctx=_ctx())
    monkeypatch.setitem(sys.modules, "laya", fake_laya_train_module(object))
    with pytest.raises(ConfigError, match="no torch module"):
        trainer.train(data, init_dir=init_dir, out_dir=tmp_path / "c", hyper=_HYPER, ctx=_ctx())
    monkeypatch.setitem(sys.modules, "laya", None)
    with pytest.raises(ConfigError, match="not installed"):
        trainer.train(data, init_dir=init_dir, out_dir=tmp_path / "d", hyper=_HYPER, ctx=_ctx())


def test_ut03_127_targets_and_schedule() -> None:
    """UT03-127 bool targets are two-way; choice targets renormalise; warmup then decay."""
    outage, cause = _QS.questions
    assert _sft_loop._target_vector(outage, {"false": 1.0}) == (0.0, 1.0)
    assert _sft_loop._target_vector(outage, {"true": 0.25}) == (0.25, 0.75)
    assert _sft_loop._target_vector(cause, {"disk": 2.0, "other": 5.0}) == (0.0, 1.0, 0.0)
    with pytest.raises(ConfigError, match="no mass"):
        _sft_loop._target_vector(cause, {"other": 1.0})
    score = Question(id="sev", type="score", instructions="How severe was it?", threshold=0.7,
                     levels=("none", "low", "high", "critical"))  # fmt: skip
    assert _sft_loop.label_order(score) == ("0", "1", "2", "3")
    dynamic = Question(id="team", type="choice", instructions="Which team owns it?",
                       options_source="core.team", threshold=0.7)  # fmt: skip
    with pytest.raises(ConfigError, match="not resolved"):
        _sft_loop.label_order(dynamic)
    factors = [_sft_loop._lr_factor(step, 100, 0.06) for step in (1, 6, 53, 100)]
    assert factors == pytest.approx([1 / 6, 1.0, 0.5, 0.0])


def test_ut03_127_select_trainer_logs_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-127 no vendored module -> SFT; vendored module present -> RLCD; choice logged."""
    with capture_logs() as logs:
        trainer = select_trainer()
    assert isinstance(trainer, SoftLabelSftTrainer)
    assert trainer.name == "sft"
    assert logs == [
        {
            "event": "enrich.distill.trainer_selected",
            "log_level": "info",
            "trainer": "sft",
            "component": "enrich.distill",
        }
    ]

    vendored = types.ModuleType(laya_trainer.VENDOR_MODULE)
    vendored.__spec__ = ModuleSpec(laya_trainer.VENDOR_MODULE, None)
    monkeypatch.setitem(sys.modules, laya_trainer.VENDOR_MODULE, vendored)
    with capture_logs() as logs:
        trainer = select_trainer()
    assert isinstance(trainer, RlcdTrainer)
    assert logs[0]["trainer"] == "rlcd"


def test_ut03_127_rlcd_delegates_or_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UT03-127 RlcdTrainer: vendored module missing -> ConfigError; present -> delegates."""
    trainer = RlcdTrainer()
    kwargs: dict[str, Any] = {
        "init_dir": tmp_path, "out_dir": tmp_path / "out", "hyper": _HYPER, "ctx": _ctx(),
    }  # fmt: skip
    with pytest.raises(ConfigError, match="missing"):
        trainer.train(_data(), **kwargs)

    expected = TrainResult(epochs_run=3, best_val_nll=0.5, files=(tmp_path / "model.safetensors",))
    calls: list[dict[str, Any]] = []
    vendored = types.ModuleType(laya_trainer.VENDOR_MODULE)

    def train(data: TrainingSet, **kw: Any) -> object:
        calls.append(kw)
        return expected if len(calls) == 1 else {"epochs_run": 1}

    vendored.train = train  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, laya_trainer.VENDOR_MODULE, vendored)
    assert trainer.train(_data(), **kwargs) is expected
    assert calls[0] == kwargs
    with pytest.raises(ConfigError, match="no TrainResult"):
        trainer.train(_data(), **kwargs)
