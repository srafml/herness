"""Compose commands, loopback health and warm-up, and VRAM readings (impl 08 U08-78 to U08-80).

`ComposeRunner` is the only place that runs compose (design 08 §5.8 "Commands"): argument
lists with `shell=False`, service names from the configured allowlist (TH08-01). `LoopbackHttp`
talks to the local model servers only through `herness.core.egress.loopback_http_client`
(R-06, D08-06), resolved at call time; this module constructs no HTTP client (TH08-12).
`vram_used_mb` and `wait_vram_free` read the GPU memory in use through `R.gpu.vram_check_cmd`.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Final, assert_never
from urllib.parse import urlsplit

from herness.core import egress, secrets
from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.logging import get_logger

# Redact, then cut to 500 chars: the U08-16 helper, reused as is (controller ruling T08-17).
from herness.core.resilience.classify import _redacted

if TYPE_CHECKING:
    from pydantic import JsonValue, SecretStr

    from herness.core.resilience.settings import GpuSettings, ServiceSettings
    from herness.core.types import GpuClass, ServiceName

__all__ = [
    "COMPOSE_UP_TIMEOUT_S",
    "ComposeRunner",
    "LoopbackHttp",
    "vram_used_mb",
    "wait_vram_free",
]

COMPOSE_UP_TIMEOUT_S: Final = 120
KILL_TIMEOUT_S: Final = 60
PS_TIMEOUT_S: Final = 30
VRAM_CHECK_TIMEOUT_S: Final = 10
STOP_GRACE_S: Final = "60"  # `compose stop -t 60`
BODY_READ_MAX: Final = 65_536  # response bodies are read up to 64 KB and discarded
_SERVICE_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,63}")
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_OPENJEV_WARMUP: Final[dict[str, JsonValue]] = {
    "model": "openjev-latest",
    "state": "warm-up",
    "questions": {
        "warmup": {"type": "noul", "instructions": "Is this text a warm-up request?"},
    },
    "samples": 1,
    "steps": 1,
    "think": 0,
}

_log = get_logger("core.jobs.gpu_services")


def _gpu() -> GpuSettings:
    return get_config().resilience.resilience.gpu


class ComposeRunner:
    """Runs `R.gpu.compose_cmd -f R.gpu.compose_file <verb> ...` (U08-78, TH08-01).

    `gpu` pins the settings (tests, one-off tools); by default `R.gpu` is read at each call.
    """

    def __init__(self, gpu: GpuSettings | None = None) -> None:
        self._pinned = gpu

    def _settings(self) -> GpuSettings:
        return self._pinned if self._pinned is not None else _gpu()

    def up(self, cls: GpuClass, service: ServiceName) -> None:
        """`--profile <cls> up -d <service>`, 120 s timeout."""
        gpu = self._settings()
        _check_class(gpu, cls)
        _check_service(gpu, service)
        tail = ["--profile", cls, "up", "-d", service]
        _run(gpu, tail, ("up", service), COMPOSE_UP_TIMEOUT_S)

    def stop(self, service: ServiceName) -> None:
        """`stop -t 60 <service>`, `R.gpu.stop_timeout_s` timeout."""
        gpu = self._settings()
        _check_service(gpu, service)
        _run(gpu, ["stop", "-t", STOP_GRACE_S, service], ("stop", service), gpu.stop_timeout_s)

    def kill(self, service: ServiceName) -> None:
        """`kill <service>`, 60 s timeout."""
        gpu = self._settings()
        _check_service(gpu, service)
        _run(gpu, ["kill", service], ("kill", service), KILL_TIMEOUT_S)

    def ps(self) -> dict[str, str]:
        """`ps --format json` as service → state, 30 s timeout."""
        out = _run(self._settings(), ["ps", "--format", "json"], ("ps",), PS_TIMEOUT_S)
        return _parse_ps(out)


def _check_class(gpu: GpuSettings, cls: object) -> None:
    if not isinstance(cls, str) or cls not in gpu.classes:
        msg = "compose profile is not a configured GPU class"
        raise ConfigError(msg)


def _check_service(gpu: GpuSettings, service: object) -> None:
    known = isinstance(service, str) and any(service in c.services for c in gpu.classes.values())
    if not known or _SERVICE_RE.fullmatch(str(service)) is None:
        msg = "compose service is not a configured service name"
        raise ConfigError(msg)


def _run(gpu: GpuSettings, tail: list[str], what: tuple[str, ...], timeout_s: float) -> str:
    """Run one compose command; stdout, or `ModelUnavailable` (U08-78 steps 2 and 3)."""
    argv = [*gpu.compose_cmd, "-f", gpu.compose_file, *tail]
    label = " ".join(("compose", *what))
    try:
        done = subprocess.run(  # noqa: S603 - argument list from config and allowlisted names
            argv, shell=False, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError:
        msg = "compose unavailable"
        raise ModelUnavailable(msg) from None
    except subprocess.TimeoutExpired:
        msg = f"{label} timed out"
        raise ModelUnavailable(msg) from None
    if done.returncode != 0:
        verb, service = what[0], what[1] if len(what) > 1 else None
        stderr = _redacted(done.stderr or "")
        _log.warning(
            "jobs.gpu.compose_failed", verb=verb, service=service, rc=done.returncode, stderr=stderr
        )
        msg = f"{label} failed rc={done.returncode}"
        raise ModelUnavailable(msg)
    return done.stdout or ""


def _parse_ps(text: str) -> dict[str, str]:
    """U08-78 step 4: one JSON array, or one JSON object per non-empty line."""
    msg = "compose ps unreadable"
    body = text.strip()
    try:
        if body.startswith("["):
            rows = json.loads(body)
        else:
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        raise ModelUnavailable(msg) from None
    states: dict[str, str] = {}
    for row in rows:
        service = row.get("Service") if isinstance(row, dict) else None
        state = row.get("State") if isinstance(row, dict) else None
        if not isinstance(service, str) or not isinstance(state, str):
            raise ModelUnavailable(msg)
        states[service] = state
    return states


def _check_loopback(url: str) -> None:
    """Every request: scheme `http`, host 127.0.0.1, localhost or ::1 (TH08-12)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host = None
    if host not in _LOOPBACK_HOSTS or parts.scheme != "http":
        msg = "service url must be http on 127.0.0.1, localhost or ::1"
        raise ConfigError(msg)


def _secret(ref: str | None) -> SecretStr | None:
    """The bearer value of a `secret:` reference (T10-06); never logged (TH08-02)."""
    return None if ref is None else secrets.resolve(ref)


def _reasoning_client() -> tuple[str, str | None]:
    """`(model, api_key ref)` of the first `models.yaml` client with `gpu_class == reasoning`."""
    for client in get_config().models.models.clients.values():
        if client.gpu_class == "reasoning":
            return client.model, client.api_key
    msg = "no models.yaml client has gpu_class reasoning"
    raise ModelUnavailable(msg)


def _warmup_request(
    name: ServiceName, svc: ServiceSettings
) -> tuple[str, dict[str, JsonValue], str | None]:
    """`(path, JSON body, bearer ref)` of the design 08 §5.8 warm-up of `name`."""
    if name == "vllm-reasoning":
        model, api_key = _reasoning_client()
        messages: list[JsonValue] = [{"role": "user", "content": "ping"}]
        body: dict[str, JsonValue] = {"model": model, "messages": messages, "max_tokens": 8}
        return "/v1/chat/completions", body, api_key
    if name == "llamacpp-large":
        return "/completion", {"prompt": "ping", "n_predict": 8}, svc.health.bearer_secret
    if name == "openjev":
        return "/v1/systemone", dict(_OPENJEV_WARMUP), svc.health.bearer_secret
    assert_never(name)  # an unknown name raises AssertionError: the warm-up fails


def _send(
    method: str,
    svc: ServiceSettings,
    path: str,
    body: dict[str, JsonValue] | None,
    bearer: SecretStr | None,
    timeout_s: float,
) -> int:
    """One request through the R-06 factory; the status. Redirects are not followed."""
    factory = egress.loopback_http_client  # module attribute, resolved per call (R-06)
    with (
        factory(svc.url, timeout_s=timeout_s, bearer=bearer) as client,
        client.stream(method, path, json=body) as response,
    ):
        read = 0
        for chunk in response.iter_bytes():
            read += len(chunk)
            if read >= BODY_READ_MAX:
                break
        return response.status_code


class LoopbackHttp:
    """Loopback-only HTTP for service health and warm-up (U08-79, TH08-12)."""

    def healthy(self, svc: ServiceSettings, *, timeout_s: float = 5) -> bool:
        """`GET url + health.path`; 2xx → True, any other status or exception → False."""
        _check_loopback(svc.url)
        try:
            bearer = _secret(svc.health.bearer_secret)
            status = _send("GET", svc, svc.health.path, None, bearer, timeout_s)
        except Exception:  # noqa: BLE001 - any failure reads as unhealthy (U08-79)
            return False
        return 200 <= status < 300  # noqa: PLR2004 - the 2xx range

    def warm_up(self, name: ServiceName, svc: ServiceSettings, *, timeout_s: float) -> None:
        """Send the warm-up request of `name`; non-2xx or exception → `ModelUnavailable`."""
        _check_loopback(svc.url)
        msg = f"warmup failed {name}"
        try:
            path, body, ref = _warmup_request(name, svc)
            status = _send("POST", svc, path, body, _secret(ref), timeout_s)
        except Exception:  # noqa: BLE001 - every failure is the one U08-79 error
            raise ModelUnavailable(msg) from None
        if not 200 <= status < 300:  # noqa: PLR2004 - the 2xx range
            raise ModelUnavailable(msg)


def vram_used_mb() -> int | None:
    """MB of GPU memory in use from `R.gpu.vram_check_cmd`; None on any failure (U08-80)."""
    argv = list(_gpu().vram_check_cmd)
    try:
        done = subprocess.run(  # noqa: S603 - argument list from config
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=VRAM_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        return None
    try:
        return int(lines[0])
    except ValueError:
        return None


def wait_vram_free(*, threshold_mb: int, poll_s: float = 2, timeout_s: float = 60) -> None:
    """Poll until `vram_used_mb() < threshold_mb` (None is not free); else `vram_not_freed`."""
    deadline = clock.monotonic() + timeout_s
    while True:
        used = vram_used_mb()
        if used is not None and used < threshold_mb:
            return
        if clock.monotonic() >= deadline:
            msg = "vram_not_freed"
            raise ModelUnavailable(msg)
        clock.sleep(poll_s)
