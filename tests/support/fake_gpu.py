"""The `fake_gpu` fixture (impl 08 §11; T08-17): compose, loopback services and VRAM, faked.

* A fake `subprocess` for `herness.core.jobs.gpu_services`: every argv and its keyword
  arguments are recorded; compose `up` / `stop` / `kill` change the service states that
  `ps --format json` reports, and `R.gpu.vram_check_cmd` answers from `vram_readings` (then,
  once they run out, 20 000 MB while any service runs and 0 MB otherwise). The real
  `ComposeRunner` runs on top of it, so the argv lists it builds are the ones recorded.
* One stub loopback HTTP server per configured service on an ephemeral 127.0.0.1 port,
  recording `(method, path, Authorization, JSON body)`; `answers[path]` sets the status,
  `payloads[path]` the body (default `ok`), and a 3xx redirects to `http://attacker.example:8000/`.
* `gpu` is the shipped `resilience.gpu` with each service URL pointed at its stub server;
  it is installed as the `R.gpu` that `gpu_services` reads, so no config load is needed
  except for the `vllm-reasoning` warm-up, which reads `models.yaml`.

Nothing here reaches a real network or starts a real process.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from herness.core.jobs import gpu_services
from herness.core.jobs.gpu_services import ComposeRunner, LoopbackHttp
from herness.core.resilience.settings import GpuSettings, ResilienceConfig

SHIPPED_RESILIENCE = Path(__file__).resolve().parents[2] / "config" / "resilience.yaml"
REDIRECT_TARGET = "http://attacker.example:8000/"
VRAM_BUSY_MB = 20_000


@dataclass(frozen=True)
class StubRequest:
    """One request a stub service saw."""

    method: str
    path: str
    authorization: str | None
    body: Any


@dataclass
class StubService:
    """A loopback HTTP stub for one service: 200 unless `answers[path]` says otherwise."""

    url: str
    requests: list[StubRequest] = field(default_factory=list)
    answers: dict[str, int] = field(default_factory=dict)
    payloads: dict[str, bytes] = field(default_factory=dict)
    server: http.server.ThreadingHTTPServer | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    stub: StubService

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        auth = self.headers.get("Authorization")
        self.stub.requests.append(StubRequest(self.command, self.path, auth, body))
        status = self.stub.answers.get(self.path, 200)
        self.send_response(status)
        if 300 <= status < 400:
            self.send_header("Location", REDIRECT_TARGET)
        payload = self.stub.payloads.get(self.path, b"ok")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _answer  # noqa: N815 - http.server method names
    do_POST = _answer  # noqa: N815 - http.server method names

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        return None


def _start_stub() -> StubService:
    stub = StubService(url="")
    handler = type("Handler", (_Handler,), {"stub": stub})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    poll = {"poll_interval": 0.01}  # fast shutdown at teardown
    threading.Thread(target=server.serve_forever, kwargs=poll, daemon=True).start()
    stub.server = server
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    return stub


def shipped_gpu_settings() -> GpuSettings:
    """`resilience.gpu` of the shipped `config/resilience.yaml`."""
    raw = yaml.safe_load(SHIPPED_RESILIENCE.read_text(encoding="utf-8"))
    raw.pop("version", None)  # checked and dropped by the config loader (U10-16)
    return ResilienceConfig.model_validate(raw).resilience.gpu


def _with_urls(gpu: GpuSettings, urls: Mapping[str, str]) -> GpuSettings:
    classes = {
        cls: spec.model_copy(
            update={
                "services": {
                    name: svc.model_copy(update={"url": urls[name]})
                    for name, svc in spec.services.items()
                }
            }
        )
        for cls, spec in gpu.classes.items()
    }
    return gpu.model_copy(update={"classes": classes})


@dataclass
class FakeGpu:
    """Recorded compose argv, service states, stub servers and VRAM readings."""

    gpu: GpuSettings
    stubs: dict[str, StubService]
    argv: list[list[str]] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    vram_readings: list[int | None] = field(default_factory=list)
    vram_argv: list[list[str]] = field(default_factory=list)
    ps_format: Literal["lines", "array"] = "lines"
    ps_stdout: str | None = None
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)
    raise_on: dict[str, BaseException] = field(default_factory=dict)
    sticky: set[str] = field(default_factory=set)
    running_classes: list[frozenset[str]] = field(default_factory=list)
    http: LoopbackHttp = field(default_factory=LoopbackHttp)

    @property
    def prefix(self) -> list[str]:
        """`R.gpu.compose_cmd + ["-f", R.gpu.compose_file]`."""
        return [*self.gpu.compose_cmd, "-f", self.gpu.compose_file]

    def runner(self) -> ComposeRunner:
        """A real `ComposeRunner` on the fake settings (and the fake subprocess)."""
        return ComposeRunner(self.gpu)

    def service(self, name: str) -> Any:
        """The (stub-pointed) settings of service `name`."""
        for spec in self.gpu.classes.values():
            if name in spec.services:
                return spec.services[name]
        raise KeyError(name)

    def class_of(self, name: str) -> str:
        return next(c for c, spec in self.gpu.classes.items() if name in spec.services)

    def running(self) -> set[str]:
        return {s for s, state in self.states.items() if state == "running"}

    def _ps(self) -> str:
        rows = [{"Service": s, "State": st} for s, st in self.states.items()]
        if self.ps_stdout is not None:
            return self.ps_stdout
        if self.ps_format == "array":
            return json.dumps(rows)
        return "".join(json.dumps(row) + "\n" for row in rows)

    def _vram(self) -> str:
        if self.vram_readings:
            reading = self.vram_readings.pop(0)  # None: nvidia-smi printed nothing
        else:
            reading = VRAM_BUSY_MB if self.running() else 0
        return "" if reading is None else f"{reading}\n"

    def _compose(self, tail: list[str]) -> str:
        if tail[:1] == ["--profile"]:
            self.states[tail[-1]] = "running"
        elif tail[0] in {"stop", "kill"} and tail[-1] not in self.sticky:
            self.states[tail[-1]] = "exited"
        elif tail[0] == "ps":
            return self._ps()
        return ""

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """The fake `subprocess.run`."""
        if argv == list(self.gpu.vram_check_cmd):
            self.vram_argv.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, self._vram(), "")
        self.argv.append(list(argv))
        self.kwargs.append(dict(kwargs))
        tail = argv[len(self.prefix) :]
        verb = "up" if tail[:1] == ["--profile"] else tail[0]
        if verb in self.raise_on:
            raise self.raise_on[verb]
        if verb in self.fail:
            rc, stderr = self.fail[verb]
            return subprocess.CompletedProcess(argv, rc, "", stderr)
        stdout = self._compose(tail)
        self.running_classes.append(frozenset(self.class_of(s) for s in self.running()))
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def assert_single_class(self) -> None:
        """Services of two classes were never running at the same time."""
        assert all(len(classes) <= 1 for classes in self.running_classes), self.running_classes


@pytest.fixture
def fake_gpu(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGpu]:
    """`FakeGpu` installed as `gpu_services`' subprocess and `R.gpu` (impl 08 §11)."""
    base = shipped_gpu_settings()
    names = [name for spec in base.classes.values() for name in spec.services]
    stubs = {name: _start_stub() for name in names}
    fake = FakeGpu(gpu=_with_urls(base, {n: s.url for n, s in stubs.items()}), stubs=stubs)
    namespace = types.SimpleNamespace(
        run=fake.run,
        TimeoutExpired=subprocess.TimeoutExpired,
        SubprocessError=subprocess.SubprocessError,
    )
    monkeypatch.setattr(gpu_services, "subprocess", namespace)
    monkeypatch.setattr(gpu_services, "_gpu", lambda: fake.gpu)
    try:
        yield fake
    finally:
        for stub in stubs.values():
            if stub.server is not None:
                stub.server.shutdown()
                stub.server.server_close()
