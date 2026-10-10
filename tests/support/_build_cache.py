"""Private sibling of `tests.support.builds` (U11-48, T11-17 spec note): the build lock,
the generator marker check and the build fingerprint, split off for the 350-line budget of
`builds.py`, which re-exports `BuildFixtureError`; imported by `builds`, `_build_dirs` and
the T11-17 tests.

Lock (F11-06 step 1): `<root>.lock` is created with `os.open(O_CREAT | O_EXCL)` and holds the
holder's host, pid and process create time as JSON; a waiter polls every 1 s for up to
30 min, then fails with "build lock timeout". Stale locks: a lock whose holder is gone on
this host (no such pid, or the pid was reused: create time differs), or an empty lock older
than 60 s (holder killed between create and write), is removed and the create retried; the
content is re-read just before the unlink so only a lock unchanged since the staleness check
is removed. A live holder that is this process or one of its ancestors fails at once (a
spawn child re-running `ensure_build`). A lock held from another host (shared drive) is never
broken: it times out. `PermissionError` counts as "exists" while the lock path exists
(Windows reports a delete-pending file that way); with no lock path it fails at once.

The lock file lives inside the private cache root (`builds.private_dir`), created 0o600;
clearing a partial generated root refuses a linked child instead of deleting through it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Literal

import psutil

from herness.core import config as herness_config
from herness.core.errors import HernessError, SchemaViolation
from herness.store import warehouse
from herness.store.layout import DataLayout
from tools.synth import GENERATOR_VERSION
from tools.synth._cli_subcommands import DEFAULT_END, DEFAULT_START
from tools.synth._manifest import MARKER
from tools.synth.params import SOURCES, load_params, params_hash

FINGERPRINT: Final = ".fixture_build.json"
FIXTURE_REV: Final = 3  # bump when `builds` changes how a build is made (3: stub script book)
LOCK_WAIT_S: Final = 30 * 60.0
LOCK_POLL_S: Final = 1.0
EMPTY_LOCK_STALE_S: Final = 60.0
Scale = Literal["tiny", "small"]


_os_open = os.open  # module seam: the permission test patches this, not `os.open`


class BuildFixtureError(RuntimeError):
    """The build fixture could not lock, generate or build (the message says which)."""


# --- lock -----------------------------------------------------------------------------------


def _host() -> str:
    return socket.gethostname().lower()


def holder_json(pid: int) -> str:
    """Lock content: host, pid and the process create time (a reused pid does not match)."""
    return json.dumps({"host": _host(), "pid": pid, "created": psutil.Process(pid).create_time()})


def _local_holder(text: str) -> tuple[int, float | None] | None:
    """`(pid, created)` of a holder on this host; None for another host or unreadable text."""
    try:
        doc = json.loads(text)
        if str(doc["host"]) != _host():
            return None
        created = doc.get("created")
        return int(doc["pid"]), None if created is None else float(created)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _alive(pid: int, created: float | None) -> bool:
    """The holder process still runs (same pid and, when recorded, same create time)."""
    try:
        started = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:  # exists but cannot be inspected: assume alive
        return True
    return created is None or abs(started - created) < 1e-3


def _check_reentry(text: str) -> None:
    """Fail at once when this process or an ancestor holds the lock (a spawn child without a
    `__main__` guard re-running `ensure_build` would otherwise wait 30 min)."""
    holder = _local_holder(text)
    if holder is None or not _alive(*holder):
        return
    try:
        mine = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    except psutil.Error:  # an ancestor cannot be inspected: not re-entry, keep polling
        return
    if holder[0] in mine:
        msg = (
            "build lock held by this process or an ancestor: ensure_build re-entered"
            " (missing `if __name__ == '__main__':` guard in a spawn child?)"
        )
        raise BuildFixtureError(msg)


def _stale(text: str, lock: Path) -> bool:
    """A dead holder on this host, or an empty lock older than `EMPTY_LOCK_STALE_S`."""
    if not text.strip():
        return time.time() - lock.stat().st_mtime > EMPTY_LOCK_STALE_S
    holder = _local_holder(text)
    return holder is not None and holder[0] != os.getpid() and not _alive(*holder)


def _lock_present(lock: Path) -> bool:
    """False only when the lock path is truly absent (Windows reports a delete-pending file
    with an access error: present)."""
    try:
        os.lstat(lock)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _break_stale(lock: Path) -> bool:
    """Remove `lock` when its holder is gone; True when it was removed. Raises on re-entry."""
    try:
        text = lock.read_text(encoding="utf-8")
        _check_reentry(text)
        if not _stale(text, lock) or lock.read_text(encoding="utf-8") != text:
            return False
        lock.unlink()
    except OSError:  # vanished or held open meanwhile: just poll again
        return False
    return True


@contextlib.contextmanager
def build_lock(
    root: Path, *, wait_s: float = LOCK_WAIT_S, poll_s: float = LOCK_POLL_S
) -> Iterator[Path]:
    """Hold `<root>.lock` (O_CREAT | O_EXCL) for the block; "build lock timeout" after `wait_s`."""
    lock = root.with_name(root.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = _os_open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except (FileExistsError, PermissionError) as exc:  # Windows: delete-pending lock file
            if isinstance(exc, PermissionError) and not _lock_present(lock):
                msg = f"cannot create the build lock (permission denied): {lock}"
                raise BuildFixtureError(msg) from exc
            if _break_stale(lock):
                continue
            if time.monotonic() >= deadline:
                msg = f"build lock timeout: {lock} held for more than {wait_s:.0f} s"
                raise BuildFixtureError(msg) from None
            time.sleep(poll_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(holder_json(os.getpid()))
        yield lock
    finally:
        lock.unlink(missing_ok=True)


# --- generate and fingerprint ---------------------------------------------------------------


def default_params_hash(scale: Scale) -> str:
    """The params hash `generate` records for `scale` with every default (dirty `default`)."""
    params = load_params(scale, start=DEFAULT_START, end=DEFAULT_END, sources=SOURCES,
                         dirty="default", fetch_mode="initial", params_file=None)  # fmt: skip
    return params_hash(params)


def marker_current(root: Path, phash: str) -> bool:
    try:
        body = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(body, dict) and (body.get("generator_version"), body.get("params_hash")) == (
        GENERATOR_VERSION,
        phash,
    )


def generate_root(seed: int, scale: Scale, root: Path) -> None:
    from tools.synth_data import generate  # noqa: PLC0415 - heavy, only when generating

    if not (root / MARKER).is_file() and any(root.iterdir()):
        for child in root.iterdir():  # a partial root: `overwrite` needs the marker
            if child.is_symlink() or child.is_junction():
                msg = f"refusing to delete through a symlink or junction: {child}"
                raise BuildFixtureError(msg)
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    try:
        generate(seed, scale, root, overwrite=True)
    except HernessError as exc:
        msg = f"synthetic generation failed: {exc}"
        raise BuildFixtureError(msg) from exc
    finally:
        herness_config.reset_config()  # generate caches the synth profile's config


def fingerprint(repo_root: Path, phash: str) -> str:
    """SHA-256 over the generator version, params hash, `FIXTURE_REV` and fingerprinted files."""
    files = [
        *repo_root.glob("herness/model/sql/*.sql"),
        *repo_root.glob("herness/model/sql/*.jinja"),
    ]
    files += [*repo_root.glob("config/*.yaml"), repo_root / "config" / "profiles" / "synth.yaml"]
    files += repo_root.glob("tests/fixtures/llm_scripts/build/*.yaml")  # the stub LLM's book
    entries = sorted(
        f"{p.relative_to(repo_root).as_posix()} {hashlib.sha256(p.read_bytes()).hexdigest()}"
        for p in files
    )
    text = "\n".join([GENERATOR_VERSION, phash, f"rev {FIXTURE_REV}", *entries])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reusable(data_root: Path, print_: str) -> str | None:
    """The recorded build id when the fingerprint matches and its warehouse exists."""
    try:
        doc = json.loads((data_root / FINGERPRINT).read_text(encoding="utf-8"))
        build_id = str(doc["build_id"])
        path = warehouse.build_path(build_id, layout=DataLayout.from_root(data_root))
    except (OSError, ValueError, KeyError, TypeError, SchemaViolation):
        return None
    return build_id if doc.get("fingerprint") == print_ and path.is_file() else None


def write_fingerprint(data_root: Path, print_: str, build_id: str) -> None:
    path = data_root / FINGERPRINT
    tmp = path.with_name(FINGERPRINT + ".tmp")
    tmp.write_text(json.dumps({"fingerprint": print_, "build_id": build_id}), encoding="utf-8")
    os.replace(tmp, path)
