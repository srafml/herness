"""Build fixtures through the real pipeline (impl 11 IT11-11, IT11-09; U11-48, T11-17).

IT11-11 builds seed 7 `tiny` into a fresh per-test cache root and calls `ensure_build` twice;
IT11-09 reads `meta.dq_result` of the session `tiny_build` (dirty `default`).

Truth key -> `meta.dq_result` check, as in impl 02 IT02-31 (`tests/integration/
test_build_dirty.py`, T11-17 spec note on IT11-09): the generator counts a defect on the
record it mutates while the build sees the deduplicated final rows, so only
`resolved_before_opened` and `unknown_enum` are observable to +-1 (and at least 1 when truth
plants one); `missing_service` lies in [truth - tombstones, truth]; `bad_timestamp` writes a
bad date, epoch milliseconds or an empty value, one third each, so on the 4 tiny writes only
1..truth failed casts hold; `future_ts` lies in [0, truth], at least 1 while the raw lake
still holds an incident opened after now (the clock hides the planted row once its shifted
`opened_at` passes); `duplicate_rows`, `later_versions` and `tombstones` have no DQ
counterpart (staging dedupe removes them; `duplicate_key:*` stays 0).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support import _build_cache, _build_dirs
from tests.support._build_cache import FIXTURE_REV, build_lock, fingerprint, holder_json
from tests.support.builds import (
    REPO,
    BuildFixtureError,
    BuildHandle,
    build_root,
    clear_build_outputs,
    default_cache_dir,
    ensure_build,
    private_dir,
)

from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

_INCIDENT_TS = ("opened_at", "acknowledged_at", "resolved_at", "closed_at")
_CHANGE_TS = ("opened_at", "planned_start", "planned_end", "actual_start", "actual_end")
# FIXTURE_REV -> base32 SHA-256 of builds.py + _build_cache.py (+ _build_stubs.py from rev 2;
# LF). A change to them fails this pin: bump FIXTURE_REV when the change alters how builds
# are made, then re-pin.
_SOURCE_PIN = {
    1: "ucacjh275w565zygyagr2olgvsptypywf4tarh2tseqlz3gbuk6a",  # pragma: allowlist secret
    2: "mz24cep43nsn3i64e3ze6e2vcdoy7rizrfpt6vukq37ybtx4ciqq",  # pragma: allowlist secret
}


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


def _raw_future_incidents(data_root: Path) -> int:
    pattern = (data_root / "raw" / "servicenow" / "incident" / "**" / "*.parquet").as_posix()
    sql = "SELECT count(*) FROM read_parquet(?) WHERE try_cast(opened_at AS TIMESTAMP) > now()"
    row = duckdb.connect().execute(sql, [pattern]).fetchone()
    return int(row[0]) if row else 0


def test_it11_11_second_ensure_build_reuses_the_build(tmp_path: Path) -> None:
    """IT11-11 a clean per-test cache root built once: the second `ensure_build` reuses the
    build (same build id and warehouse, no further `build_pipeline` job enqueued)."""
    first = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert first.root == build_root(tmp_path, 7, "tiny", repo_root=REPO)
    assert first.root.name.startswith("7-tiny-")
    assert first.warehouse.is_file()
    jobs = _build_jobs(first.data_root)
    assert [status for _, status in jobs] == ["done"]
    stamp = first.warehouse.stat().st_mtime_ns
    second = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert second == first
    assert _build_jobs(second.data_root) == jobs
    assert second.warehouse.stat().st_mtime_ns == stamp
    assert not first.root.with_name(first.root.name + ".lock").exists()
    recorded = json.loads((first.data_root / ".fixture_build.json").read_text("utf-8"))
    assert recorded["build_id"] == first.build_id


def test_it11_11_another_fingerprint_builds_beside_a_build_in_use(tmp_path: Path) -> None:
    """IT11-11 (cv) a session with another fingerprint (another checkout) builds its own slot
    and leaves the first slot's warehouse, ops store and truth untouched and readable."""
    first = ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / "cache")
    jobs = _build_jobs(first.data_root)
    other_repo = tmp_path / "other_checkout"
    (other_repo / "config" / "profiles").mkdir(parents=True)
    (other_repo / "config" / "profiles" / "synth.yaml").write_text("version: 1", encoding="utf-8")
    (other_repo / "config" / "models.yaml").write_text("changed", encoding="utf-8")
    other = ensure_build(7, "tiny", repo_root=other_repo, cache_dir=tmp_path / "cache")
    assert other.root != first.root
    assert other.root.parent == first.root.parent
    assert first.warehouse.is_file()
    assert _build_jobs(first.data_root) == jobs
    assert _dq(first)
    assert (
        first.truth == ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / "cache").truth
    )
    assert ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / "cache") == first


def test_it11_09_tiny_build_dq_counts_match_truth(tiny_build: BuildHandle) -> None:
    """IT11-09 `tiny_build` (dirty `default`) is promoted and its `meta.dq_result` defect
    counts match `truth.dirty`: +-1 (and >= 1) where the build observes the defect exactly,
    bounded where tombstones, NULL writes or the clock hide part of it (module docstring)."""
    layout = DataLayout.from_root(tiny_build.data_root)
    assert warehouse.read_current(layout=layout) == tiny_build.build_id
    truth = tiny_build.truth.dirty
    keys = ("resolved_before_opened", "unknown_enum", "missing_service", "tombstones")
    assert all(truth[k] > 0 for k in (*keys, "bad_timestamp", "future_ts")), truth
    dq = _dq(tiny_build)
    rbo = dq["resolved_before_opened"]
    observed = {
        "resolved_before_opened": round(rbo["value"] * rbo["details"]["rows"]),
        "unknown_enum": _failed(dq, ["stg.sn_incident.priority", "stg.sn_change_request.type"]),
    }
    for key, count in observed.items():
        assert max(truth[key] - 1, 1) <= count <= truth[key] + 1, (key, count, truth[key])
    missing = dq["incident_service_null"]["details"]["null_rows"]
    assert truth["missing_service"] - truth["tombstones"] <= missing <= truth["missing_service"]
    names = [f"stg.sn_incident.{c}" for c in _INCIDENT_TS]
    names += [f"stg.sn_change_request.{c}" for c in _CHANGE_TS]
    assert 1 <= _failed(dq, names) <= truth["bad_timestamp"]
    future = dq["future_timestamp:core.incident.opened_at"]["value"]
    low = 1 if _raw_future_incidents(tiny_build.data_root) else 0
    assert low <= future <= truth["future_ts"]
    dupes = {k: v["value"] for k, v in dq.items() if k.startswith("duplicate_key:") and v["value"]}
    assert dupes == {}


# --- lock -----------------------------------------------------------------------------------


@pytest.fixture
def sleeper() -> Iterator[int]:
    """A live process that is not an ancestor of this one; killed afterwards."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    proc = subprocess.Popen(cmd)  # noqa: S603 - fixed argv, no shell
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def _hold(root: Path, seen: list[int]) -> None:
    with build_lock(root, wait_s=0.3):
        seen.append(json.loads(root.with_name(root.name + ".lock").read_text("utf-8"))["pid"])
        msg = "inside"
        raise RuntimeError(msg)


def test_it11_11_lock_held_by_a_live_process_times_out(tmp_path: Path, sleeper: int) -> None:
    """IT11-11 (cv) a lock held by another live process is waited for, then "build lock
    timeout"; the holder's lock file is left in place."""
    lock = tmp_path / "7-tiny.lock"
    held = holder_json(sleeper)
    lock.write_text(held, encoding="utf-8")
    with (
        pytest.raises(BuildFixtureError, match="build lock timeout"),
        build_lock(tmp_path / "7-tiny", wait_s=0.3, poll_s=0.05),
    ):
        pass
    assert lock.read_text(encoding="utf-8") == held


def test_it11_11_stale_lock_of_a_dead_process_is_broken(tmp_path: Path) -> None:
    """IT11-11 (cv) a lock left by a killed process on this host is removed and taken; the
    new lock names this process and is released in `finally`, also on an error."""
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)  # fmt: skip
    lock = tmp_path / "7-tiny.lock"
    lock.write_text(json.dumps({"host": socket.gethostname().lower(), "pid": int(done.stdout)}))
    seen: list[int] = []
    with pytest.raises(RuntimeError, match="inside"):
        _hold(tmp_path / "7-tiny", seen)
    assert seen == [os.getpid()]
    assert not lock.exists()


def test_it11_11_lock_of_a_reused_pid_is_broken(tmp_path: Path, sleeper: int) -> None:
    """IT11-11 (cv) a lock whose pid now belongs to another process (create time differs)
    counts as stale and is taken at once."""
    doc = json.loads(holder_json(sleeper)) | {"created": 1.0}
    (tmp_path / "7-tiny.lock").write_text(json.dumps(doc), encoding="utf-8")
    seen: list[int] = []
    with pytest.raises(RuntimeError, match="inside"):
        _hold(tmp_path / "7-tiny", seen)
    assert seen == [os.getpid()]


def test_it11_11_reentry_from_a_child_fails_fast(tmp_path: Path) -> None:
    """IT11-11 (cv) a lock held by this process's parent (a spawn child re-running
    `ensure_build` without a `__main__` guard) fails at once, without waiting."""
    (tmp_path / "7-tiny.lock").write_text(holder_json(os.getppid()), encoding="utf-8")
    with (
        pytest.raises(BuildFixtureError, match="re-entered"),
        build_lock(tmp_path / "7-tiny", wait_s=5, poll_s=0.05),
    ):
        pass


def test_it11_11_lock_permission_failure_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 (cv) `PermissionError` with no lock file is a real failure, not a wait."""

    def denied(*_args: object) -> int:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(_build_cache, "_os_open", denied)
    with (
        pytest.raises(BuildFixtureError, match="permission denied"),
        build_lock(tmp_path / "7-tiny", wait_s=5),
    ):
        pass


# --- fingerprint ----------------------------------------------------------------------------


def test_it11_11_fingerprint_follows_generator_params_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 (cv) the fingerprint changes with `GENERATOR_VERSION`, `FIXTURE_REV`, the
    params hash and the content of any fingerprinted file (`herness/model/sql/*.sql` and
    `*.jinja`, `config/*.yaml`, `config/profiles/synth.yaml`), and not with other files."""
    files = ["herness/model/sql/100_a.sql", "herness/model/sql/_macros.jinja",
             "config/models.yaml", "config/profiles/synth.yaml"]  # fmt: skip
    for name in [*files, "config/profiles/local.yaml", "herness/model/notes.txt"]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("v1", encoding="utf-8")
    base = fingerprint(tmp_path, "sha256:p1")
    assert fingerprint(tmp_path, "sha256:p1") == base
    assert fingerprint(tmp_path, "sha256:p2") != base
    with monkeypatch.context() as patch:
        patch.setattr(_build_cache, "GENERATOR_VERSION", "9.9.9")
        assert fingerprint(tmp_path, "sha256:p1") != base
    with monkeypatch.context() as patch:
        patch.setattr(_build_cache, "FIXTURE_REV", FIXTURE_REV + 1)
        assert fingerprint(tmp_path, "sha256:p1") != base
    for name in ["config/profiles/local.yaml", "herness/model/notes.txt"]:
        (tmp_path / name).write_text("v2", encoding="utf-8")
    assert fingerprint(tmp_path, "sha256:p1") == base
    seen = {base}
    for name in files:
        (tmp_path / name).write_text(f"changed {name}", encoding="utf-8")
        seen.add(fingerprint(tmp_path, "sha256:p1"))
    assert len(seen) == 1 + len(files)


def test_it11_11_fixture_rev_is_pinned_to_the_build_sources() -> None:
    """IT11-11 (cv) `FIXTURE_REV` convention: the pinned hash of `builds.py`,
    `_build_cache.py` and `_build_stubs.py` must match, so a change to how builds are made is
    a deliberate re-pin
    (and a `FIXTURE_REV` bump when cached builds must not be reused)."""
    support = REPO / "tests" / "support"
    data = b"".join(
        (support / n).read_bytes() for n in ("builds.py", "_build_cache.py", "_build_stubs.py")
    )
    digest = hashlib.sha256(data.replace(b"\r\n", b"\n")).digest()
    pin = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    assert _SOURCE_PIN.get(FIXTURE_REV) == pin, f"FIXTURE_REV {FIXTURE_REV}: re-pin to {pin}"


# --- private cache directories --------------------------------------------------------------


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
    """IT11-11 (cv) the cache root is created private (0o700 where POSIX modes apply), an
    existing directory of this user is accepted and (POSIX) made private again."""
    root = private_dir(tmp_path / "cache")
    assert root.is_dir()
    assert private_dir(root) == root
    if hasattr(os, "getuid"):
        assert root.stat().st_mode & 0o777 == 0o700
        root.chmod(0o755)
        private_dir(root)
        assert root.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX owner check (no st_uid on Windows)")
def test_it11_11_cache_dir_of_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 (cv) an existing cache directory owned by another user is refused."""
    (tmp_path / "cache").mkdir()
    uid = (tmp_path / "cache").stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    with pytest.raises(BuildFixtureError, match="another user"):
        private_dir(tmp_path / "cache")


def test_it11_11_symlinked_cache_root_and_subdir_are_refused(tmp_path: Path) -> None:
    """IT11-11 (cv) `ensure_build` refuses a symlinked cache root (the `--synth-builds-dir` /
    `cache_dir` value) and a symlinked slot subdir before writing anything."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _dir_link(tmp_path / "link", elsewhere)
    with pytest.raises(BuildFixtureError, match="symlink"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path / "link")
    cache = private_dir(tmp_path / "cache")
    slot = build_root(cache, 7, "tiny", repo_root=REPO)
    _dir_link(slot, elsewhere)
    with pytest.raises(BuildFixtureError, match="symlink"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=cache)
    assert list(elsewhere.iterdir()) == []
    assert not slot.with_name(slot.name + ".lock").exists()


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


def test_it11_11_link_in_the_parent_chain_to_an_unsafe_dir_is_refused(tmp_path: Path) -> None:
    """IT11-11 (cv) a cache dir reached through a link is refused: on Windows any symlink or
    junction in the chain; on POSIX a link whose target chain is writable by others."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # no sticky bit (a no-op on Windows)
    _dir_link(tmp_path / "link", shared)
    with pytest.raises(BuildFixtureError, match=r"parent chain|sticky"):
        private_dir(tmp_path / "link" / "cache")
    assert not (shared / "cache").exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX modes and owners (Windows: none)")
def test_it11_11_writable_or_foreign_ancestor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 (cv) OpenSSH StrictModes rule on the ancestors: a group/other-writable parent
    without the sticky bit is refused, a sticky one (like /tmp) is accepted, and a parent
    owned by another (non-root) user is refused."""
    parent = tmp_path / "parent"
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(BuildFixtureError, match="sticky"):
        private_dir(parent / "cache")
    parent.chmod(0o1777)
    assert private_dir(parent / "cache").is_dir()
    _foreign(monkeypatch, parent)  # only `parent` itself reports another (non-root) owner
    with pytest.raises(BuildFixtureError, match="another user") as info:
        private_dir(parent / "other")
    assert str(info.value).endswith(str(parent))


def _foreign(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """`_build_dirs._lstat` reports `target` as owned by another, non-root uid."""
    real = _build_dirs._lstat

    def lstat(path: object) -> os.stat_result:
        info = real(path)  # type: ignore[arg-type]
        if Path(str(path)) != target:
            return info
        fields = list(info)
        fields[4] = os.getuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(_build_dirs, "_lstat", lstat)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX owners (Windows: none)")
def test_it11_11_parent_created_by_another_user_in_the_race_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-11 (cv) a missing parent that turns out (after the chain check) to be another
    user's directory is refused before the leaf is created inside it."""
    _foreign(monkeypatch, tmp_path / "a")
    with pytest.raises(BuildFixtureError, match="another user") as info:
        private_dir(tmp_path / "a" / "cache")
    assert str(info.value).endswith(str(tmp_path / "a"))
    assert not (tmp_path / "a" / "cache").exists()


def test_it11_11_default_cache_dir_passes_the_chain_check() -> None:
    """IT11-11 (cv) the default per-user root under the system temp dir passes the checks."""
    assert private_dir(default_cache_dir()).is_dir()
