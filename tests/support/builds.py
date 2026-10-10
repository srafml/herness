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
   Enrichment and cluster naming run for real on the loopback stubs of `_build_stubs`
   (T11-34): started for this build only, stopped in `finally`, never on a reuse.

Deviations (T11-17 rulings): the cache is per user, outside the repo
(`<tempfile.gettempdir()>/herness-synth-builds-<user>`, or `cache_dir` / `--synth-builds-dir`;
no new `HERNESS_*` variable), created 0o700; a symlink, junction or (POSIX) foreign-owned root
or slot and an unsafe parent chain are refused (`_build_dirs`), deletes never follow links
(Windows has no owner check: a `TMP` on a shared folder is not private). `enrich` is the real
`run_enrichment` on the stubs (`BuildHandle.stubs` describes them); `score` still runs
T02-22's fake hook (the card's scope is enrichment and naming). Compose is answered by
`fake_gpu.FakeGpu` over the stubs. Process-wide state is restored after the build.
`tiny_root` copies `tests/fixtures/lake_small/` into a tmp dir. `small` generation uses a
`spawn` pool: a script calling `ensure_build` outside pytest needs an `if __name__ ==
"__main__":` guard (re-entry fails fast with a clear error).
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast

import keyring
import pytest
from pydantic import BaseModel
from tests.support import _build_cache as cache
from tests.support._build_cache import BuildFixtureError, Scale
from tests.support._build_dirs import clear_build_outputs, private_dir, safe_rmtree
from tests.support._build_stubs import (
    STUB_SECRETS,
    STUBS,
    StubSet,
    install_laya,
    llm_factory,
    running_stubs,
    stub_overrides,
    wire_enrichment,
)
from tests.support.config_tree import SHIPPED, write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as herness_config
from herness.core.errors import HernessError
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
    "build_root",
    "clear_build_outputs",
    "default_cache_dir",
    "ensure_build",
    "private_dir",
    "safe_rmtree",
    "small_build",
    "tiny_build",
    "tiny_root",
]

REPO: Final = Path(__file__).resolve().parents[2]
LAKE_SMALL: Final = REPO / "tests" / "fixtures" / "lake_small"
GENERATED: Final = frozenset({"raw", "inbox"})  # what `generate` writes under `<root>/data`
_OPTION: Final = "--synth-builds-dir"


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
    """`<system temp>/herness-synth-builds-<user>`: outside the repository, per user."""
    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers)
        user = "uid" + str(getattr(os, "getuid", os.getpid)())
    return Path(tempfile.gettempdir()) / ("herness-synth-builds-" + re.sub(r"[^\w.-]", "_", user))


# --- build ----------------------------------------------------------------------------------


class _Report(BaseModel):
    name: str


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


@contextlib.contextmanager
def _test_keyring() -> Iterator[None]:
    """A `MemoryKeyring` with a fixed redaction key (as `fake_keyring`), restored after."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    backend.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()  # built at run time
    for name in STUB_SECRETS:  # the stubs ignore the bearer; the clients still resolve one
        backend.store[("herness", name)] = "stub-" + name
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
    with running_stubs(root) as urls, pytest.MonkeyPatch.context() as patch, _test_keyring():
        patch.setattr(_state._holder, "state", ProcessState())  # restored on exit
        for name, value in env.items():
            patch.setenv(name, value)
        patch.delenv("HERNESS_FAULTS", raising=False)
        patch.setattr(stages, "_load_run_scoring", lambda: _scoring)
        wire_enrichment(patch, urls)
        install_laya(patch, data_root, cfg_dir)
        herness_config.reset_config()
        overrides = [f"paths.data={data_root.as_posix()}", *stub_overrides(cfg_dir, urls)]
        try:
            herness_config.init_config("synth", overrides, config_dir=cfg_dir, env=env)
            ops.reset_connections(path=data_root / "ops.sqlite")
            migrate()
            bind_ops_backend(SqliteResilienceBackend())
            bind_jobs_backend(cast("JobsBackend", SqliteJobsBackend()))
            register_handler("build_pipeline", make_build_pipeline_handler(llm_factory=llm_factory))
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
    return BuildHandle(root, data_root, build_id, path, truth, STUBS)


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
