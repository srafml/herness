"""Tests of U08-78 to U08-80: ComposeRunner, LoopbackHttp and the VRAM helpers."""

from __future__ import annotations

import re
import subprocess
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_gpu import FakeGpu, StubRequest, shipped_gpu_settings
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress
from herness.core import redact as r
from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.jobs import gpu_services as gs
from herness.core.jobs.gpu_services import ComposeRunner, LoopbackHttp
from herness.core.redact_directory import NameDirectory
from herness.core.resilience.settings import HealthCheck, ServiceSettings
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

PREFIX = [
    "wsl.exe", "-d", "herness", "--", "docker", "compose", "--env-file",
    "/opt/herness/docker.env", "-f", "/mnt/d/herness/docker/compose.yaml",
]  # fmt: skip
# The shipped flow list splits "--format=csv,noheader,nounits" at its commas (a config
# carry-over reported with T08-17): the runner must pass R.gpu.vram_check_cmd as loaded.
VRAM_ARGV = list(shipped_gpu_settings().vram_check_cmd)
EMAIL = "ops.person@example.com"
OPENJEV_KEY = "openjev-" + "stub-bearer-" + "value-1"  # built at runtime (detect-secrets)
VLLM_KEY = "vllm-" + "stub-bearer-" + "value-2"


@pytest.fixture
def herness_cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[MemoryKeyring]:
    """The full test config cached for `get_config`, and the fake keyring holding the keys."""
    c.reset_config()
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    fake_keyring.store[("herness", "openjev_api_key")] = OPENJEV_KEY
    fake_keyring.store[("herness", "vllm.api_key")] = VLLM_KEY
    yield fake_keyring
    c.reset_config()


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and no directory names."""
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every `herness.core.egress.loopback_http_client` call, passed on to the real factory."""
    calls: list[tuple[str, dict[str, Any]]] = []
    real = egress.loopback_http_client

    def factory(base_url: str, **kwargs: Any) -> Any:
        calls.append((base_url, kwargs))
        return real(base_url, **kwargs)

    monkeypatch.setattr(egress, "loopback_http_client", factory)
    return calls


# --- UT08-87 ComposeRunner argv ---------------------------------------------------------------


def test_ut08_87_exact_argv_lists(fake_gpu: FakeGpu) -> None:
    """UT08-87 up/stop/kill/ps build the exact argv lists, shell=False, per-verb timeouts."""
    runner = fake_gpu.runner()
    runner.up("reasoning", "vllm-reasoning")
    runner.stop("vllm-reasoning")
    runner.kill("openjev")
    assert runner.ps() == {"vllm-reasoning": "exited", "openjev": "exited"}
    assert fake_gpu.argv == [
        [*PREFIX, "--profile", "reasoning", "up", "-d", "vllm-reasoning"],
        [*PREFIX, "stop", "-t", "60", "vllm-reasoning"],
        [*PREFIX, "kill", "openjev"],
        [*PREFIX, "ps", "--format", "json"],
    ]
    common = {"shell": False, "capture_output": True, "text": True, "check": False}
    assert fake_gpu.kwargs == [{**common, "timeout": t} for t in (120, 120, 60, 30)]


def test_ut08_87_default_runner_reads_r_gpu(fake_gpu: FakeGpu) -> None:
    """UT08-87 a runner without pinned settings reads R.gpu at call time."""
    ComposeRunner().up("large", "llamacpp-large")
    assert fake_gpu.argv == [[*PREFIX, "--profile", "large", "up", "-d", "llamacpp-large"]]
    assert fake_gpu.states == {"llamacpp-large": "running"}


@pytest.mark.parametrize(
    "service",
    ["x; rm -rf", "foo", "OPENJEV", "", "openjev ", None, pytest.param("a" * 65, id="long")],
)
def test_ut08_87_bad_service_name_config_error(fake_gpu: FakeGpu, service: Any) -> None:
    """UT08-87 a service that is not a configured, well-formed name → ConfigError, no argv."""
    runner = fake_gpu.runner()
    for call in (
        lambda: runner.up("reasoning", service),
        lambda: runner.stop(service),
        lambda: runner.kill(service),
    ):
        with pytest.raises(ConfigError, match="not a configured service name"):
            call()
    assert fake_gpu.argv == []


@pytest.mark.parametrize("cls", ["none", "x; rm -rf", "REASONING", None])
def test_ut08_87_bad_class_config_error(fake_gpu: FakeGpu, cls: Any) -> None:
    """UT08-87 a profile that is not a key of R.gpu.classes → ConfigError, no argv."""
    with pytest.raises(ConfigError, match="not a configured GPU class"):
        fake_gpu.runner().up(cls, "vllm-reasoning")
    assert fake_gpu.argv == []


def test_ut08_87_compose_missing(fake_gpu: FakeGpu) -> None:
    """UT08-87 FileNotFoundError → ModelUnavailable("compose unavailable")."""
    fake_gpu.raise_on["up"] = FileNotFoundError("wsl.exe")
    with pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().up("decider", "openjev")
    assert info.value.message == "compose unavailable"


def test_ut08_87_compose_timeout(fake_gpu: FakeGpu) -> None:
    """UT08-87 TimeoutExpired → ModelUnavailable("compose <verb> <service> timed out")."""
    fake_gpu.raise_on["stop"] = subprocess.TimeoutExpired(["compose"], 120)
    with pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().stop("vllm-reasoning")
    assert info.value.message == "compose stop vllm-reasoning timed out"
    fake_gpu.raise_on["ps"] = subprocess.TimeoutExpired(["compose"], 30)
    with pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().ps()
    assert info.value.message == "compose ps timed out"


@pytest.mark.usefixtures("test_redactor")
def test_ut08_87_compose_failure_logged_redacted(fake_gpu: FakeGpu) -> None:
    """UT08-87 rc != 0 → ModelUnavailable("... failed rc=<n>"); stderr redacted, ≤ 500 chars."""
    fake_gpu.fail["kill"] = (1, f"error for {EMAIL}: " + "x" * 5000)
    with structlog.testing.capture_logs() as logs, pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().kill("openjev")
    assert info.value.message == "compose kill openjev failed rc=1"
    (line,) = [log for log in logs if log["event"] == "jobs.gpu.compose_failed"]
    assert line["log_level"] == "warning"
    assert (line["verb"], line["service"], line["rc"]) == ("kill", "openjev", 1)
    assert 0 < len(line["stderr"]) <= 500
    assert EMAIL not in line["stderr"]


@pytest.mark.usefixtures("test_redactor")
def test_ut08_87_ps_failure(fake_gpu: FakeGpu) -> None:
    """UT08-87 a failing `ps` names no service."""
    fake_gpu.fail["ps"] = (2, "")
    with structlog.testing.capture_logs() as logs, pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().ps()
    assert info.value.message == "compose ps failed rc=2"
    (line,) = [log for log in logs if log["event"] == "jobs.gpu.compose_failed"]
    assert (line["verb"], line["service"], line["stderr"]) == ("ps", None, "")


# --- UT08-88 ps parsing -----------------------------------------------------------------------


@pytest.mark.parametrize("ps_format", ["lines", "array"])
def test_ut08_88_ps_array_and_lines(fake_gpu: FakeGpu, ps_format: Any) -> None:
    """UT08-88 `ps` output as one JSON array and as JSON lines gives service → state."""
    fake_gpu.ps_format = ps_format
    fake_gpu.states.update({"vllm-reasoning": "running", "openjev": "exited"})
    assert fake_gpu.runner().ps() == {"vllm-reasoning": "running", "openjev": "exited"}


@pytest.mark.parametrize(
    "stdout",
    [
        '[{"Service": "openjev", "State": "running"}]\n',
        '\n{"Service": "openjev", "State": "running"}\n\n',
        '  [{"Service": "openjev", "State": "running", "Name": "herness-openjev-1"}]',
    ],
)
def test_ut08_88_ps_shapes(fake_gpu: FakeGpu, stdout: str) -> None:
    """UT08-88 blank lines, leading space and extra fields are accepted."""
    fake_gpu.ps_stdout = stdout
    assert fake_gpu.runner().ps() == {"openjev": "running"}


def test_ut08_88_ps_empty(fake_gpu: FakeGpu) -> None:
    """UT08-88 nothing running → empty mapping."""
    fake_gpu.ps_stdout = ""
    assert fake_gpu.runner().ps() == {}


@pytest.mark.parametrize(
    "stdout",
    [
        "garbage",
        "[1, 2]",
        '[{"Service": "openjev"}]',
        '{"State": "running"}',
        '{"Service": 1, "State": "running"}',
        '"openjev"',
        '[{"Service": "openjev", "State": "running"}',
        '{"Service": "openjev", "State": "running"}\nnot json',
        pytest.param("[" * 100_000, id="deep-nesting"),
    ],
)
def test_ut08_88_ps_garbage_unreadable(fake_gpu: FakeGpu, stdout: str) -> None:
    """UT08-88 garbage → ModelUnavailable("compose ps unreadable")."""
    fake_gpu.ps_stdout = stdout
    with pytest.raises(ModelUnavailable) as info:
        fake_gpu.runner().ps()
    assert info.value.message == "compose ps unreadable"


# --- UT08-89 LoopbackHttp ---------------------------------------------------------------------


@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_healthy_via_spy(fake_gpu: FakeGpu, spy: list[tuple[str, dict[str, Any]]]) -> None:
    """UT08-89 2xx → healthy; the client comes from the egress factory (R-06)."""
    svc = fake_gpu.service("vllm-reasoning")
    assert fake_gpu.http.healthy(svc) is True
    assert spy == [(svc.url, {"timeout_s": 5, "bearer": None})]
    assert fake_gpu.stubs["vllm-reasoning"].requests == [StubRequest("GET", "/health", None, None)]


@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_bearer_sent_not_logged(
    fake_gpu: FakeGpu, spy: list[tuple[str, dict[str, Any]]]
) -> None:
    """UT08-89 the OpenJev bearer (secret:OPENJEV_API_KEY) is sent and never logged."""
    svc = fake_gpu.service("openjev")
    with structlog.testing.capture_logs() as logs:
        assert fake_gpu.http.healthy(svc, timeout_s=2) is True
    (req,) = fake_gpu.stubs["openjev"].requests
    assert (req.method, req.path, req.authorization) == (
        "GET",
        "/v1/models",
        f"Bearer {OPENJEV_KEY}",
    )
    assert OPENJEV_KEY not in repr(logs)
    assert spy[0][1]["timeout_s"] == 2


@pytest.mark.parametrize("status", [302, 301, 404, 500, 503])
@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_non_2xx_unhealthy(fake_gpu: FakeGpu, status: int) -> None:
    """UT08-89 a 302 (not followed) or any other non-2xx status → unhealthy."""
    stub = fake_gpu.stubs["llamacpp-large"]
    stub.answers["/health"] = status
    assert fake_gpu.http.healthy(fake_gpu.service("llamacpp-large")) is False
    assert [r.path for r in stub.requests] == ["/health"]


@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_exception_unhealthy(fake_gpu: FakeGpu) -> None:
    """UT08-89 connection refused and a missing bearer secret read as unhealthy."""
    svc = fake_gpu.service("vllm-reasoning")
    stub = fake_gpu.stubs["vllm-reasoning"]
    assert stub.server is not None
    stub.server.shutdown()
    stub.server.server_close()
    assert fake_gpu.http.healthy(svc, timeout_s=1) is False


def test_ut08_89_missing_secret_unhealthy(fake_gpu: FakeGpu, fake_keyring: MemoryKeyring) -> None:
    """UT08-89 an unresolvable bearer secret → unhealthy, no request sent."""
    del fake_keyring
    assert fake_gpu.http.healthy(fake_gpu.service("openjev")) is False
    assert fake_gpu.stubs["openjev"].requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1:8000",
        "https://127.0.0.1:8000",
        "http://attacker.example:8000",
        "http://[::1:8000",
        "http://127.0.0.1.attacker.example:8000",
    ],
)
def test_ut08_89_non_loopback_config_error(url: str, spy: list[tuple[str, dict[str, Any]]]) -> None:
    """UT08-89 a non-loopback URL → ConfigError before any client is built."""
    svc = ServiceSettings.model_construct(
        url=url, health=HealthCheck(path="/health"), start_timeout_s=10, start_on_entry=True
    )
    http = LoopbackHttp()
    with pytest.raises(ConfigError, match=re.escape("must be http on 127.0.0.1")):
        http.healthy(svc)
    with pytest.raises(ConfigError, match=re.escape("must be http on 127.0.0.1")):
        http.warm_up("llamacpp-large", svc, timeout_s=5)
    assert spy == []


@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_warm_up_bodies(fake_gpu: FakeGpu, spy: list[tuple[str, dict[str, Any]]]) -> None:
    """UT08-89 warm-up requests match the design 08 §5.8 table."""
    for name in ("vllm-reasoning", "llamacpp-large", "openjev"):
        fake_gpu.http.warm_up(name, fake_gpu.service(name), timeout_s=120)  # type: ignore[arg-type]
    assert fake_gpu.stubs["vllm-reasoning"].requests == [
        StubRequest(
            "POST",
            "/v1/chat/completions",
            f"Bearer {VLLM_KEY}",
            {
                "model": "local-30b",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
        )
    ]
    assert fake_gpu.stubs["llamacpp-large"].requests == [
        StubRequest("POST", "/completion", None, {"prompt": "ping", "n_predict": 8})
    ]
    openjev_body = {
        "model": "openjev-latest",
        "state": "warm-up",
        "questions": {
            "warmup": {"type": "noul", "instructions": "Is this text a warm-up request?"}
        },
        "samples": 1,
        "steps": 1,
        "think": 0,
    }
    assert fake_gpu.stubs["openjev"].requests == [
        StubRequest("POST", "/v1/systemone", f"Bearer {OPENJEV_KEY}", openjev_body)
    ]
    assert len(spy) == 3
    assert all(kwargs["timeout_s"] == 120 for _url, kwargs in spy)


@pytest.mark.parametrize("status", [302, 400, 500])
@pytest.mark.usefixtures("herness_cfg")
def test_ut08_89_warm_up_non_2xx(fake_gpu: FakeGpu, status: int) -> None:
    """UT08-89 a non-2xx warm-up answer → ModelUnavailable("warmup failed <name>")."""
    fake_gpu.stubs["openjev"].answers["/v1/systemone"] = status
    with pytest.raises(ModelUnavailable) as info:
        fake_gpu.http.warm_up("openjev", fake_gpu.service("openjev"), timeout_s=5)
    assert info.value.message == "warmup failed openjev"


def test_ut08_89_warm_up_exception(fake_gpu: FakeGpu, fake_keyring: MemoryKeyring) -> None:
    """UT08-89 an exception (missing secret, closed port) → ModelUnavailable."""
    del fake_keyring
    with pytest.raises(ModelUnavailable, match="warmup failed openjev"):
        fake_gpu.http.warm_up("openjev", fake_gpu.service("openjev"), timeout_s=5)
    stub = fake_gpu.stubs["llamacpp-large"]
    assert stub.server is not None
    stub.server.shutdown()
    stub.server.server_close()
    with pytest.raises(ModelUnavailable, match="warmup failed llamacpp-large"):
        fake_gpu.http.warm_up("llamacpp-large", fake_gpu.service("llamacpp-large"), timeout_s=1)
    with pytest.raises(ModelUnavailable, match="warmup failed nothing"):
        fake_gpu.http.warm_up("nothing", fake_gpu.service("openjev"), timeout_s=1)  # type: ignore[arg-type]


def test_ut08_89_warm_up_without_reasoning_client(
    fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-89 no models.yaml client with gpu_class reasoning → the vLLM warm-up fails."""
    local = types.SimpleNamespace(gpu_class=None, model="small", api_key=None)
    cfg = types.SimpleNamespace(models=types.SimpleNamespace(models=types.SimpleNamespace(
        clients={"local-small-cpu": local}
    )))  # fmt: skip
    monkeypatch.setattr(gs, "get_config", lambda: cfg)
    with pytest.raises(ModelUnavailable, match="warmup failed vllm-reasoning"):
        fake_gpu.http.warm_up("vllm-reasoning", fake_gpu.service("vllm-reasoning"), timeout_s=5)
    assert fake_gpu.stubs["vllm-reasoning"].requests == []


def test_ut08_89_reasoning_client_without_key(
    fake_gpu: FakeGpu, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-89 the vLLM warm-up sends no bearer when the client has no api_key."""
    client = types.SimpleNamespace(gpu_class="reasoning", model="local-lora-14b", api_key=None)
    cfg = types.SimpleNamespace(models=types.SimpleNamespace(models=types.SimpleNamespace(
        clients={"local-lora-14b": client}
    )))  # fmt: skip
    monkeypatch.setattr(gs, "get_config", lambda: cfg)
    fake_gpu.http.warm_up("vllm-reasoning", fake_gpu.service("vllm-reasoning"), timeout_s=5)
    (req,) = fake_gpu.stubs["vllm-reasoning"].requests
    assert req.authorization is None
    assert req.body["model"] == "local-lora-14b"


def test_ut08_89_large_body_read_up_to_64_kb(fake_gpu: FakeGpu) -> None:
    """UT08-89 a large response body is read up to 64 KB and discarded; status still counts."""
    fake_gpu.stubs["llamacpp-large"].payloads["/health"] = b"x" * 1_000_000
    assert fake_gpu.http.healthy(fake_gpu.service("llamacpp-large")) is True


# --- UT08-90 VRAM -----------------------------------------------------------------------------


@pytest.mark.usefixtures("fake_clock")
def test_ut08_90_returns_on_second_poll(fake_gpu: FakeGpu) -> None:
    """UT08-90 readings 20 000 then 1 500 → returns on the second poll."""
    fake_gpu.vram_readings = [20_000, 1_500, 20_000]
    gs.wait_vram_free(threshold_mb=2000)
    assert fake_gpu.vram_argv == [VRAM_ARGV, VRAM_ARGV]


def test_ut08_90_timeout(fake_gpu: FakeGpu, fake_clock: FakeClock) -> None:
    """UT08-90 never below the threshold → ModelUnavailable("vram_not_freed") after timeout_s."""
    fake_gpu.vram_readings = [20_000] * 100
    start = fake_clock.now()
    with pytest.raises(ModelUnavailable) as info:
        gs.wait_vram_free(threshold_mb=2000, poll_s=2, timeout_s=60)
    assert info.value.message == "vram_not_freed"
    assert (fake_clock.now() - start).total_seconds() == 60
    assert len(fake_gpu.vram_argv) == 31


@pytest.mark.usefixtures("fake_clock")
def test_ut08_90_none_reading_is_not_free(fake_gpu: FakeGpu) -> None:
    """UT08-90 a None reading counts as not free; equal to the threshold is not free."""
    fake_gpu.vram_readings = [None, 2000, 1999]
    gs.wait_vram_free(threshold_mb=2000, poll_s=1, timeout_s=10)
    assert len(fake_gpu.vram_argv) == 3


def _completed(stdout: str, rc: int = 0) -> Any:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    return run


def _raises(exc: BaseException) -> Any:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise exc

    return run


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_completed("  \n1536\n2048\n"), 1536),
        (_completed("0\n"), 0),
        (_completed(""), None),
        (_completed("[N/A]\n"), None),
        (_completed("1536\n", rc=9), None),
        (_raises(FileNotFoundError("nvidia-smi")), None),
        (_raises(subprocess.TimeoutExpired(["nvidia-smi"], 10)), None),
        (_raises(PermissionError("denied")), None),
    ],
)
@pytest.mark.usefixtures("herness_cfg")
def test_ut08_90_vram_used_mb(monkeypatch: pytest.MonkeyPatch, run: Any, expected: Any) -> None:
    """UT08-90 first non-empty stdout line as int, from R.gpu.vram_check_cmd; failure → None."""
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def recording(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs))
        return run(argv, **kwargs)

    namespace = types.SimpleNamespace(
        run=recording,
        TimeoutExpired=subprocess.TimeoutExpired,
        SubprocessError=subprocess.SubprocessError,
    )
    monkeypatch.setattr(gs, "subprocess", namespace)
    assert gs.vram_used_mb() == expected
    common = {"shell": False, "capture_output": True, "text": True, "check": False}
    assert seen == [(VRAM_ARGV, {**common, "timeout": 10})]
