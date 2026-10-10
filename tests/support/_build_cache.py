"""Private sibling of `tests.support.builds` (U11-48, T11-17 spec note): the build lock,
the generator marker check and the build fingerprint, split off for the 350-line budget of
`builds.py`, which re-exports `BuildFixtureError`; imported only by `builds`.

Lock (F11-06 step 1): `<root>.lock` is created with `os.open(O_CREAT | O_EXCL)` and holds the
holder's host and pid as JSON; a waiter polls every 1 s for up to 30 min, then fails with
"build lock timeout". Stale locks: a lock whose holder pid is dead on this host
(`psutil.pid_exists`), or an empty lock older than 60 s (holder killed between create and
write), is removed and the create retried; the content is re-read just before the unlink so
only a lock unchanged since the staleness check is removed. A lock held from another host
(shared drive) is never broken: it times out. `PermissionError` counts as "exists" (Windows
reports a delete-pending file that way).

Cache location (security ruling on U11-48, a spec deviation): the persistent cache root is
per user (`<tempfile.gettempdir()>/herness-synth-builds-<user>`, or the given directory) and
is created with mode 0o700. `private_dir` refuses an existing root or `<seed>-<scale>` subdir
that is a symlink or junction, not a directory, or (POSIX only: Windows has no `st_uid`, its
per-user temp directory is already private) owned by another user. `safe_rmtree` refuses a
symlink or junction instead of deleting through it, so reuse and invalidation never follow
links. The lock file lives inside the private cache root, created 0o600.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import stat
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
FIXTURE_REV: Final = 1  # bump when `builds` changes how a build is made
LOCK_WAIT_S: Final = 30 * 60.0
LOCK_POLL_S: Final = 1.0
EMPTY_LOCK_STALE_S: Final = 60.0
Scale = Literal["tiny", "small"]


class BuildFixtureError(RuntimeError):
    """The build fixture could not lock, generate or build (the message says which)."""


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
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        msg = f"refusing build cache path owned by another user: {path}"
        raise BuildFixtureError(msg)
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


# --- lock -----------------------------------------------------------------------------------


def _holder() -> str:
    return json.dumps({"host": socket.gethostname().lower(), "pid": os.getpid()})


def _stale(text: str, lock: Path) -> bool:
    """A dead holder on this host, or an empty lock older than `EMPTY_LOCK_STALE_S`."""
    if not text.strip():
        return time.time() - lock.stat().st_mtime > EMPTY_LOCK_STALE_S
    try:
        doc = json.loads(text)
        host, pid = str(doc["host"]), int(doc["pid"])
    except (ValueError, KeyError, TypeError):
        return False  # not ours to judge: wait for the timeout
    return (
        host == socket.gethostname().lower() and pid != os.getpid() and not psutil.pid_exists(pid)
    )


def _break_stale(lock: Path) -> bool:
    """Remove `lock` when its holder is gone; True when it was removed."""
    try:
        text = lock.read_text(encoding="utf-8")
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
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except (FileExistsError, PermissionError):  # Windows: delete-pending lock file
            if _break_stale(lock):
                continue
            if time.monotonic() >= deadline:
                msg = f"build lock timeout: {lock} held for more than {wait_s:.0f} s"
                raise BuildFixtureError(msg) from None
            time.sleep(poll_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_holder())
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
            if child.is_dir():
                safe_rmtree(child)
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
