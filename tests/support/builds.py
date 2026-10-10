"""tests.support.builds: `ensure_build`, `BuildHandle`, fixtures (impl 11 U11-48, flow F11-06).

`ensure_build(seed, scale, repo_root=...)` builds the synthetic dataset once through the real
spec 02 pipeline; later calls, sessions and concurrent processes reuse it (T11-17 spec note):

1. Slot `root = <cache>/<seed>-<scale>-<fingerprint[:12]>`. The fingerprint (SHA-256 over
   `GENERATOR_VERSION`, the params hash, `FIXTURE_REV` and the sorted path/content hashes of
   `herness/model/sql/*.sql` and `*.jinja`, `config/*.yaml`, `config/profiles/synth.yaml`
   under `repo_root`) keys the slot, so another worktree builds beside it and never clears a
   slot in use; old slots are not pruned. Python code is not fingerprinted: bump
   `FIXTURE_REV` when a change alters how builds are made (a guard test pins the sources).
2. Lock `<root>.lock` (`_build_cache.build_lock`), released in `finally`; truth loads under it.
3. Generate when `<root>/.synth_root` is missing or stale; reuse when
   `<root>/data/.fixture_build.json` names an existing warehouse.
4. Otherwise clear the build outputs (the generated `raw/`, `inbox/` stay), load profile
   `synth` on a `config_tree` tree with the shipped `mappings.yaml` (`paths.data =
   <root>/data`, `HERNESS_SYNTH_CONFIG = <root>/synth_mappings.yaml`), migrate the ops store,
   enqueue `build_pipeline` with every stage and `run_inline` it; write the fingerprint file.

Deviations (T11-17 rulings): the cache is per user, outside the repo
(`<tempfile.gettempdir()>/herness-synth-builds-<user>`, or `cache_dir` / `--synth-builds-dir`;
no new `HERNESS_*` variable), created 0o700; a symlink, junction or (POSIX) foreign-owned
root or slot is refused, deletes never follow links (Windows has no owner check: a `TMP` on a
shared folder is not private). `enrich` / `score` run T02-22's fake hooks, so no stubs start
and `StubSet` is empty until T11-34. Compose is answered by `fake_gpu.FakeGpu` (class `none`).
Process-wide state is restored after the build. `tiny_root` copies `tests/fixtures/lake_small/`
into a tmp dir. `small` generation uses a `spawn` pool: a script calling `ensure_build` outside
pytest needs an `if __name__ == "__main__":` guard (re-entry fails fast with a clear error).
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import os
import re
import shutil
import stat
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


# --- private cache directories -------------------------------------------------------------


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def private_dir(path: Path) -> Path:
    """Create `path` (mode 0o700) or accept it; refuse a link, a non-directory or (POSIX) a
    directory owned by another user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    info = os.lstat(path)
    if _linked(path) or not stat.S_ISDIR(info.st_mode):
        msg = f"refusing build cache path (symlink, junction or not a directory): {path}"
        raise BuildFixtureError(msg)
    if hasattr(os, "getuid"):  # POSIX only: Windows has no st_uid
        if info.st_uid != os.getuid():
            msg = f"refusing build cache path owned by another user: {path}"
            raise BuildFixtureError(msg)
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)  # an older run's 0o755: make it private again
    return path


def safe_rmtree(path: Path) -> None:
    """`shutil.rmtree(path)`, refused when `path` itself is a symlink or junction."""
    if _linked(path):
        msg = f"refusing to delete through a symlink or junction: {path}"
        raise BuildFixtureError(msg)
    shutil.rmtree(path)


def clear_build_outputs(data_root: Path, keep: frozenset[str]) -> None:
    """Remove every child of `data_root` except `keep`; a linked child is refused."""
    for child in private_dir(data_root).iterdir():
        if child.name in keep:
            continue
        if _linked(child):
            msg = f"refusing to delete through a symlink or junction: {child}"
            raise BuildFixtureError(msg)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def default_cache_dir() -> Path:
    """`<system temp>/herness-synth-builds-<user>`: outside the repository, per user."""
    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers)
        user = "uid" + str(getattr(os, "getuid", os.getpid)())
    return Path(tempfile.gettempdir()) / ("herness-synth-builds-" + re.sub(r"[^\w.-]", "_", user))


# --- build ----------------------------------------------------------------------------------


class _Report(BaseModel):
    name: str


def _enrichment(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="enrich")


def _scoring(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="scoring")


def _config_dir(root: Path) -> Path:
    cfg_root = root / "build_config"
    if cfg_root.exists() or cfg_root.is_symlink():
        safe_rmtree(cfg_root)
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
    clear_build_outputs(data_root, GENERATED)  # the generated lake stays
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
                msg = f"build failed: {_last_error(job_id) or exc}"
                raise BuildFixtureError(msg) from exc
            if outcome.status != "done" or not outcome.result.get("promoted"):
                msg = f"build did not finish: status {outcome.status}: {_last_error(job_id)}"
                raise BuildFixtureError(msg)
        finally:
            ops.reset_connections()
            herness_config.reset_config()
    return str(outcome.result["build_id"])


def _last_error(job_id: str) -> object:
    return getattr(require_jobs_backend().get_job(job_id), "last_error", None)


def build_root(base: Path, seed: int, scale: Scale, *, repo_root: Path) -> Path:
    """`<base>/<seed>-<scale>-<fingerprint[:12]>`: one immutable slot per fingerprint."""
    return _slot(base, seed, scale, cache.fingerprint(repo_root, cache.default_params_hash(scale)))


def _slot(base: Path, seed: int, scale: Scale, print_: str) -> Path:
    return base / f"{seed}-{scale}-{print_[:12]}"


def ensure_build(
    seed: int, scale: Scale, *, repo_root: Path, cache_dir: Path | None = None
) -> BuildHandle:
    """Build `seed` / `scale` once (or reuse it) under `cache_dir` (U11-48; module doc)."""
    base = private_dir(cache_dir or default_cache_dir())
    phash = cache.default_params_hash(scale)
    print_ = cache.fingerprint(repo_root, phash)
    root = private_dir(_slot(base, seed, scale, print_))  # == build_root(...), hashed once
    data_root = root / "data"
    with cache.build_lock(root):
        if not cache.marker_current(root, phash):
            cache.generate_root(seed, scale, root)
        build_id = cache.reusable(data_root, print_)
        if build_id is None:  # no complete build in this slot: nobody can be reading one
            build_id = _run_build(root, data_root)
            cache.write_fingerprint(data_root, print_, build_id)
        truth = load_truth(root / "truth")
    path = warehouse.build_path(build_id, layout=DataLayout.from_root(data_root))
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
