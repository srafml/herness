"""Compose, GPU-lock and health helpers of ``herness deploy up|down|rollback`` (impl 10 U10-84
step 3, U10-85 step 1).

Size-forced private sibling of ``herness.admin.deploy`` (T10-25), which keeps the public API.
Compose runs as ``resilience.gpu.compose_cmd + ["-f", compose_file, ...]`` through
``herness.admin.wsl.run_cmd`` (argument list, no shell); health GETs go only through
``herness.core.egress.loopback_http_client`` (R-06). Nothing here pulls or downloads.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.admin import wsl
from herness.core import egress
from herness.core import time as clock
from herness.core.config import HernessConfig
from herness.core.errors import ConfigError, ModelUnavailable, StoreBusy
from herness.core.jobs import GpuLock
from herness.core.secrets import resolve

if TYPE_CHECKING:
    from pydantic import SecretStr

    from herness.core.resilience.settings import GpuSettings, ServiceSettings

__all__ = ["POLL_S", "compose", "gpu_lock", "image_on_disk", "up_compose"]

POLL_S: Final = 5.0  # U10-84: worker state and health are polled every 5 s
_HEALTH_TIMEOUT_S: Final = 10.0
_UP_TIMEOUT_S: Final = 600  # ``up -d`` of local images only; health has its own deadline
_INSPECT_TIMEOUT_S: Final = 60
_OPENJEV_KEY: Final = "secret:OPENJEV_API_KEY"  # pragma: allowlist secret - reference (R-53)


@contextmanager
def gpu_lock(cfg: HernessConfig) -> Iterator[None]:
    """The impl 08 GPU owner lock ``data/locks/gpu.lock``, without waiting; held: StoreBusy."""
    lock = GpuLock(Path(cfg.paths.data) / "locks" / "gpu.lock")
    try:
        lock.__enter__()
    except ConfigError:
        msg = "GPU lock held"
        raise StoreBusy(msg) from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def compose(gpu: GpuSettings, tail: list[str], timeout_s: float) -> None:
    """One compose command; a non-zero exit is ``ModelUnavailable`` (output not echoed)."""
    done = wsl.run_cmd([*gpu.compose_cmd, "-f", gpu.compose_file, *tail], timeout_s=timeout_s)
    if done.returncode != 0:
        verb = "stop" if tail[-1] == "stop" else "up"
        msg = f"compose {verb} failed: exit {done.returncode}"
        raise ModelUnavailable(msg)


def _healthy(svc: ServiceSettings, bearer: SecretStr | None) -> bool:
    try:
        factory = egress.loopback_http_client  # module attribute, resolved per call (R-06)
        with factory(svc.url, timeout_s=_HEALTH_TIMEOUT_S, bearer=bearer) as client:
            status = int(client.get(svc.health.path).status_code)
    except Exception:  # noqa: BLE001 - any failure reads as not yet healthy
        return False
    return 200 <= status < 300  # noqa: PLR2004 - the 2xx range


def _await_healthy(name: str, svc: ServiceSettings) -> None:
    """Poll the service's health URL every 5 s until healthy or its ``start_timeout_s``."""
    ref = svc.health.bearer_secret or (_OPENJEV_KEY if name == "openjev" else None)
    bearer = resolve(ref) if ref else None
    deadline = clock.monotonic() + svc.start_timeout_s
    while not _healthy(svc, bearer):
        if clock.monotonic() >= deadline:
            msg = f"{name} not healthy within start_timeout_s"
            raise ModelUnavailable(msg)
        clock.sleep(POLL_S)


def up_compose(cfg: HernessConfig, gpu_class: str) -> None:
    """U10-84 step 3: under the GPU lock stop the other classes, start this one, await health."""
    gpu = cfg.resilience.resilience.gpu
    services = gpu.classes[gpu_class].services  # type: ignore[index]  # checked by the caller
    with gpu_lock(cfg):
        for other in (c for c in gpu.classes if c != gpu_class):
            compose(gpu, ["--profile", other, "stop"], gpu.stop_timeout_s)
        compose(gpu, ["--profile", gpu_class, "up", "-d", *services], _UP_TIMEOUT_S)
        for name, svc in services.items():
            _await_healthy(name, svc)


def image_on_disk(cfg: HernessConfig, ref: str) -> bool:
    """``docker image inspect`` (never a pull) lists the pinned ``ref`` in ``RepoDigests``."""
    argv = ["docker", "image", "inspect", ref]
    done = wsl.run_wsl(cfg.deploy.wsl_distro, argv, timeout_s=_INSPECT_TIMEOUT_S)
    try:
        digests = json.loads(done.stdout)[0]["RepoDigests"] if done.returncode == 0 else []
    except (ValueError, LookupError, TypeError):
        return False
    pinned = "@" + ref.rpartition("@")[2]  # the U10-82 check: the pinned digest is present
    return isinstance(digests, list) and any(
        isinstance(d, str) and d.endswith(pinned) for d in digests
    )
