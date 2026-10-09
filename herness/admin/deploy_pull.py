"""``herness deploy pull``: firewall window, image and weight pulls, verification (impl 10
U10-81 to U10-83, U10-92; design 10 §3.7, §5.6.3, §9.2; ENG §5.6).

The WSL VM's outbound traffic is allowed only inside ``firewall_window``, after every model
service is stopped (TH10-45), and is set back to ``Block`` and read back on every exit path,
including a timer for a stalled body (TH10-39). Every image digest, weight blob and the GGUF is
verified fail-closed before anything is used: a missing or garbled check result is a mismatch
(TH10-30, TH10-33 to TH10-35). Every program runs through ``herness.admin.wsl`` (no shell
except the one fixed ``refs/main`` script with positional arguments, TH10-05).
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, cast

from herness.admin import _pull_verify as pv
from herness.admin import wsl
from herness.core import egress
from herness.core import time as clock
from herness.core.audit import audit, log_lock
from herness.core.config import HernessConfig, get_config
from herness.core.egress import EgressGuard, registry_host
from herness.core.errors import ConfigError, FatalError, PermissionDenied, StoreBusy
from herness.core.jobs import worker_alive
from herness.core.logging import get_logger

__all__ = ["REFS_SCRIPT", "WSL_VM_CREATOR_ID", "firewall_window", "pull_images", "pull_weights"]
__all__ += ["run_pull", "verify_weights"]

WSL_VM_CREATOR_ID: Final = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"  # verification V-20
REFS_SCRIPT: Final = 'printf %s "$1" > "$2"'  # U10-83 step 3: the fixed script, $1/$2 only
MAX_WINDOW_MINUTES: Final = 120
_MINUTE_S = 60.0  # module attributes so tests shorten the timer and the retry delay
_RETRY_DELAY_S = 2.0
_PS_TIMEOUT_S: Final = 30
_BLOCK_VALUES: Final = ("Block", 4)  # rendered name or NetSecurity Action enum integer (V-20)
_BLOCK_CMD: Final = (
    f"Set-NetFirewallHyperVVMSetting -Name '{WSL_VM_CREATOR_ID}' -DefaultOutboundAction Block"
)
type WeightClass = Literal["reasoning", "decider", "large"]
_WEIGHT_CLASSES: Final[tuple[WeightClass, ...]] = ("reasoning", "decider", "large")
_log = get_logger("admin.deploy")


# --- U10-81 firewall_window ------------------------------------------------------------------


def _require_elevated() -> None:
    """Precondition of U10-81: a Windows host and an elevated shell."""
    elevated = os.name == "nt" and bool(cast("Any", ctypes).windll.shell32.IsUserAnAdmin())
    if not elevated:
        msg = "deploy pull needs an elevated Windows shell"
        raise PermissionDenied(msg)


def _set_outbound(action: Literal["Allow", "Block"]) -> wsl.CmdResult:
    params = {"Name": WSL_VM_CREATOR_ID, "DefaultOutboundAction": action}
    return wsl.run_powershell("Set-NetFirewallHyperVVMSetting", params, timeout_s=_PS_TIMEOUT_S)


def _outbound_blocked() -> bool:
    """Read the setting back; only a parsed ``Block`` (name or enum integer) counts."""
    params = {"Name": WSL_VM_CREATOR_ID}
    result = wsl.run_powershell("Get-NetFirewallHyperVVMSetting", params, timeout_s=_PS_TIMEOUT_S)
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return False
    items = data if isinstance(data, list) else [data]
    return bool(items) and all(
        isinstance(i, dict) and i.get("DefaultOutboundAction") in _BLOCK_VALUES for i in items
    )


def _block_twice() -> bool:
    """Set ``Block`` and read it back, once more after 2 s; a runner failure is "not closed"."""
    for attempt in range(2):
        if attempt:
            clock.sleep(_RETRY_DELAY_S)
        with suppress(Exception):
            _set_outbound("Block")
            if _outbound_blocked():
                return True
    return False


class _Window:
    """One open window: ``close`` is serialised between the body thread and the timer."""

    def __init__(self, actor: str) -> None:
        self.actor = actor
        self.expired = False
        self._lock = threading.Lock()

    def close(self) -> None:
        """Step 4: set ``Block``, read it back, retry once after 2 s; else CRITICAL and raise."""
        with self._lock:
            try:
                closed = _block_twice()
            except BaseException:  # e.g. Ctrl+C in the retry delay: CRITICAL, one more Block
                _log.critical(
                    "deploy.firewall.restore_failed", actor=self.actor, command=_BLOCK_CMD
                )
                with suppress(Exception):
                    _set_outbound("Block")
                raise
            if closed:
                _log.info("deploy.firewall.window_closed", actor=self.actor)
                return
            _log.critical("deploy.firewall.restore_failed", actor=self.actor, command=_BLOCK_CMD)
            msg = "firewall window could not be closed; run the Block command manually"
            raise FatalError(msg, hint=_BLOCK_CMD)

    def expire(self) -> None:
        """Timer: the body overran ``max_minutes``; close now (the body is still running)."""
        self.expired = True
        _log.warning("deploy.firewall.window_expired", actor=self.actor)
        with suppress(FatalError):  # logged CRITICAL; the body's ``finally`` closes again
            self.close()


@contextmanager
def firewall_window(*, max_minutes: int = MAX_WINDOW_MINUTES, actor: str) -> Iterator[None]:
    """Allow WSL outbound for the body only; ``Block`` read back on every exit (U10-81)."""
    if not 0 < max_minutes <= MAX_WINDOW_MINUTES:
        msg = f"max_minutes must be in (0, {MAX_WINDOW_MINUTES}]"
        raise ConfigError(msg)
    _require_elevated()
    lock = Path(get_config().paths.data) / "locks" / "deploy-pull.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with log_lock(lock):
        window = _Window(actor)
        timer = threading.Timer(max_minutes * _MINUTE_S, window.expire)
        timer.daemon = True
        try:
            if _set_outbound("Allow").returncode != 0:
                msg = "firewall window could not be opened"
                raise FatalError(msg)
            _log.warning("deploy.firewall.window_opened", actor=actor, max_minutes=max_minutes)
            timer.start()
            yield
        finally:
            timer.cancel()
            window.close()  # also after a failed Allow: the setting may be half applied
        if window.expired:
            msg = f"firewall window expired after {max_minutes} minutes; pull aborted"
            raise FatalError(msg)


# --- U10-82 pull_images ----------------------------------------------------------------------


def _fail(message: str, **fields: str) -> NoReturn:
    """Log ``deploy.pull.failed`` (reason codes only) and raise ``FatalError``."""
    _log.error("deploy.pull.failed", **fields)
    raise FatalError(message)


def _repo_digests(stdout: str) -> list[str]:
    """``RepoDigests`` of a ``docker image inspect`` JSON array; garbled output gives none."""
    try:
        data = json.loads(stdout)
        digests = data[0]["RepoDigests"] if isinstance(data, list) and data else []
    except (ValueError, LookupError, TypeError):
        return []
    return [d for d in digests if isinstance(d, str)] if isinstance(digests, list) else []


def pull_images(cfg: HernessConfig, *, guard: EgressGuard) -> list[dict[str, str]]:
    """Pull the three pinned images by digest and check ``RepoDigests`` (U10-82, TH10-33)."""
    cfg.deploy.require_pinned()
    distro, statuses = cfg.deploy.wsl_distro, []
    for service in ("reasoning", "openjev", "large"):
        ref: str = getattr(cfg.deploy, service).image
        guard.check(f"https://{registry_host(ref)}/v2/", b"", "model_download", "none")
        if wsl.run_wsl(distro, ["docker", "pull", ref], timeout_s=3600).returncode != 0:
            _fail(f"image pull failed: {service}", service=service, reason="image_pull")
        # JSON parsed here: ``{{json .RepoDigests}}`` fails the U10-77 pattern (T10-24 ruling)
        inspect = wsl.run_wsl(distro, ["docker", "image", "inspect", ref], timeout_s=60)
        digest = ref.rpartition("@")[2]
        found = _repo_digests(inspect.stdout) if inspect.returncode == 0 else []
        if not any(entry.endswith("@" + digest) for entry in found):
            msg = f"image digest mismatch: {service}"
            _fail(msg, service=service, reason="image_digest")
        _log.info("deploy.pull.image_verified", service=service, digest=digest)
        statuses.append({"service": service, "digest": digest, "status": "ok"})
    return statuses


# --- U10-83 pull_weights, verify_weights -----------------------------------------------------


def verify_weights(cfg: HernessConfig, gpu_class: WeightClass, *, full: bool) -> dict[str, str]:
    """Check one class's weights on disk; ``status`` ``ok`` or a reason (U10-83, TH10-30/35)."""
    if gpu_class not in _WEIGHT_CLASSES:
        msg = "gpu_class must be reasoning, decider or large"
        raise ConfigError(msg)
    cfg.deploy.require_pinned(["openjev" if gpu_class == "decider" else gpu_class])
    if gpu_class == "large":
        large = cfg.deploy.large
        gguf = f"{cfg.deploy.model_root}/gguf/{large.gguf}"
        expected = {large.gguf: large.sha256}
        status, manifest = pv.check_files(cfg, {large.gguf: gguf}, expected, full=full)
        name = f"large-{large.sha256[:12]}"
    else:
        _model, revision, repo = pv.hf_repo(cfg, gpu_class)
        snapshot = f"{repo}/snapshots/{revision}"
        if pv.run(cfg, ["test", "-d", snapshot]).returncode != 0:
            return {"status": "snapshot_missing", "files": "0"}
        links = pv.snapshot_links(cfg, snapshot, repo)
        if isinstance(links, str):
            return {"status": links, "files": "0"}
        status, manifest = pv.check_snapshot(cfg, links, full=full)
        name = f"{gpu_class}-{revision}"
    if status == "ok":
        pv.write_manifest(cfg, name, manifest)
    return {"status": status, "files": str(len(manifest))}


def pull_weights(cfg: HernessConfig, *, guard: EgressGuard) -> list[dict[str, str]]:
    """Download pinned weight revisions, pin OpenJev's ``refs/main``, verify all (U10-83)."""
    cfg.deploy.require_pinned()
    deploy = cfg.deploy
    for gpu_class in _WEIGHT_CLASSES[:2]:  # reasoning, decider
        model, revision, repo = pv.hf_repo(cfg, gpu_class)
        url = f"https://huggingface.co/{model}/resolve/{revision}"
        guard.check(url, b"", "model_download", "none")
        argv = [
            "docker", "run", "--rm", "--entrypoint", "huggingface-cli",
            "-e", "HF_HUB_OFFLINE=0", "-e", "HF_HUB_DISABLE_TELEMETRY=1",
            "-v", deploy.model_root + ":/root/.cache/huggingface", deploy.reasoning.image,
            "download", model, "--revision", revision,
        ]  # fmt: skip
        if pv.run(cfg, argv, timeout_s=7200).returncode != 0:
            msg = f"weight download failed: {gpu_class}"
            _fail(msg, gpu_class=gpu_class, reason="weight_download")
        if gpu_class == "decider":  # V-15: offline resolution picks the pinned snapshot
            refs = ["sh", "-c", REFS_SCRIPT, "sh", revision, f"{repo}/refs/main"]
            done = wsl.run_wsl(
                deploy.wsl_distro, refs, user="root", timeout_s=30, fixed_script=REFS_SCRIPT
            )
            if done.returncode != 0:
                _fail("weight refs write failed: decider", reason="refs_main")
    results = []
    for gpu_class in _WEIGHT_CLASSES:
        result = verify_weights(cfg, gpu_class, full=True)
        if result["status"] != "ok":
            msg = f"weight verification failed: {gpu_class}: {result['status']}"
            _fail(msg, gpu_class=gpu_class, reason=result["status"])
        results.append({"gpu_class": gpu_class, **result})
    return results


# --- U10-92 run_pull -------------------------------------------------------------------------


def _stop_services(cfg: HernessConfig) -> None:
    """Step 1: stop every compose service so no model server runs while outbound is open."""
    gpu = cfg.resilience.resilience.gpu
    profiles = ["--profile", "reasoning", "--profile", "decider", "--profile", "large"]
    argv = [*gpu.compose_cmd, "-f", gpu.compose_file, *profiles, "stop"]
    if wsl.run_cmd(argv, timeout_s=gpu.stop_timeout_s).returncode != 0:
        msg = "could not stop the model services; the firewall window was not opened"
        raise FatalError(msg)
    _log.info("deploy.pull.services_stopped")


def run_pull(cfg: HernessConfig, *, allow_download: bool, actor: str) -> dict[str, Any]:
    """``herness deploy pull --allow-download`` (U10-92): stop, window, pull, verify, audit."""
    if not allow_download:
        msg = "deploy pull requires --allow-download"
        raise ConfigError(msg)
    cfg.deploy.require_pinned()
    if worker_alive():
        msg = "stop the worker before pulling"
        raise StoreBusy(msg)
    _stop_services(cfg)
    guard = egress.get_guard()
    with guard.download_window(allow_download=True, actor=actor), firewall_window(actor=actor):
        images = pull_images(cfg, guard=guard)
        weights = pull_weights(cfg, guard=guard)
    after = {c: verify_weights(cfg, c, full=False) for c in _WEIGHT_CLASSES}  # window closed
    for gpu_class, result in after.items():
        if result["status"] != "ok":
            msg = f"weight check after the window failed: {gpu_class}: {result['status']}"
            _fail(msg, gpu_class=gpu_class, reason=result["status"])
    audit("admin_action", actor, action="deploy_pull", target="complete")
    _log.info("deploy.pull.completed", actor=actor, images=len(images), weights=len(weights))
    return {"images": images, "weights": weights, "verified": after}
