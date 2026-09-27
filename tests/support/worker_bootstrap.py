"""Test worker bootstrap and config (impl 08 T08-21) until T09-27 `herness.cli.worker_bootstrap`.

`bootstrap()` ("tests.support.worker_bootstrap:bootstrap") is what the supervisor and every job
child call first: it loads the config tree named by `HERNESS_TEST_WORKER_CONFIG`, points the ops
store at `HERNESS_TEST_WORKER_DB`, installs an in-memory keyring and a fixed-key redactor, binds
the SQLite resilience and jobs backends and registers the fake handlers below. Only these two
non-secret paths travel through the environment; nothing secret is in argv or the environment
(TH08-13, ST08-09).

The fake handler of every kind dispatches on `payload["mode"]` (default `done`).

Subprocess entry (a list argv, never a shell string):
`python -m tests.support.worker_bootstrap --gpu-classes none --concurrency 1 [--once]`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, get_args

import yaml

BOOTSTRAP = "tests.support.worker_bootstrap:bootstrap"
ENV_CONFIG = "HERNESS_TEST_WORKER_CONFIG"
ENV_DB = "HERNESS_TEST_WORKER_DB"
LOOP_LIMIT_S = 300.0  # no fake handler runs longer than this, even when never stopped

# Fast timings for the worker tests (all integers but `tick_s`; heartbeat_s * 3 < lease_s).
FAST_JOBS: dict[str, Any] = {
    "lease_s": 10,
    "heartbeat_s": 1,
    "reaper_interval_s": 1,
    "tick_s": 0.2,
    "cancel_grace_s": 2,
    "shutdown_grace_s": 20,
    "stall_timeout_s": 60,
    "stall_timeout_large_s": 120,
}


def write_worker_config(root: Path, jobs: dict[str, Any] | None = None) -> Path:
    """A full config tree whose `resilience.jobs` timings are `jobs` (default `FAST_JOBS`),
    with no scheduled chains, a 1-minute maintenance catch-up and a 1-2 s job backoff, so
    the scheduler adds no jobs of its own while a test runs."""
    from tests.support.config_tree import write_full_config  # noqa: PLC0415 - test helper

    cfg = write_full_config(root)
    path = cfg / "resilience.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["resilience"]["jobs"].update(FAST_JOBS if jobs is None else jobs)
    data["resilience"]["retry"]["job_backoff"] = {"base_s": 1, "cap_s": 2}
    data["schedule"]["jobs"] = []
    data["schedule"]["maintenance"] = {"catch_up_max": "1m"}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return cfg


def _sleep_until_stop(ctx: Any, *, heartbeat: bool, obey: bool) -> None:
    deadline = time.monotonic() + LOOP_LIMIT_S
    while time.monotonic() < deadline and not (obey and ctx.should_yield()):
        if heartbeat:
            ctx.heartbeat("working")
        time.sleep(0.1)


def fake_handler(ctx: Any) -> Any:
    """The handler of every kind: behaviour chosen by `payload["mode"]`."""
    from herness.core.types import JobOutcome  # noqa: PLC0415 - light import for children

    mode = ctx.job.payload.get("mode", "done")
    first = ctx.attempt == 1
    if mode == "loop":  # heartbeats until stopped, then yields
        _sleep_until_stop(ctx, heartbeat=True, obey=True)
        return JobOutcome(status="yield")
    if mode == "ignore_stop":  # heartbeats and never looks at should_yield
        _sleep_until_stop(ctx, heartbeat=True, obey=False)
    elif mode == "stall" and first:  # silent: no heartbeat at all
        _sleep_until_stop(ctx, heartbeat=False, obey=False)
    elif mode == "crash" and first:
        os._exit(9)
    elif mode == "checkpoint":
        return _checkpoint(ctx)
    return JobOutcome(status="done", result={"pid": os.getpid(), "attempt": ctx.attempt})


def _checkpoint(ctx: Any) -> Any:
    from herness.core.types import JobOutcome  # noqa: PLC0415 - light import for children

    n = int(ctx.load_state().get("n", 0))
    deadline = time.monotonic() + LOOP_LIMIT_S
    while not ctx.should_yield() and time.monotonic() < deadline:
        n += 1
        ctx.save_state({"n": n, "pid": os.getpid()})
        ctx.heartbeat(f"step {n}")
        time.sleep(0.2)
    return JobOutcome(status="yield")


def bootstrap() -> None:
    """Config, store, keyring, redactor, backends and fake handlers (see the module doc)."""
    import keyring  # noqa: PLC0415 - imported by the process that bootstraps
    from tests.support.fake_keyring import MemoryKeyring  # noqa: PLC0415

    from herness.core import config as c  # noqa: PLC0415
    from herness.core import redact as r  # noqa: PLC0415
    from herness.core.jobs.handlers import register_handler  # noqa: PLC0415
    from herness.core.jobs.ports import bind_jobs_backend  # noqa: PLC0415
    from herness.core.redact_directory import NameDirectory  # noqa: PLC0415
    from herness.core.resilience import bind_ops_backend  # noqa: PLC0415
    from herness.core.settings import RedactionConfig  # noqa: PLC0415
    from herness.core.types import JobKind  # noqa: PLC0415
    from herness.store.ops import core, reset_connections  # noqa: PLC0415
    from herness.store.ops.jobs import SqliteJobsBackend  # noqa: PLC0415
    from herness.store.ops.resilience import SqliteResilienceBackend  # noqa: PLC0415

    cfg_dir, db = Path(os.environ[ENV_CONFIG]), Path(os.environ[ENV_DB])
    if not isinstance(keyring.get_keyring(), MemoryKeyring):
        keyring.set_keyring(MemoryKeyring())
    c.init_config("local", config_dir=cfg_dir, env={"HERNESS_PATHS__DATA": str(db.parent)})
    if core._registry.path_override != db:
        reset_connections(path=db)
    directory = NameDirectory.from_files(None, (), None)
    red_cfg = RedactionConfig(directory_file=None)
    r._State.redactor = r.Redactor(red_cfg, bytes(range(32)), directory)
    bind_ops_backend(SqliteResilienceBackend())
    bind_jobs_backend(SqliteJobsBackend())  # type: ignore[arg-type]
    for kind in get_args(JobKind.__value__):
        register_handler(kind, fake_handler)


def main(argv: list[str] | None = None) -> int:
    """`python -m tests.support.worker_bootstrap ...`: run the worker, exit with its code."""
    from herness.core.jobs.supervisor import run_worker  # noqa: PLC0415

    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-classes", nargs="+", default=["none"])
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return run_worker(
        gpu_classes=args.gpu_classes,
        concurrency=args.concurrency,
        once=args.once,
        bootstrap=BOOTSTRAP,
    )


if __name__ == "__main__":
    sys.exit(main())
