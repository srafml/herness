"""A scripted host for ``herness deploy up|down|rollback|prune`` tests (impl 10 U10-84, U10-85).

``DeployHost`` extends the ``deploy pull`` host (``tests.support.pull_host``) with what the class
and prune units call: ``docker image ls`` / ``rm`` over an in-memory image store, the ``find
-maxdepth 1`` listings of snapshot, blob and GGUF directories, ``rm -rf`` (which updates the
in-memory state), and the compose ``stop`` / ``up -d`` calls. ``FakeJobs`` stands in for the
impl 08 worker (``worker_alive``, ``request_gpu_class``, ``gpu_state``), ``FakeHealth`` for the
loopback health client, and ``FakeTime`` for ``clock.monotonic`` / ``clock.sleep`` so every poll
loop runs instantly. Nothing here reaches a real process or network.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from tests.support.fake_runner import FakeRunner
from tests.support.pull_host import ACTOR, ROOT, PullHost, repo_dir, start_pull_host

from herness.admin import deploy
from herness.admin.wsl import CmdResult
from herness.core import config as c
from herness.core import egress
from herness.core import time as clock

__all__ = ["DeployHost", "FakeHealth", "FakeJobs", "FakeTime", "start_deploy_host"]

GGUF_DIR = f"{ROOT}/gguf"


def _ok(stdout: str = "", returncode: int = 0) -> CmdResult:
    return CmdResult(returncode, stdout, "", 0.0)


@dataclass
class FakeTime:
    """``clock.monotonic`` / ``clock.sleep`` in fake seconds; ``sleeps`` records each wait."""

    t: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class FakeJobs:
    """The impl 08 surface ``deploy`` calls: a worker that loads ``load_after`` polls later."""

    alive: bool = False
    loaded: str = "none"
    requested: list[str] = field(default_factory=list)
    load_after: int | None = 1  # polls until the requested class shows; None never loads
    answer: str = "requested"
    _polls: int = 0

    def worker_alive(self) -> bool:
        return self.alive

    def request_gpu_class(self, cls: str) -> str:
        self.requested.append(cls)
        self._polls = 0
        return self.answer

    def loaded_class(self) -> str:
        self._polls += 1
        if self.requested and self.load_after is not None and self._polls > self.load_after:
            self.loaded = self.requested[-1]
        return self.loaded

    def gpu_state(self) -> FakeJobs:
        return self


@dataclass
class FakeHealth:
    """``egress.loopback_http_client`` stand-in: ``unhealthy[url]`` failing GETs before a 200."""

    unhealthy: dict[str, int] = field(default_factory=dict)
    always_down: set[str] = field(default_factory=set)
    raise_for: set[str] = field(default_factory=set)
    gets: list[tuple[str, str, str | None, float]] = field(default_factory=list)

    @contextmanager
    def client(self, base_url: str, *, timeout_s: float, bearer: Any = None) -> Iterator[Any]:
        token = None if bearer is None else bearer.get_secret_value()
        health = self

        class _Client:
            def get(self, path: str) -> Any:
                health.gets.append((base_url, path, token, timeout_s))
                if base_url in health.raise_for:
                    msg = "connection refused"
                    raise OSError(msg)
                left = health.unhealthy.get(base_url, 0)
                if base_url in health.always_down or left > 0:
                    health.unhealthy[base_url] = max(left - 1, 0)
                    return type("R", (), {"status_code": 503})()
                return type("R", (), {"status_code": 200})()

        yield _Client()


@dataclass
class DeployHost(PullHost):
    """``PullHost`` plus images, snapshot directories, blobs, GGUF files and ``rm``."""

    images: dict[str, list[str]] = field(default_factory=dict)  # repo -> digests
    snapshot_dirs: dict[str, list[str]] = field(default_factory=dict)  # repo dir -> names
    blob_files: dict[str, list[str]] = field(default_factory=dict)  # repo dir -> blob names
    gguf_files: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    image_rm: list[str] = field(default_factory=list)
    listing_override: dict[str, str] = field(default_factory=dict)  # directory -> raw stdout
    rm_fails: set[str] = field(default_factory=set)
    compose: list[list[str]] = field(default_factory=list)

    def add_snapshot(self, model: str, revision: str, files: dict[str, str]) -> str:
        """A snapshot ``revision`` of ``model`` linking ``rel -> blob name``; blobs listed."""
        repo = repo_dir(model)
        snap = f"{repo}/snapshots/{revision}"
        self.links[snap] = {}
        for rel, blob in files.items():
            self.links[snap][rel] = "../" * (rel.count("/") + 2) + f"blobs/{blob}"
            if blob not in self.blob_files.setdefault(repo, []):
                self.blob_files[repo].append(blob)
        if revision not in self.snapshot_dirs.setdefault(repo, []):
            self.snapshot_dirs[repo].append(revision)
        return snap

    def seed_disk(self) -> None:
        """Register the pull host's seeded snapshots, blobs, images and GGUF as on disk."""
        for snap, links in list(self.links.items()):
            repo, _s, revision = snap.rpartition("/snapshots/")
            self.snapshot_dirs.setdefault(repo, []).append(revision)
            for target in links.values():
                blob = posixpath.basename(target)
                if blob not in self.blob_files.setdefault(repo, []):
                    self.blob_files[repo].append(blob)
        for name in ("reasoning", "openjev", "large"):
            repo, _at, digest = getattr(self.cfg.deploy, name).image.partition("@")
            self.images.setdefault(repo, []).append(digest)
        self.gguf_files.append(posixpath.basename(self.cfg.deploy.large.gguf))

    def _listing(self, directory: str) -> CmdResult:
        if directory in self.listing_override:
            return _ok(self.listing_override[directory])
        if directory == GGUF_DIR:
            names = self.gguf_files
        else:
            repo, _slash, kind = directory.rpartition("/")
            table = self.snapshot_dirs if kind == "snapshots" else self.blob_files
            if repo not in table:
                return _ok(returncode=1)
            names = table[repo]
        return _ok("".join(f"{directory}/{n}\0" for n in names))

    def _remove(self, path: str) -> CmdResult:
        if path in self.rm_fails:
            return _ok(returncode=1)
        self.removed.append(path)
        parent, name = posixpath.split(path)
        if parent == GGUF_DIR:
            self.gguf_files.remove(name)
        elif parent.endswith("/snapshots"):
            self.snapshot_dirs[parent.rpartition("/")[0]].remove(name)
            self.links.pop(path, None)
        else:
            self.blob_files[parent.rpartition("/")[0]].remove(name)
        return _ok()

    def _wsl(self, argv: list[str]) -> CmdResult:
        inner = argv[argv.index("--exec") + 1 :]
        if inner[:3] == ["docker", "image", "ls"]:
            digests = self.images.get(inner[-1], [])
            return _ok("".join(f"{d}\n" for d in [*digests, "<none>"]))
        if inner[:3] == ["docker", "image", "rm"]:
            repo, _at, digest = inner[3].partition("@")
            self.image_rm.append(inner[3])
            self.images[repo].remove(digest)
            return _ok()
        if inner[:1] == ["find"] and "-maxdepth" in inner:
            return self._listing(inner[1])
        if inner[:3] == ["rm", "-rf", "--"]:
            return self._remove(inner[3])
        return super()._wsl(argv)

    def _compose(self, argv: list[str]) -> CmdResult:
        gpu = self.cfg.resilience.resilience.gpu
        self.compose.append(argv[len(gpu.compose_cmd) + 2 :])
        if self.fail_prefix is not None and tuple(argv[-len(self.fail_prefix) :]) == (
            self.fail_prefix
        ):
            return _ok(returncode=1)
        return _ok()


def start_deploy_host(
    tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[DeployHost, FakeJobs, FakeHealth, FakeTime]:
    """A pinned config, the scripted host, a fake worker, health client and clock.

    Needs the ``fake_keyring`` fixture with both API keys set.
    """
    base = start_pull_host(tmp_path, runner, monkeypatch)
    runner.table.clear()
    host = DeployHost(base.cfg, runner)
    host.seed()
    host.seed_disk()
    host.install()
    jobs, health, fake_time = FakeJobs(), FakeHealth(), FakeTime()
    monkeypatch.setattr(deploy, "worker_alive", jobs.worker_alive)
    monkeypatch.setattr(deploy, "request_gpu_class", jobs.request_gpu_class)
    monkeypatch.setattr(deploy, "gpu_state", jobs.gpu_state)
    monkeypatch.setattr(egress, "loopback_http_client", health.client)
    monkeypatch.setattr(clock, "monotonic", fake_time.monotonic)
    monkeypatch.setattr(clock, "sleep", fake_time.sleep)
    return host, jobs, health, fake_time


def render(host: DeployHost) -> None:
    """Write ``rendered.json`` for the current config (as ``herness deploy render`` would)."""
    deploy.render_env(host.cfg, actor=ACTOR)


def history_path(cfg: c.HernessConfig) -> Path:
    return Path(cfg.paths.data) / "deploy" / "history.jsonl"
