"""``herness deploy render`` and the deploy history (impl 10 U10-78, U10-79; design 10 §5.6, §7.4).

``render_env`` is the only place secrets reach the containers (TH10-28): the env file is written
inside WSL from stdin by one fixed shell script, root-owned with mode 600, and the config value
``env_file`` enters only as the positional ``$1`` (TH10-05). Callers see key names, never values.
T10-25 adds ``class_up``, ``class_down``, ``rollback_class`` and ``prune`` here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final

from herness.admin import wsl
from herness.core import time as clock
from herness.core.audit import append_jsonl_locked, audit, log_lock
from herness.core.config import HernessConfig, config_hash, get_config
from herness.core.errors import ConfigError, FatalError
from herness.core.ids import canonical_json, sha256_hex
from herness.core.logging import get_logger
from herness.core.secrets import resolve

__all__ = ["ENV_KEYS", "SCRIPT", "DeployHistory", "render_env"]

# The fixed script of U10-79 step 4; no config value is ever part of its text.
SCRIPT: Final = (
    'umask 077; cat > "$1.tmp" && chown root:root "$1.tmp" && chmod 600 "$1.tmp" '
    '&& mv "$1.tmp" "$1"'
)
ENV_KEYS: Final = (
    "REASONING_IMAGE", "REASONING_PORT", "REASONING_MODEL", "REASONING_REVISION",
    "REASONING_SERVED_NAME", "REASONING_GPU_UTIL", "REASONING_MAX_MODEL_LEN",
    "REASONING_TOOL_PARSER", "REASONING_PARSER", "OPENJEV_IMAGE", "OPENJEV_PORT",
    "OPENJEV_MODEL", "OPENJEV_GPU_UTIL", "OPENJEV_MAX_NUM_SEQS", "OPENJEV_CANVAS",
    "LARGE_IMAGE", "LARGE_PORT", "LARGE_GGUF", "LARGE_CTX", "LARGE_GPU_LAYERS", "MODEL_ROOT",
    "VLLM_API_KEY", "OPENJEV_API_KEY",
)  # fmt: skip
_RENDER_TIMEOUT_S: Final = 30
_BAD_CHARS: Final = ("\n", "\r", "\0")
_log = get_logger("admin.deploy")


def _deploy_dir(cfg: HernessConfig) -> Path:
    return Path(cfg.paths.data) / "deploy"


# --- U10-79 render_env -----------------------------------------------------------------------


def _env_values(cfg: HernessConfig) -> dict[str, str]:
    """Step 1: the 23 keys; the two secrets are resolved here and live only in this dict."""
    r, o, lg = cfg.deploy.reasoning, cfg.deploy.openjev, cfg.deploy.large
    values: dict[str, object] = {
        "REASONING_IMAGE": r.image,
        "REASONING_PORT": r.port,
        "REASONING_MODEL": r.model,
        "REASONING_REVISION": r.revision,
        "REASONING_SERVED_NAME": r.served_name,
        "REASONING_GPU_UTIL": r.gpu_memory_utilization,
        "REASONING_MAX_MODEL_LEN": r.max_model_len,
        "REASONING_TOOL_PARSER": r.tool_call_parser,
        "REASONING_PARSER": r.reasoning_parser,
        "OPENJEV_IMAGE": o.image,
        "OPENJEV_PORT": o.port,
        "OPENJEV_MODEL": o.model,
        "OPENJEV_GPU_UTIL": o.gpu_util,
        "OPENJEV_MAX_NUM_SEQS": o.max_num_seqs,
        "OPENJEV_CANVAS": o.canvas,
        "LARGE_IMAGE": lg.image,
        "LARGE_PORT": lg.port,
        "LARGE_GGUF": lg.gguf,
        "LARGE_CTX": lg.ctx,
        "LARGE_GPU_LAYERS": lg.gpu_layers,
        "MODEL_ROOT": cfg.deploy.model_root,
        "VLLM_API_KEY": resolve("vllm.api_key").get_secret_value(),
        "OPENJEV_API_KEY": resolve("OPENJEV_API_KEY").get_secret_value(),
    }
    return {key: str(value) for key, value in values.items()}


def _env_content(values: dict[str, str]) -> bytes:
    """Steps 2-3: refuse line breaks and NUL (key named, value never), then sorted lines."""
    for key, value in values.items():
        if any(ch in value for ch in _BAD_CHARS):
            msg = f"env value for {key} invalid"
            raise ConfigError(msg)
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode("utf-8")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # no stray temp file next to rendered.json
        raise


def render_env(cfg: HernessConfig, *, actor: str) -> dict[str, str]:
    """``herness deploy render`` (U10-79): write ``deploy.env_file`` in WSL; key -> ``"set"``."""
    cfg.deploy.require_pinned()  # precondition: U10-07 deploy-time pin rules
    values = _env_values(cfg)
    content = _env_content(values)
    env_file = cfg.deploy.env_file
    result = wsl.run_wsl(
        cfg.deploy.wsl_distro,
        ["sh", "-c", SCRIPT, "sh", env_file],
        user="root",
        stdin=content,
        timeout_s=_RENDER_TIMEOUT_S,
        fixed_script=SCRIPT,
    )
    if result.returncode != 0:  # stderr is not echoed: it is free text from inside WSL
        msg = f"deploy render failed: exit {result.returncode}"
        raise FatalError(msg, hint="check the WSL distro and its root account")
    record = {
        "config_hash": config_hash(cfg),
        "env_sha256": sha256_hex(content),
        "ts": clock.format_utc(clock.now()),
    }
    _write_atomic(_deploy_dir(cfg) / "rendered.json", canonical_json(record) + "\n")
    audit("admin_action", actor, action="deploy_render", target=env_file)
    keys = sorted(values)
    _log.info("deploy.render.completed", env_file=env_file, keys=keys)
    return dict.fromkeys(keys, "set")


# --- U10-78 DeployHistory --------------------------------------------------------------------


class DeployHistory:
    """Promoted pin sets per GPU class, one canonical JSON line each (U10-78, design 10 §5.6.5)."""

    def __init__(self, path: Path | None = None) -> None:
        """``path`` defaults to ``<paths.data>/deploy/history.jsonl``; the lock sits beside it."""
        self.path = path if path is not None else _deploy_dir(get_config()) / "history.jsonl"
        self._lock = self.path.parent / ".deploy.lock"

    def _entries(self, gpu_class: str) -> list[dict[str, str]]:
        """Pin sets of ``gpu_class``, newest first; unreadable lines are skipped."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        found: list[dict[str, str]] = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("gpu_class") != gpu_class:
                continue
            if isinstance(pins := entry.get("pins"), dict):
                found.append({str(k): str(v) for k, v in pins.items()})
        return found

    def record(self, gpu_class: str, pins: dict[str, str], config_hash: str, now: datetime) -> None:
        """Append ``pins`` unless they equal the class's newest entry; ``StoreBusy`` on lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": clock.format_utc(now),
            "gpu_class": gpu_class,
            "pins": dict(pins),
            "config_hash": config_hash,
        }
        data = (canonical_json(line) + "\n").encode("utf-8")
        with log_lock(self._lock):
            newest = next(iter(self._entries(gpu_class)), None)
            if newest == dict(pins):
                return
            append_jsonl_locked(self.path, lambda _prev: data, lock_path=self._lock, lock_held=True)

    def last_promoted(self, gpu_class: str, n: int = 2) -> list[dict[str, str]]:
        """The newest ``n`` distinct pin sets of ``gpu_class``, newest first."""
        distinct: list[dict[str, str]] = []
        for pins in self._entries(gpu_class):
            if pins not in distinct:
                distinct.append(pins)
        return distinct[: max(n, 0)]
