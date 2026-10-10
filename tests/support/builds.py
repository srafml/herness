"""tests.support.builds: `ensure_build`, `BuildHandle`, fixtures (impl 11 U11-48, flow F11-06).

`ensure_build(seed, scale, repo_root=...)` builds the synthetic dataset of `seed` / `scale`
once through the real spec 02 pipeline and hands out its paths. Later calls (and later pytest
sessions, and concurrent xdist workers) reuse the build while its fingerprint is unchanged:

1. Lock: `<root>.lock` (`O_CREAT | O_EXCL`, 1 s poll, "build lock timeout" after 30 min,
   stale locks of dead holders broken; `tests.support._build_cache`), released in `finally`.
2. `generate(seed, scale, root, overwrite=True)` when `<root>/.synth_root` is missing or its
   `generator_version` / `params_hash` differ from the current defaults (dirty `default`).
3. Fingerprint: SHA-256 over `GENERATOR_VERSION`, the params hash, `FIXTURE_REV` and the
   sorted (path, content hash) pairs of `herness/model/sql/*.sql` (plus the `*.jinja` macros
   they include), `config/*.yaml` and `config/profiles/synth.yaml` under `repo_root`. Equal to
   `<root>/data/.fixture_build.json` and that build's warehouse exists → reuse.
4. Otherwise `<root>/data` is rebuilt from scratch (every build output removed, the generated
   `raw/` and `inbox/` kept): config profile `synth` (a full config tree
   from `tests.support.config_tree` with the shipped `mappings.yaml`, overrides
   `paths.data = <root>/data`, `HERNESS_SYNTH_CONFIG = <root>/synth_mappings.yaml`), a migrated
   ops store, `build_pipeline` enqueued with every stage of `STAGE_ORDER` and run with
   `run_inline`; then the fingerprint file is written.

Deviations (sub-controller rulings, T11-17):

* The build root is NOT `<repo>/data/synth/<seed>-<scale>`: nothing is written into the repo.
  The cache directory defaults to `<tempfile.gettempdir()>/herness-synth-builds` and can be
  set with the `cache_dir` keyword or the `--synth-builds-dir` pytest option (no new
  `HERNESS_*` variable: impl 10's env guard rejects unknown ones). `repo_root` still names
  the fingerprinted inputs.
* Phase 1: the `enrich` and `score` stages run the same fake spec 03 / 04 hooks as impl 02's
  T02-22 (`tests.support.synth_build`), so no stub decider or stub LLM is started and
  `BuildHandle.stubs` is empty. T11-34 starts the oracle `StubDeciderServer` and the
  `FakeLLMServer` (`tests/fixtures/llm_scripts/build/`) and fills `StubSet`.
* `run_inline` holds the host GPU lock for `build_pipeline` (R-43) and detects the loaded
  class through compose; compose is answered by `tests.support.fake_gpu.FakeGpu` (nothing
  running → class `none`), so no real process starts.

Process-wide state (config, ops store binding, process state, hooks, env) is set for the
build only and restored after it. `tiny_root` copies `tests/fixtures/lake_small/` into a tmp
dir (no build). Registered as a pytest plugin by `tests/conftest.py`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
import subprocess
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast

import keyring
import pytest
from pydantic import BaseModel
from tests.support import _build_cache as cache
from tests.support._build_cache import BuildFixtureError, Scale
from tests.support.config_tree import SHIPPED, write_full_config
from tests.support.fake_gpu import FakeGpu, shipped_gpu_settings
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as herness_config
from herness.core.errors import HernessError
from herness.core.jobs import gpu_services
from herness.core.jobs.handlers import register_handler
from herness.core.jobs.inline import run_inline
from herness.core.jobs.ports import JobsBackend, bind_jobs_backend, require_jobs_backend
from herness.core.jobs.queue import enqueue
from herness.core.resilience import ProcessState, _state, bind_ops_backend
from herness.eval.truth import TruthManifest, load_truth
from herness.model import _build_stages as stages
from herness.model._build_support import STAGE_ORDER
from herness.model.build import make_build_pipeline_handler
from herness.store import ops, warehouse
from herness.store.layout import DataLayout
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.migrate import migrate
from herness.store.ops.resilience import SqliteResilienceBackend

__all__ = [
    "REPO",
    "BuildFixtureError",
    "BuildHandle",
    "StubSet",
    "default_cache_dir",
    "ensure_build",
    "small_build",
    "tiny_build",
    "tiny_root",
]

REPO: Final = Path(__file__).resolve().parents[2]
LAKE_SMALL: Final = REPO / "tests" / "fixtures" / "lake_small"
GENERATED: Final = frozenset({"raw", "inbox"})  # what `generate` writes under `<root>/data`
_OPTION: Final = "--synth-builds-dir"


@dataclasses.dataclass(frozen=True)
class StubSet:
    """Loopback stubs the build enriched with (T11-34); empty in phase 1 (fake hooks)."""

    decider_url: str | None = None
    llm_url: str | None = None


@dataclasses.dataclass(frozen=True)
class BuildHandle:
    """A built synthetic dataset: its root, pipeline data root, build and truth."""

    root: Path
    data_root: Path
    build_id: str
    warehouse: Path
    truth: TruthManifest
    stubs: StubSet


def default_cache_dir() -> Path:
    """`<system temp>/herness-synth-builds`: outside the repository (ruling 1, T11-17)."""
    return Path(tempfile.gettempdir()) / "herness-synth-builds"


# --- build ----------------------------------------------------------------------------------


class _Report(BaseModel):
    name: str


def _enrichment(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="enrich")


def _scoring(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="scoring")


def _config_dir(root: Path) -> Path:
    cfg_root = root / "build_config"
    if cfg_root.exists():
        shutil.rmtree(cfg_root)
    cfg_dir = write_full_config(cfg_root)
    shutil.copyfile(SHIPPED / "mappings.yaml", cfg_dir / "mappings.yaml")
    (cfg_dir / "sources.yaml").write_text("version: 1\nbuild:\n  threads: 2\n", encoding="utf-8")
    return cfg_dir


def _fake_compose(patch: pytest.MonkeyPatch) -> None:
    """Compose answered by `FakeGpu` with nothing running (class `none`), as in `fake_gpu`."""
    fake = FakeGpu(gpu=shipped_gpu_settings(), stubs={})
    namespace = types.SimpleNamespace(run=fake.run, TimeoutExpired=subprocess.TimeoutExpired,
                                      SubprocessError=subprocess.SubprocessError)  # fmt: skip
    patch.setattr(gpu_services, "subprocess", namespace)
    patch.setattr(gpu_services, "_gpu", lambda: fake.gpu)


@contextlib.contextmanager
def _test_keyring() -> Iterator[None]:
    """A `MemoryKeyring` with a fixed redaction key (as `fake_keyring`), restored after."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    backend.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()  # built at run time
    keyring.set_keyring(backend)
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def _run_build(root: Path, data_root: Path) -> str:
    """F11-06 steps 4-5 on a fresh `data_root`; returns the build id."""
    for child in data_root.iterdir():  # earlier build outputs; the generated lake stays
        if child.name not in GENERATED:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    cfg_dir = _config_dir(root)
    env = {"HERNESS_ENV": "test", "HERNESS_SYNTH_CONFIG": str(root / "synth_mappings.yaml")}
    with pytest.MonkeyPatch.context() as patch, _test_keyring():
        patch.setattr(_state._holder, "state", ProcessState())  # restored on exit
        for name, value in env.items():
            patch.setenv(name, value)
        patch.delenv("HERNESS_FAULTS", raising=False)
        patch.setattr(stages, "_load_run_enrichment", lambda: _enrichment)
        patch.setattr(stages, "_load_run_scoring", lambda: _scoring)
        _fake_compose(patch)
        herness_config.reset_config()
        overrides = [f"paths.data={data_root.as_posix()}"]
        try:
            herness_config.init_config("synth", overrides, config_dir=cfg_dir, env=env)
            ops.reset_connections(path=data_root / "ops.sqlite")
            migrate()
            bind_ops_backend(SqliteResilienceBackend())
            bind_jobs_backend(cast("JobsBackend", SqliteJobsBackend()))
            register_handler("build_pipeline", make_build_pipeline_handler(llm_factory=None))
            job_id = enqueue("build_pipeline", {"stages": list(STAGE_ORDER)}, "none")
            try:
                outcome = run_inline(job_id)
            except HernessError as exc:
                row = require_jobs_backend().get_job(job_id)
                msg = f"build failed: {getattr(row, 'last_error', None) or exc}"
                raise BuildFixtureError(msg) from exc
        finally:
            ops.reset_connections()
            herness_config.reset_config()
    if outcome.status != "done" or not outcome.result.get("promoted"):
        msg = f"build did not finish: status {outcome.status}"
        raise BuildFixtureError(msg)
    return str(outcome.result["build_id"])


def ensure_build(
    seed: int, scale: Scale, *, repo_root: Path, cache_dir: Path | None = None
) -> BuildHandle:
    """Build `seed` / `scale` once (or reuse it) under `cache_dir` (U11-48; module doc)."""
    root = (cache_dir or default_cache_dir()) / f"{seed}-{scale}"
    data_root = root / "data"
    phash = cache.default_params_hash(scale)
    with cache.build_lock(root):
        if not cache.marker_current(root, phash):
            cache.generate_root(seed, scale, root)
        print_ = cache.fingerprint(repo_root, phash)
        build_id = cache.reusable(data_root, print_)
        if build_id is None:
            build_id = _run_build(root, data_root)
            cache.write_fingerprint(data_root, print_, build_id)
    path = warehouse.build_path(build_id, layout=DataLayout.from_root(data_root))
    truth = load_truth(root / "truth")
    return BuildHandle(root, data_root, build_id, path, truth, StubSet())


# --- pytest plugin --------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("herness-builds", "Herness build fixtures (impl 11 U11-48)")
    group.addoption(
        _OPTION,
        dest="synth_builds_dir",
        metavar="DIR",
        default=None,
        help="cache directory of tiny_build / small_build (default: system temp)",
    )


def _cache_dir(config: pytest.Config) -> Path | None:
    value: Any = config.getoption("synth_builds_dir")
    return None if value is None else Path(value)


@pytest.fixture(scope="session")
def tiny_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of `tests/fixtures/lake_small/` (seed 7 tiny raw lake) in a tmp dir; no build."""
    target = tmp_path_factory.mktemp("tiny_root") / "lake_small"
    shutil.copytree(LAKE_SMALL, target)
    return target


@pytest.fixture(scope="session")
def tiny_build(pytestconfig: pytest.Config) -> BuildHandle:
    """Seed 7 `tiny` (dirty `default`) built through the real pipeline, once per fingerprint."""
    return ensure_build(7, "tiny", repo_root=REPO, cache_dir=_cache_dir(pytestconfig))


@pytest.fixture(scope="session")
def small_build(pytestconfig: pytest.Config) -> BuildHandle:
    """Seed 42 `small` (dirty `default`) built through the real pipeline, once per fingerprint."""
    return ensure_build(42, "small", repo_root=REPO, cache_dir=_cache_dir(pytestconfig))
