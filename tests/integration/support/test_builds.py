"""Build fixtures through the real pipeline (impl 11 IT11-11, IT11-09; U11-48, T11-17).

IT11-11 builds seed 7 `tiny` into a fresh per-test cache root and calls `ensure_build` twice;
IT11-09 reads `meta.dq_result` of the session `tiny_build` (dirty `default`).

Truth key -> `meta.dq_result` check, as in impl 02 IT02-31 (`tests/integration/
test_build_dirty.py`): the generator counts a defect on the record it mutates while the build
sees the deduplicated final rows, so only `resolved_before_opened` and `unknown_enum` are
observable to +-1; `missing_service`, `bad_timestamp` and `future_ts` are bounded by
tombstones, NULL writes and the clock (`bad_timestamp` writes a bad date, epoch
milliseconds or an empty value, one third each: on the 4 tiny writes only ">= 1 failed cast"
holds, where IT02-31's `small` scale allows ">= half"); `duplicate_rows`, `later_versions`
and `tombstones` have no DQ counterpart (staging dedupe removes them; `duplicate_key:*` stays 0).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.support._build_cache import build_lock, clear_build_outputs, fingerprint, private_dir
from tests.support.builds import REPO, BuildFixtureError, BuildHandle, ensure_build

from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

_INCIDENT_TS = ("opened_at", "acknowledged_at", "resolved_at", "closed_at")
_CHANGE_TS = ("opened_at", "planned_start", "planned_end", "actual_start", "actual_end")


def _build_jobs(data_root: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(data_root / "ops.sqlite") as con:
        rows = con.execute(
            "SELECT job_id, status FROM job WHERE kind = 'build_pipeline' ORDER BY job_id"
        ).fetchall()
    return [(str(job_id), str(status)) for job_id, status in rows]


def _dq(handle: BuildHandle) -> dict[str, dict[str, Any]]:
    with warehouse.open_readonly(
        handle.build_id, layout=DataLayout.from_root(handle.data_root)
    ) as con:
        rows = con.execute(
            "SELECT check_name, value, CAST(details AS VARCHAR) FROM meta.dq_result"
        ).fetchall()
    return {str(n): {"value": v, "details": json.loads(d)} for n, v, d in rows}


def _failed(dq: dict[str, Any], names: list[str]) -> int:
    return int(sum(dq[f"cast_fail:{n}"]["details"]["failed"] for n in names))


def test_it11_11_second_ensure_build_reuses_the_build(tmp_path: Path) -> None:
    """IT11-11 a clean `7-tiny` cache root built once: the second `ensure_build` reuses the
    build (same build id and warehouse, no further `build_pipeline` job enqueued)."""
    first = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert first.root == tmp_path / "7-tiny"
    assert first.warehouse.is_file()
    jobs = _build_jobs(first.data_root)
    assert [status for _, status in jobs] == ["done"]
    stamp = first.warehouse.stat().st_mtime_ns
    second = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert second == first
    assert _build_jobs(second.data_root) == jobs
    assert second.warehouse.stat().st_mtime_ns == stamp
    assert not (tmp_path / "7-tiny.lock").exists()
    fingerprint = json.loads((first.data_root / ".fixture_build.json").read_text("utf-8"))
    assert fingerprint["build_id"] == first.build_id


def test_it11_09_tiny_build_dq_counts_match_truth(tiny_build: BuildHandle) -> None:
    """IT11-09 `tiny_build` (dirty `default`) is promoted and its `meta.dq_result` defect
    counts are within +-1 of `truth.dirty` where the build observes the defect exactly,
    bounded where tombstones, NULL writes or the clock hide part of it (module docstring)."""
    layout = DataLayout.from_root(tiny_build.data_root)
    assert warehouse.read_current(layout=layout) == tiny_build.build_id
    truth = tiny_build.truth.dirty
    dq = _dq(tiny_build)
    rbo = dq["resolved_before_opened"]
    assert abs(round(rbo["value"] * rbo["details"]["rows"]) - truth["resolved_before_opened"]) <= 1
    enum = _failed(dq, ["stg.sn_incident.priority", "stg.sn_change_request.type"])
    assert abs(enum - truth["unknown_enum"]) <= 1
    missing = dq["incident_service_null"]["details"]["null_rows"]
    assert truth["missing_service"] - truth["tombstones"] <= missing <= truth["missing_service"]
    names = [f"stg.sn_incident.{c}" for c in _INCIDENT_TS]
    names += [f"stg.sn_change_request.{c}" for c in _CHANGE_TS]
    assert 1 <= _failed(dq, names) <= truth["bad_timestamp"]
    assert 0 <= dq["future_timestamp:core.incident.opened_at"]["value"] <= truth["future_ts"]
    keys = {k: v["value"] for k, v in dq.items() if k.startswith("duplicate_key:") and v["value"]}
    assert keys == {}


def _holder(pid: int) -> str:
    return json.dumps({"host": socket.gethostname().lower(), "pid": pid})


def _hold(root: Path, seen: list[int]) -> None:
    with build_lock(root, wait_s=0.3):
        seen.append(json.loads(root.with_name(root.name + ".lock").read_text("utf-8"))["pid"])
        msg = "inside"
        raise RuntimeError(msg)


def test_it11_11_lock_held_by_a_live_process_times_out(tmp_path: Path) -> None:
    """IT11-11 (cv) a lock held by a live process is waited for, then "build lock timeout";
    the holder's lock file is left in place."""
    lock = tmp_path / "7-tiny.lock"
    lock.write_text(_holder(os.getppid()), encoding="utf-8")
    with (
        pytest.raises(BuildFixtureError, match="build lock timeout"),
        build_lock(tmp_path / "7-tiny", wait_s=0.3, poll_s=0.05),
    ):
        pass
    assert lock.read_text(encoding="utf-8") == _holder(os.getppid())


def test_it11_11_stale_lock_of_a_dead_process_is_broken(tmp_path: Path) -> None:
    """IT11-11 (cv) a lock left by a killed process on this host is removed and taken; the
    new lock names this process and is released in `finally`, also on an error."""
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)  # fmt: skip
    lock = tmp_path / "7-tiny.lock"
    lock.write_text(_holder(int(done.stdout)), encoding="utf-8")
    seen: list[int] = []
    with pytest.raises(RuntimeError, match="inside"):
        _hold(tmp_path / "7-tiny", seen)
    assert seen == [os.getpid()]
    assert not lock.exists()


def test_it11_11_fingerprint_follows_generator_params_and_files(tmp_path: Path) -> None:
    """IT11-11 (cv) the fingerprint changes with the params hash and with the content of any
    fingerprinted file (`herness/model/sql/*.sql`, `config/*.yaml`, `config/profiles/synth.yaml`),
    and not with other files."""
    files = ["herness/model/sql/100_a.sql", "config/models.yaml", "config/profiles/synth.yaml"]
    for name in [*files, "config/profiles/local.yaml", "herness/model/notes.txt"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("v1", encoding="utf-8")
    base = fingerprint(tmp_path, "sha256:p1")
    assert fingerprint(tmp_path, "sha256:p1") == base
    assert fingerprint(tmp_path, "sha256:p2") != base
    for name in ["config/profiles/local.yaml", "herness/model/notes.txt"]:
        (tmp_path / name).write_text("v2", encoding="utf-8")
    assert fingerprint(tmp_path, "sha256:p1") == base
    seen = {base}
    for name in files:
        (tmp_path / name).write_text(f"changed {name}", encoding="utf-8")
        seen.add(fingerprint(tmp_path, "sha256:p1"))
    assert len(seen) == 1 + len(files)


def _dir_link(link: Path, target: Path) -> None:
    """A directory symlink; on Windows without symlink rights a junction (the same refusal
    path); skip where neither can be made."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if sys.platform != "win32":
            pytest.skip(f"cannot create a directory symlink here: {type(exc).__name__}")
        import _winapi  # noqa: PLC0415 - Windows only

        _winapi.CreateJunction(str(target), str(link))


def test_it11_11_private_cache_dir_is_created_and_reused(tmp_path: Path) -> None:
    """IT11-11 (cv) the cache root is created private (0o700 where POSIX modes apply) and an
    existing directory of this user is accepted."""
    root = private_dir(tmp_path / "cache")
    assert root.is_dir()
    if hasattr(os, "getuid"):
        assert root.stat().st_mode & 0o777 == 0o700
    assert private_dir(root) == root


def test_it11_11_symlinked_cache_root_and_subdir_are_refused(tmp_path: Path) -> None:
    """IT11-11 (cv) `ensure_build` refuses a symlinked cache root (the `--synth-builds-dir` /
    `cache_dir` value) and a symlinked `<seed>-<scale>` subdir before writing anything."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _dir_link(tmp_path / "link", elsewhere)
    with pytest.raises(BuildFixtureError, match="symlink"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / "link")
    cache = private_dir(tmp_path / "cache")
    _dir_link(cache / "7-tiny", elsewhere)
    with pytest.raises(BuildFixtureError, match="symlink"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=cache)
    assert list(elsewhere.iterdir()) == []
    assert not (cache / "7-tiny.lock").exists()


def test_it11_11_rebuild_cleanup_does_not_delete_through_a_symlink(tmp_path: Path) -> None:
    """IT11-11 (cv) clearing old build outputs refuses a symlinked child (nothing behind it is
    deleted) and a symlinked data root; plain outputs go, the generated lake stays."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious", encoding="utf-8")
    data = private_dir(tmp_path / "data")
    (data / "raw").mkdir()
    (data / "ops.sqlite").write_text("x", encoding="utf-8")
    _dir_link(data / "warehouse", victim)
    with pytest.raises(BuildFixtureError, match="symlink"):
        clear_build_outputs(data, frozenset({"raw"}))
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "precious"
    (data / "warehouse").unlink()
    clear_build_outputs(data, frozenset({"raw"}))
    assert sorted(p.name for p in data.iterdir()) == ["raw"]
    _dir_link(tmp_path / "data_link", victim)
    with pytest.raises(BuildFixtureError, match="symlink"):
        clear_build_outputs(tmp_path / "data_link", frozenset())
    assert (victim / "keep.txt").exists()
