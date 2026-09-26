"""Laya fine-tuning adapters (impl 03 §3.22, U03-130 ... U03-134; design 03 §5.8 step 5).

`SoftLabelSftTrainer` is the fallback: KL to teacher distributions on encoder and head.
`RlcdTrainer` delegates to the vendored notebook procedure `herness.enrich._vendor.laya_rlcd`
and `select_trainer` picks it only when that module exists. `laya` and `torch` are imported
lazily; the `laya` package is not a dependency yet (verification item V-11) and every
training call into the agent or its model is marked `# V-11:`. Checkpoints and weights are
safetensors only (TH03-16, `_train_ckpt`); gold hashes never reach a `TrainingSet` (TH03-04).
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import pyarrow as pa

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json, sha256_hex
from herness.core.logging import get_logger
from herness.core.types import QuestionSet
from herness.enrich._sft_loop import SftLoop
from herness.enrich.questions import question_fingerprint

if TYPE_CHECKING:
    from herness.core.jobs import JobContext

__all__ = [
    "TRAIN_SCHEMA",
    "LayaTrainer",
    "RlcdTrainer",
    "SoftLabelSftTrainer",
    "TrainHyper",
    "TrainResult",
    "TrainingSet",
    "select_trainer",
]

TRAIN_SCHEMA: Final = pa.schema([
    ("content_hash", pa.string()), ("text", pa.string()), ("question", pa.string()),
    ("target", pa.map_(pa.string(), pa.float64())), ("weight", pa.float64()),
])  # fmt: skip
VENDOR_MODULE: Final = "herness.enrich._vendor.laya_rlcd"
_HUMAN_WEIGHT: Final = 3.0
_VAL_BELOW: Final = 2  # validation: last hex digit of sha256(content_hash) < 2 (OI-12)

_log = get_logger("enrich.distill")


@dataclass(frozen=True)
class TrainingSet:
    """Inputs for one fine-tuning run (U03-130); tables have `TRAIN_SCHEMA`."""

    train: pa.Table
    val: pa.Table
    questions: QuestionSet
    sha256: str


@dataclass(frozen=True)
class TrainHyper:
    """Design 03 §5.8 starting hyperparameters (U03-131); `seed` is recorded in the manifest."""

    epochs: int = 4
    lr_encoder: float = 2e-5
    lr_head: float = 1e-4
    weight_decay: float = 0.01
    warmup: float = 0.06
    micro_batch: int = 8
    accumulation: int = 4
    bf16: bool = True
    gradient_checkpointing: bool = True
    head_checkpointing: bool = True
    max_len: int = 512
    patience: int = 1
    seed: int = 0
    wall_clock_cap_s: float = 6 * 3600.0


@dataclass(frozen=True)
class TrainResult:
    """Outcome of `LayaTrainer.train` (U03-131)."""

    epochs_run: int
    best_val_nll: float
    files: tuple[Path, ...]


class LayaTrainer(Protocol):
    """Adapter over the Laya training procedure (U03-131); runs inside `gpu_scope("decider")`."""

    name: str

    def train(
        self, data: TrainingSet, *, init_dir: Path, out_dir: Path, hyper: TrainHyper,
        ctx: JobContext,
    ) -> TrainResult: ...  # fmt: skip


def _is_val(content_hash: str) -> bool:
    return int(sha256_hex(content_hash)[-1], 16) < _VAL_BELOW


def build_training_set(
    teacher: pa.Table,
    human: pa.Table,
    *,
    texts: Mapping[str, str],
    questions: QuestionSet,
    gold: AbstractSet[str],
) -> TrainingSet:
    """Private helper of `run_distill` (U03-130, UT03-126): the training set of `questions`.

    `teacher` has the teacher label schema, `human` the latest human rows
    (`LabelStore.latest_human`), `texts` maps content hash to redacted text, `questions` is
    the trainable (unblocked) set. Rows of other questions or fingerprints, gold hashes,
    hashes without text and empty distributions are dropped; the latest round wins per
    (hash, question). A human correction replaces the target with a one-hot, weight 3.
    """
    wanted = {q.id: q.fingerprint or question_fingerprint(q) for q in questions.questions}
    names = ("content_hash", "question", "question_fingerprint", "answer")
    corrections = {
        (h, q): answer
        for h, q, fp, answer in zip(*(human.column(n).to_pylist() for n in names), strict=True)
        if wanted.get(q) == fp
    }
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(teacher.to_pylist(), key=lambda r: -1 if r["round"] is None else r["round"]):
        key = (row["content_hash"], row["question"])
        stale = wanted.get(key[1]) != row["question_fingerprint"] or not row["distribution"]
        if stale or key[0] in gold or key[0] not in texts:
            continue
        target, weight = dict(row["distribution"]), 1.0
        if key in corrections:
            target, weight = {corrections[key]: 1.0}, _HUMAN_WEIGHT
        rows[key] = {"content_hash": key[0], "text": texts[key[0]], "question": key[1],
                     "target": target, "weight": weight}  # fmt: skip
    ordered = [rows[key] for key in sorted(rows)]
    digest = sha256_hex(
        canonical_json([[r["content_hash"], r["question"], r["target"]] for r in ordered])
    )
    return TrainingSet(
        train=_table([r for r in ordered if not _is_val(r["content_hash"])]),
        val=_table([r for r in ordered if _is_val(r["content_hash"])]),
        questions=questions,
        sha256=digest,
    )


def _table(rows: list[dict[str, Any]]) -> pa.Table:
    listed = [{**row, "target": sorted(row["target"].items())} for row in rows]
    return pa.Table.from_pylist(listed, schema=TRAIN_SCHEMA)


def _load_agent(init_dir: Path) -> Any:  # noqa: ANN401 - V-11: the Laya agent is untyped
    try:
        laya = importlib.import_module("laya")  # V-11: package name
    except ModuleNotFoundError as exc:
        msg = "the laya package is not installed"
        raise ConfigError(msg) from exc
    return laya.load(str(init_dir), fast=False)  # V-11: load signature for training


class SoftLabelSftTrainer:
    """Fallback trainer: soft-label SFT with KL to teacher distributions (U03-132)."""

    name: str = "sft"

    def __init__(self, *, device: str | None = None, check_every: int = 50) -> None:
        self._device = device
        self._check_every = check_every

    def train(
        self, data: TrainingSet, *, init_dir: Path, out_dir: Path, hyper: TrainHyper,
        ctx: JobContext,
    ) -> TrainResult:  # fmt: skip
        """Train from `init_dir` into `out_dir`, resuming from its latest complete checkpoint.

        Raises ConfigError (no rows, unresolved options, `laya` missing, no config file),
        FatalError (CUDA OOM at micro batch 1) and YieldRequested("train") after saving a
        mid-epoch checkpoint when `ctx.should_yield()` fires.
        """
        import torch  # noqa: PLC0415 - lazy import

        loop = SftLoop(data, hyper, ctx, self._check_every)
        device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
        epochs_run, best_val_nll, files = loop.execute(
            _load_agent(init_dir), device, (init_dir, out_dir)
        )
        return TrainResult(epochs_run, best_val_nll, files)


class RlcdTrainer:
    """Laya's published RLCD procedure, vendored from the notebook (U03-133)."""

    name: str = "rlcd"

    def train(
        self, data: TrainingSet, *, init_dir: Path, out_dir: Path, hyper: TrainHyper,
        ctx: JobContext,
    ) -> TrainResult:  # fmt: skip
        """Delegate to the vendored module. Raises ConfigError when it is missing."""
        try:
            vendored = importlib.import_module(VENDOR_MODULE)
        except ModuleNotFoundError as exc:
            msg = "vendored Laya RLCD module is missing"
            raise ConfigError(msg) from exc
        # V-11: vendored entry point name and signature are frozen at Phase 4
        result = vendored.train(data, init_dir=init_dir, out_dir=out_dir, hyper=hyper, ctx=ctx)
        if not isinstance(result, TrainResult):
            msg = "vendored Laya RLCD returned no TrainResult"
            raise ConfigError(msg)
        return result


def _vendor_available() -> bool:
    try:
        return importlib.util.find_spec(VENDOR_MODULE) is not None
    except ModuleNotFoundError:  # parent package `herness.enrich._vendor` absent
        return False


def select_trainer() -> LayaTrainer:
    """`RlcdTrainer` when the vendored module imports, else `SoftLabelSftTrainer` (U03-134)."""
    trainer: LayaTrainer = RlcdTrainer() if _vendor_available() else SoftLabelSftTrainer()
    _log.info("enrich.distill.trainer_selected", trainer=trainer.name)
    return trainer
