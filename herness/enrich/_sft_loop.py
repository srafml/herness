"""Soft-label SFT loop of `SoftLabelSftTrainer` (private helper of U03-132, impl 03 §3.22).

`SftLoop` runs one `train` call: AdamW with encoder and head groups, linear warmup then
decay, bf16 autocast, gradient accumulation, per-epoch validation NLL with early stopping,
per-epoch and yield checkpoints (`_train_ckpt`), the wall-clock cap, and the output files.
Loss = weighted mean over (row, question) of KL(target || softmax(logits)); bool targets are
two-way (true, false). Every call into the Laya agent or its model is marked `# V-11:`.
`torch` is imported lazily inside functions.
"""

from __future__ import annotations

import math
import random
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.types import Question, QuestionSet
from herness.enrich import _train_ckpt as ckpt
from herness.enrich.deciders.jev_wire import to_wire_questions
from herness.enrich.gpu import YieldRequested, run_batches_with_oom_backoff

if TYPE_CHECKING:
    import torch

    from herness.core.jobs import JobContext
    from herness.enrich.laya_trainer import TrainHyper, TrainingSet

_SIDE_FILES: Final = ("rl_agent_config.json", "tokenizer*", "special_tokens_map.json", "vocab*")
_BOOL_LABELS: Final = ("true", "false")
_SCORE_LABELS: Final = ("0", "1", "2", "3")

type Outcome = tuple[int, float, tuple[Path, ...]]

_log = get_logger("enrich.distill")


def label_order(question: Question) -> tuple[str, ...]:
    """Target labels of `question` in wire order (bool: true, false)."""
    if question.type == "bool":
        return _BOOL_LABELS
    if question.type == "score":
        return _SCORE_LABELS
    if question.options is None:
        msg = f"{question.id}: choice options are not resolved"
        raise ConfigError(msg)
    return tuple(question.options)


def _target_vector(question: Question, target: Mapping[str, float]) -> tuple[float, ...]:
    """`target` as a distribution over `label_order`; bool targets are two-way."""
    if question.type == "bool":
        if not {"true", "false"} & target.keys():
            msg = f"{question.id}: bool training target has neither true nor false"
            raise ConfigError(msg)
        p_true = float(target.get("true", 1.0 - target.get("false", 0.0)))
        return p_true, 1.0 - p_true
    values = [float(target.get(label, 0.0)) for label in label_order(question)]
    total = sum(values)
    if total <= 0.0:
        msg = f"{question.id}: training target has no mass on the question's labels"
        raise ConfigError(msg)
    return tuple(value / total for value in values)


@dataclass(frozen=True)
class _Row:
    text: str
    question: str
    target: tuple[float, ...]
    weight: float


def _rows(table: pa.Table, questions: QuestionSet) -> list[_Row]:
    records = table.select(["text", "question", "target", "weight"]).to_pylist()
    return [
        _Row(
            r["text"],
            r["question"],
            _target_vector(questions.get(r["question"]), dict(r["target"])),
            r["weight"],
        )
        for r in records
    ]


def _lr_factor(step: int, total: int, warmup: float) -> float:
    """Linear warmup over `warmup` of `total` optimizer steps, then linear decay to 0."""
    warm = max(1, math.ceil(total * warmup))
    if step <= warm:
        return step / warm
    return max(0.0, (total - step) / max(1, total - warm))


def _micro_batches(n: int, hyper: TrainHyper, epoch: int) -> list[list[int]]:
    order = list(range(n))
    random.Random(f"{hyper.seed}:{epoch}").shuffle(order)  # noqa: S311 - data order, not security
    return [order[i : i + hyper.micro_batch] for i in range(0, n, hyper.micro_batch)]


def _prepare(
    agent: object, hyper: TrainHyper, device: str
) -> tuple[torch.nn.Module, torch.optim.Optimizer]:
    """Move the model to `device`, enable checkpointing, build AdamW with two groups."""
    import torch  # noqa: PLC0415 - lazy: importing this module must never load torch

    module = getattr(agent, "model", agent)  # V-11: the agent's torch module
    if not isinstance(module, torch.nn.Module):
        msg = "the Laya agent exposes no torch module"
        raise ConfigError(msg)
    module.to(device)
    if hyper.head_checkpointing:
        module.head_checkpointing = True  # type: ignore[assignment]  # V-11: design 03 §5.8 flag
    enable = getattr(module, "gradient_checkpointing_enable", None)  # V-11: encoder hook
    if hyper.gradient_checkpointing and callable(enable):
        enable()
    groups: dict[str, list[torch.nn.Parameter]] = {"encoder": [], "head": []}
    for name, param in module.named_parameters():  # V-11: head parameters are `head.*`
        if param.requires_grad:
            groups["head" if name.startswith("head") else "encoder"].append(param)
    lrs = {"encoder": hyper.lr_encoder, "head": hyper.lr_head}
    params = [{"params": ps, "lr": lrs[key]} for key, ps in groups.items() if ps]
    return module, torch.optim.AdamW(params, weight_decay=hyper.weight_decay)


def _side_files(init_dir: Path) -> list[Path]:
    """Config and tokenizer files copied next to the new weights; the config is required."""
    side = sorted({p for pattern in _SIDE_FILES for p in init_dir.glob(pattern) if p.is_file()})
    if not any(p.name == _SIDE_FILES[0] for p in side):
        msg = f"{_SIDE_FILES[0]} missing from the initial model directory"
        raise ConfigError(msg)
    return side


def _log_probs(logits: torch.Tensor) -> torch.Tensor:
    """Log-softmax over labels; a single `noul` logit becomes the two-way (true, false)."""
    import torch  # noqa: PLC0415 - lazy import

    if logits.shape[-1] == 1:
        logits = torch.cat([logits, torch.zeros_like(logits)], dim=-1)
    return torch.log_softmax(logits.float(), dim=-1)


class SftLoop:
    """One `SoftLabelSftTrainer.train` call: loop state, checkpoints and outputs."""

    def __init__(
        self, data: TrainingSet, hyper: TrainHyper, ctx: JobContext, check_every: int
    ) -> None:
        self.hyper, self.ctx, self.check_every = hyper, ctx, check_every
        self.train_rows = _rows(data.train, data.questions)
        self.val_rows = _rows(data.val, data.questions)
        if not self.train_rows or not self.val_rows:
            msg = "training set needs training and validation rows"
            raise ConfigError(msg)
        asked = {row.question for row in (*self.train_rows, *self.val_rows)}
        self.wire = {qid: to_wire_questions([data.questions.get(qid)]) for qid in asked}
        per_epoch = math.ceil(
            math.ceil(len(self.train_rows) / hyper.micro_batch) / hyper.accumulation
        )
        self.total_steps = hyper.epochs * per_epoch
        self.state: dict[str, Any] = {
            "epochs_done": 0, "step": 0, "global_step": 0, "best_val_nll": None,
            "best_epoch": 0, "bad_epochs": 0, "finished": False, "elapsed_s": 0.0,
            "seed": hyper.seed, "data_sha256": data.sha256,
        }  # fmt: skip
        self.started = clock.monotonic()

    def execute(self, agent: object, device: str, dirs: tuple[Path, Path]) -> Outcome:
        """Train (resuming from the latest checkpoint); returns (epochs, best NLL, files)."""
        import torch  # noqa: PLC0415 - lazy import

        init_dir, out_dir = dirs
        side = _side_files(init_dir)
        torch.manual_seed(self.hyper.seed)
        self.agent: Any = agent
        self.device = device
        self.module, self.optimizer = _prepare(agent, self.hyper, device)
        self.base_lrs = [group["lr"] for group in self.optimizer.param_groups]
        self.root = out_dir / "checkpoints"
        latest = ckpt.latest_checkpoint(self.root)
        if latest is not None:
            if ckpt.read_state(latest).get("data_sha256") != self.state["data_sha256"]:
                msg = f"checkpoint {latest.name} was trained on other data; use a new out_dir"
                raise ConfigError(msg)
            self.state.update(ckpt.load_checkpoint(latest, (self.module, self.optimizer), device))
        self.started = clock.monotonic()
        while not self.state["finished"]:
            epoch = self.state["epochs_done"] + 1
            self._train_epoch(epoch)
            self._end_epoch(epoch, self._val_nll())
        return self._outputs(side, out_dir)

    def _progress(self) -> dict[str, Any]:
        elapsed = self.state["elapsed_s"] + clock.monotonic() - self.started
        return {**self.state, "elapsed_s": elapsed}

    def _save(self, name: str) -> None:
        ckpt.save_checkpoint(self.root, name, (self.module, self.optimizer), self._progress())

    def _train_epoch(self, epoch: int) -> None:
        self.module.train()
        batches = _micro_batches(len(self.train_rows), self.hyper, epoch)
        accumulation = self.hyper.accumulation
        for index in range(self.state["step"], len(batches)):
            start = index - index % accumulation  # the accumulation group of this micro batch
            group = batches[start : start + accumulation]
            group_weight = sum(self.train_rows[i].weight for batch in group for i in batch)
            self._micro_step([self.train_rows[i] for i in batches[index]], 1.0 / group_weight)
            if (index + 1) % accumulation == 0 or index + 1 == len(batches):
                self._optimizer_step(epoch, index + 1)

    def _micro_step(self, rows: list[_Row], scale: float) -> None:
        """Backward of this micro batch's weighted KL sum over its group's total weight."""

        def forward_backward(chunk: Sequence[_Row]) -> list[float]:
            loss = self._divergence(chunk, nll=False) * scale
            loss.backward()  # type: ignore[no-untyped-call]  # untyped in the torch stubs
            return [float(loss.detach())]

        run_batches_with_oom_backoff(
            rows, forward_backward, start_batch=len(rows), fault_name="decider.batch"
        )

    def _divergence(self, rows: Sequence[_Row], *, nll: bool) -> torch.Tensor:
        """Sum over rows of weight * KL(target || softmax(logits)), or weight * NLL when `nll`."""
        import torch  # noqa: PLC0415 - lazy import

        total = torch.zeros((), device=self.device)
        for qid in sorted({row.question for row in rows}):
            group = [row for row in rows if row.question == qid]
            kind = torch.device(self.device).type
            with torch.autocast(device_type=kind, dtype=torch.bfloat16, enabled=self.hyper.bf16):
                # V-11: training-mode per-question logits, frozen in tests/support/fake_laya.py
                logits = self.agent.question_logits(
                    [r.text for r in group], self.wire[qid], max_len=self.hyper.max_len
                )[qid]
            log_p = _log_probs(logits)
            target = torch.tensor([row.target for row in group], device=self.device)
            per_row = -(target * log_p).sum(dim=-1)
            if not nll:
                per_row = per_row + torch.xlogy(target, target).sum(dim=-1)
            weights = torch.tensor([row.weight for row in group], device=self.device)
            total = total + (weights * per_row).sum()
        return total

    def _optimizer_step(self, epoch: int, micro_done: int) -> None:
        self.state["global_step"] += 1
        factor = _lr_factor(self.state["global_step"], self.total_steps, self.hyper.warmup)
        for group, base in zip(self.optimizer.param_groups, self.base_lrs, strict=True):
            group["lr"] = base * factor
        self.optimizer.step()
        self.optimizer.zero_grad(set_to_none=True)
        self.state["step"] = micro_done
        if self.state["global_step"] % self.check_every == 0:
            self.ctx.heartbeat("train")
            if self.ctx.should_yield():
                self._save(f"epoch-{epoch}-step-{micro_done}")
                stage = "train"
                raise YieldRequested(stage)

    def _val_nll(self) -> float:
        import torch  # noqa: PLC0415 - lazy import

        self.module.eval()
        with torch.no_grad():
            parts = run_batches_with_oom_backoff(
                self.val_rows, lambda chunk: [float(self._divergence(chunk, nll=True))],
                start_batch=self.hyper.micro_batch, fault_name="decider.batch",
            )  # fmt: skip
        return sum(parts) / sum(row.weight for row in self.val_rows)

    def _end_epoch(self, epoch: int, val_nll: float) -> None:
        state = self.state
        best = state["best_val_nll"]
        if best is None or val_nll < best:
            state.update(best_val_nll=val_nll, best_epoch=epoch, bad_epochs=0)
        else:
            state["bad_epochs"] += 1
        capped = self._progress()["elapsed_s"] >= self.hyper.wall_clock_cap_s
        done = epoch >= self.hyper.epochs or state["bad_epochs"] >= self.hyper.patience
        state.update(epochs_done=epoch, step=0, finished=done or capped)
        self._save(f"epoch-{epoch}")
        ckpt.drop_partials(self.root)
        if capped and not done:
            _log.warning(
                "enrich.distill.wall_clock_cap", epochs_run=epoch, best_epoch=state["best_epoch"]
            )

    def _outputs(self, side: list[Path], out_dir: Path) -> Outcome:
        """Best epoch's weights plus the config and tokenizer files into `out_dir`."""
        from safetensors.torch import save_model  # noqa: PLC0415 - lazy import

        ckpt.load_weights(self.root / f"epoch-{self.state['best_epoch']}", self.module, self.device)
        save_model(self.module, str(out_dir / ckpt.MODEL_FILE))
        for path in side:
            shutil.copyfile(path, out_dir / path.name)
        files = (out_dir / ckpt.MODEL_FILE, *(out_dir / p.name for p in side))
        return self.state["epochs_done"], float(self.state["best_val_nll"]), files
