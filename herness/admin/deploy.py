"""``herness deploy render|up|down|rollback|prune`` and the deploy history (impl 10 U10-78,
U10-79, U10-84, U10-85; design 10 §3.7, §5.6, §7.4).

``render_env`` is the only place secrets reach the containers (TH10-28): the env file is written
inside WSL from stdin by one fixed shell script, root-owned with mode 600, and the config value
``env_file`` enters only as the positional ``$1`` (TH10-05). Callers see key names, never values.
``class_up`` / ``class_down`` defer to a live worker's arbiter, else hold ``data/locks/gpu.lock``
(TH10-31) and are audited (TH10-32). ``rollback_class`` uses only what is on disk (TH10-46);
``prune`` keeps the current pins and the two newest promoted pin sets (planning in
``herness.admin._deploy_prune``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from herness.admin import _deploy_compose as dc
from herness.admin import _deploy_prune as dp
from herness.admin import wsl
from herness.admin.deploy_pull import verify_weights
from herness.core import time as clock
from herness.core.audit import append_jsonl_locked, audit, log_lock
from herness.core.config import HernessConfig, config_hash, get_config
from herness.core.errors import ConfigError, FatalError, ModelUnavailable, StoreBusy
from herness.core.ids import canonical_json, sha256_hex
from herness.core.jobs import gpu_state, request_gpu_class, worker_alive
from herness.core.logging import get_logger
from herness.core.secrets import resolve
from herness.core.settings import PinClass

__all__ = ["ENV_KEYS", "SCRIPT", "DeployHistory", "class_down", "class_up", "prune", "render_env"]
__all__ += ["rollback_class"]

type DeployClass = Literal["reasoning", "decider", "large"]

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
_CLASSES: Final[tuple[DeployClass, ...]] = ("reasoning", "decider", "large")
_SECTION: Final[dict[str, PinClass]] = {
    "reasoning": "reasoning",
    "decider": "openjev",
    "large": "large",
}
_WORKER_GRACE_S: Final = 300  # the worker gets start_timeout_s + 300 s to load the class
_ALL: Final = 1_000_000  # ``last_promoted`` bound meaning "every distinct pin set"
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


# --- U10-84 class_up, class_down -------------------------------------------------------------


def _check_class(gpu_class: str) -> None:
    if gpu_class not in _CLASSES:
        msg = "gpu_class must be reasoning, decider or large"
        raise ConfigError(msg)


def _pins(cfg: HernessConfig, gpu_class: str) -> dict[str, str]:
    """The current pin set of a class (U10-78 keys)."""
    if gpu_class == "large":
        lg = cfg.deploy.large
        return {"image": lg.image, "gguf": lg.gguf, "sha256": lg.sha256}
    section = cfg.deploy.reasoning if gpu_class == "reasoning" else cfg.deploy.openjev
    return {"image": section.image, "model": section.model, "revision": section.revision}


def _target(gpu_class: str, pins: dict[str, str]) -> str:
    """Audit target ``<class>:<pinned digest or gguf sha>``."""
    pin = pins["sha256"] if gpu_class == "large" else pins["image"].partition("@")[2]
    return f"{gpu_class}:{pin}"


def _history(cfg: HernessConfig) -> DeployHistory:
    return DeployHistory(_deploy_dir(cfg) / "history.jsonl")


def _require_rendered(cfg: HernessConfig) -> None:
    """Precondition: ``rendered.json`` belongs to this config."""
    try:
        record = json.loads((_deploy_dir(cfg) / "rendered.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if not isinstance(record, dict) or record.get("config_hash") != config_hash(cfg):
        msg = "run herness deploy render first"
        raise ConfigError(msg)


def _up_worker(cfg: HernessConfig, gpu_class: DeployClass) -> bool:
    """Step 2: ask the worker's arbiter and wait for it; False when no worker took it."""
    if request_gpu_class(gpu_class) != "requested":
        return False
    _log.info("deploy.class.requested", via="worker", gpu_class=gpu_class)
    services = cfg.resilience.resilience.gpu.classes[gpu_class].services.values()
    deadline = clock.monotonic() + max(s.start_timeout_s for s in services) + _WORKER_GRACE_S
    while gpu_state().loaded_class() != gpu_class:
        if clock.monotonic() >= deadline:
            msg = f"worker did not load {gpu_class}"
            raise ModelUnavailable(msg)
        clock.sleep(dc.POLL_S)
    return True


def class_up(
    cfg: HernessConfig, gpu_class: DeployClass, *, actor: str, now: datetime
) -> dict[str, str]:
    """``herness deploy up <class>`` (U10-84): via the worker, else under the GPU lock."""
    _check_class(gpu_class)
    cfg.deploy.require_pinned([_SECTION[gpu_class]])
    _require_rendered(cfg)
    pins = _pins(cfg, gpu_class)
    audit("admin_action", actor, action="deploy_up", target=_target(gpu_class, pins))
    via = "worker" if worker_alive() and _up_worker(cfg, gpu_class) else "compose"
    if via == "compose":
        dc.up_compose(cfg, gpu_class)
    _history(cfg).record(gpu_class, pins, config_hash(cfg), now)
    _log.info("deploy.class.up", gpu_class=gpu_class, via=via)
    return {"gpu_class": gpu_class, "via": via, "status": "loaded"}


def class_down(cfg: HernessConfig, gpu_class: DeployClass, *, actor: str) -> dict[str, str]:
    """``herness deploy down <class>`` (U10-84): worker class ``none``, else compose ``stop``."""
    _check_class(gpu_class)
    audit("admin_action", actor, action="deploy_down", target=gpu_class)
    if worker_alive() and request_gpu_class("none") == "requested":
        _log.info("deploy.class.requested", via="worker", gpu_class="none")
        return {"gpu_class": gpu_class, "via": "worker", "status": "requested"}
    gpu = cfg.resilience.resilience.gpu
    with dc.gpu_lock(cfg):
        dc.compose(gpu, ["--profile", gpu_class, "stop"], gpu.stop_timeout_s)
    _log.info("deploy.class.down", gpu_class=gpu_class, via="compose")
    return {"gpu_class": gpu_class, "via": "compose", "status": "stopped"}


# --- U10-85 rollback_class, prune ------------------------------------------------------------


def rollback_class(
    cfg: HernessConfig, gpu_class: DeployClass, *, actor: str, now: datetime
) -> dict[str, str]:
    """``herness deploy rollback <class>`` (U10-85): on-disk check, render, down, up, audit."""
    _check_class(gpu_class)
    pins = _pins(cfg, gpu_class)
    weights = verify_weights(cfg, gpu_class, full=False)
    if weights["status"] != "ok" or not dc.image_on_disk(cfg, pins["image"]):
        msg = "rollback target not on disk; run deploy pull"
        raise ConfigError(msg)
    render_env(cfg, actor=actor)
    class_down(cfg, gpu_class, actor=actor)
    result = class_up(cfg, gpu_class, actor=actor, now=now)
    audit("admin_action", actor, action="deploy_rollback", target=_target(gpu_class, pins))
    return result


def _keep(cfg: HernessConfig, history: DeployHistory) -> dict[str, list[dict[str, str]]]:
    """Step 1: per class the current pins plus the two newest valid promoted pin sets."""
    keep: dict[str, list[dict[str, str]]] = {}
    for cls in _CLASSES:
        promoted = [p for p in history.last_promoted(cls, _ALL) if dp.valid_pins(cls, p)]
        keep[cls] = [_pins(cfg, cls), *promoted[:2]]
    return keep


def _models(history: DeployHistory, keep: dict[str, list[dict[str, str]]]) -> dict[str, set[str]]:
    """Model -> kept revisions, over every model a valid pin set of the HF classes names."""
    revisions: dict[str, set[str]] = {}
    for cls in ("reasoning", "decider"):
        for pins in history.last_promoted(cls, _ALL):
            if dp.valid_pins(cls, pins):
                revisions.setdefault(pins["model"], set())
        for pins in keep[cls]:
            revisions.setdefault(pins["model"], set()).add(pins["revision"])
    return revisions


def _refuse_in_use(cfg: HernessConfig, history: DeployHistory, todo: dp.PrunePlan) -> None:
    """``StoreBusy`` while the loaded class uses an item the plan would remove."""
    loaded = gpu_state().loaded_class()
    if loaded == "swapping":
        msg = "GPU class swap in progress; prune refused"
        raise StoreBusy(msg)
    if loaded not in _CLASSES:
        return
    in_use: set[str] = set()
    valid = [p for p in history.last_promoted(loaded, _ALL) if dp.valid_pins(loaded, p)]
    for pins in [_pins(cfg, loaded), *valid[:1]]:
        in_use.add(pins.get("image", ""))
        if "revision" in pins:
            in_use.add(f"{dp.repo_of(cfg, pins['model'])}/snapshots/{pins['revision']}")
        if "gguf" in pins:
            in_use.add(f"{cfg.deploy.model_root}/gguf/{pins['gguf']}")
    if in_use & todo.items():
        msg = "the loaded GPU class uses an item prune would remove"
        raise StoreBusy(msg)


def prune(cfg: HernessConfig, *, actor: str) -> dict[str, int]:
    """``herness deploy prune`` (U10-85): plan, refuse while in use, remove, audit counts."""
    cfg.deploy.require_pinned()
    history = _history(cfg)
    keep = _keep(cfg, history)
    todo = dp.plan(
        cfg,
        kept_refs={pins["image"] for sets in keep.values() for pins in sets},
        kept_revisions=_models(history, keep),
        kept_gguf={pins["gguf"] for pins in keep["large"]},
    )
    _refuse_in_use(cfg, history, todo)
    counts = dp.execute(cfg, todo)
    summary = ";".join(f"{kind}={n}" for kind, n in counts.items())
    audit("admin_action", actor, action="deploy_prune", target="model_root", counts=summary)
    _log.info("deploy.prune.completed", **counts)
    return counts
