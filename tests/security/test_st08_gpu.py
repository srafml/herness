"""Security tests for GPU service control (impl 08 ST08-01, ST08-12; TH08-01, TH08-12; T08-17).

ST08-01's `gpu_request` entry point (U08-86, JobContext) is a later card: here the request
path is the `ComposeRunner` every GPU request ends in, which must refuse the name before
any process starts. The `gpu_request` case is a T08-17 carry-over.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.support.fake_gpu import REDIRECT_TARGET, SHIPPED_RESILIENCE, FakeGpu

from herness.core.errors import ConfigError
from herness.core.resilience.settings import GpuSettings, HealthCheck, ServiceSettings

pytestmark = pytest.mark.unit

BAD_NAMES = ["openjev;calc", "$(id)", "../x"]


def _gpu_raw() -> dict[str, Any]:
    raw = yaml.safe_load(SHIPPED_RESILIENCE.read_text(encoding="utf-8"))
    gpu: dict[str, Any] = raw["resilience"]["gpu"]
    return gpu


@pytest.mark.parametrize("name", BAD_NAMES)
def test_st08_01_config_rejects_bad_service_names(name: str) -> None:
    """ST08-01 a service name outside the allowlist is rejected by the config model."""
    raw = _gpu_raw()
    services = raw["classes"]["decider"]["services"]
    services[name] = services.pop("openjev")
    with pytest.raises(ValidationError):
        GpuSettings.model_validate(raw)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_st08_01_request_config_error_no_subprocess(fake_gpu: FakeGpu, name: str) -> None:
    """ST08-01 up/stop/kill with an injected name → ConfigError; no subprocess is started."""
    runner = fake_gpu.runner()
    with pytest.raises(ConfigError):
        runner.up("decider", name)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        runner.up(name, "openjev")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        runner.stop(name)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        runner.kill(name)  # type: ignore[arg-type]
    assert fake_gpu.argv == []
    assert fake_gpu.kwargs == []


def test_st08_01_argv_is_a_list_without_shell(fake_gpu: FakeGpu) -> None:
    """ST08-01 compose runs as an argument list with shell=False."""
    fake_gpu.runner().up("decider", "openjev")
    (argv,) = fake_gpu.argv
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert fake_gpu.kwargs[0]["shell"] is False


@pytest.mark.parametrize(
    "url",
    ["http://attacker.example:8000", "http://10.0.0.1:8000", "https://127.0.0.1:8000"],
)
def test_st08_12_config_rejects_non_loopback_url(url: str) -> None:
    """ST08-12 a service URL off loopback in config is rejected."""
    raw = _gpu_raw()
    raw["classes"]["reasoning"]["services"]["vllm-reasoning"]["url"] = url
    with pytest.raises(ValidationError):
        GpuSettings.model_validate(raw)


def test_st08_12_request_to_non_loopback_refused(fake_gpu: FakeGpu) -> None:
    """ST08-12 a non-loopback URL that bypassed config is refused per request."""
    svc = ServiceSettings.model_construct(
        url="http://attacker.example:8000",
        health=HealthCheck(path="/health"),
        start_timeout_s=10,
        start_on_entry=True,
    )
    with pytest.raises(ConfigError):
        fake_gpu.http.healthy(svc)


def test_st08_12_redirect_not_followed(fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST08-12 a loopback redirect to an external host is not followed: unhealthy."""
    stub = fake_gpu.stubs["vllm-reasoning"]
    stub.answers["/health"] = 302
    svc = fake_gpu.service("vllm-reasoning")
    connects: list[object] = []
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> None:
        connects.append(address)
        real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    assert fake_gpu.http.healthy(svc) is False
    assert [(r.method, r.path) for r in stub.requests] == [("GET", "/health")]
    port = int(svc.url.rsplit(":", 1)[1])
    assert connects == [("127.0.0.1", port)]
    assert REDIRECT_TARGET.startswith("http://attacker.example")
