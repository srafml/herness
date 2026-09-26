"""Training checkpoints for `laya_trainer` (private helper of U03-131 ... U03-133).

A checkpoint is a directory `checkpoints/<name>/` holding `model.safetensors`,
`optimizer.safetensors` (every optimizer state tensor, keyed `<param index>.<name>`, plus
the torch CPU RNG state as a uint8 tensor under `rng.cpu`) and
`state.json` (progress plus the optimizer's JSON-safe `param_groups`). It is written to a
`.tmp-<name>` directory and renamed into place, so every directory without a leading `.`
is complete. Safetensors only, never pickle (TH03-16).

`torch` and `safetensors` are imported lazily inside functions.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.core.errors import ConfigError

if TYPE_CHECKING:
    import torch

MODEL_FILE: Final = "model.safetensors"
_OPTIMIZER_FILE: Final = "optimizer.safetensors"
_STATE_FILE: Final = "state.json"
_GROUPS_KEY: Final = "optimizer_groups"
_RNG_KEY: Final = "rng.cpu"


def _order(state: Mapping[str, Any]) -> tuple[int, int]:
    return int(state["epochs_done"]), int(state["step"])


def read_state(path: Path) -> dict[str, Any]:
    """The `state.json` of checkpoint `path`. Raises ConfigError when unreadable."""
    try:
        body = json.loads((path / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"checkpoint {path.name} is unreadable"
        raise ConfigError(msg) from exc
    if not isinstance(body, dict):
        msg = f"checkpoint {path.name} is unreadable"
        raise ConfigError(msg)
    return body


def latest_checkpoint(root: Path) -> Path | None:
    """The complete checkpoint with the most progress (epochs done, then micro steps)."""
    if not root.is_dir():
        return None
    complete = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not complete:
        return None
    return max(complete, key=lambda p: _order(read_state(p)))


def save_checkpoint(
    root: Path,
    name: str,
    parts: tuple[torch.nn.Module, torch.optim.Optimizer],
    state: Mapping[str, Any],
) -> Path:
    """Write checkpoint `root/name` atomically (tmp dir, then rename) and return it."""
    from safetensors.torch import save_file, save_model  # noqa: PLC0415 - lazy import

    module, optimizer = parts
    tmp, final = root / f".tmp-{name}", root / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    save_model(module, str(tmp / MODEL_FILE))
    tensors, groups = _optimizer_parts(optimizer)
    save_file(tensors, str(tmp / _OPTIMIZER_FILE))
    body = {**state, _GROUPS_KEY: groups}
    (tmp / _STATE_FILE).write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    return final


def _optimizer_parts(optimizer: torch.optim.Optimizer) -> tuple[dict[str, torch.Tensor], Any]:
    import torch  # noqa: PLC0415 - lazy import

    state_dict = optimizer.state_dict()
    tensors: dict[str, torch.Tensor] = {}
    for index, values in state_dict["state"].items():
        for key, value in values.items():
            tensor = value if isinstance(value, torch.Tensor) else torch.tensor(value)
            tensors[f"{index}.{key}"] = tensor.detach().cpu().contiguous()
    tensors[_RNG_KEY] = torch.get_rng_state()  # torch CPU RNG (dropout) for identical resume
    return tensors, state_dict["param_groups"]


def load_weights(path: Path, module: torch.nn.Module, device: str) -> None:
    """Load `path/model.safetensors` into `module` (strict)."""
    from safetensors.torch import load_model  # noqa: PLC0415 - lazy import

    load_model(module, str(path / MODEL_FILE), strict=True, device=device)


def load_checkpoint(
    path: Path, parts: tuple[torch.nn.Module, torch.optim.Optimizer], device: str
) -> dict[str, Any]:
    """Restore weights, optimizer state and the torch CPU RNG from `path`; returns progress."""
    import torch  # noqa: PLC0415 - lazy import
    from safetensors.torch import load_file  # noqa: PLC0415 - lazy import

    module, optimizer = parts
    state = read_state(path)
    load_weights(path, module, device)
    tensors = load_file(str(path / _OPTIMIZER_FILE))
    torch.set_rng_state(tensors.pop(_RNG_KEY))
    per_param: dict[int, dict[str, Any]] = {}
    for key, tensor in tensors.items():
        index, _, name = key.partition(".")
        per_param.setdefault(int(index), {})[name] = tensor
    groups = state.pop(_GROUPS_KEY)
    optimizer.load_state_dict({"state": per_param, "param_groups": groups})
    return state


def drop_partials(root: Path) -> None:
    """Remove mid-epoch checkpoints (names containing `-step-`) once an epoch is saved."""
    for path in root.glob("epoch-*-step-*"):
        shutil.rmtree(path, ignore_errors=True)
