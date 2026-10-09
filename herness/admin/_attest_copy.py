"""Private copies of a release bundle for verification and install (impl 10 U10-87, U10-88).

Size-forced private sibling of ``herness.admin.attest`` (ruling T10-26b). ``verify_bundle``
copies the operator's download directory into ``<paths.data>/.release/.verify-<ulid>/`` before
any hash or ``gh`` call, and ``install_bundle`` installs only from that copy, so a writer of the
download directory cannot swap the trust root or a subject between check and use (TH10-36,
TH10-37, TH10-40). Only regular files are copied: a link or any other entry is refused, and a
link is never followed out of the bundle. Copies older than 24 h are swept (review round 1).
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NoReturn

from herness.core import time as clock
from herness.core.errors import ConfigError, FatalError
from herness.core.ids import CROCKFORD_ALPHABET, is_valid_ulid, new_ulid
from herness.core.logging import get_logger

__all__ = ["copy_files", "private_dir", "private_dir_of", "remove_private"]

_log = get_logger("admin.deploy")
_PREFIX: Final = ".verify-"
_TTL_S: Final = 24 * 3600
_CHUNK: Final = 1024 * 1024
_BIN: Final[int] = getattr(os, "O_BINARY", 0)  # O_NONBLOCK: a FIFO swapped in must not block
_READ: Final = os.O_RDONLY | _BIN | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_WRITE: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BIN


def _invalid(rel: str) -> NoReturn:
    msg = f"release bundle invalid: {rel}"
    raise ConfigError(msg)


def _age_s(path: Path, now: float) -> float:
    """Age from the ULID time in the name (an in-flight copy is never old); else from mtime."""
    ulid = path.name.removeprefix(_PREFIX)
    if is_valid_ulid(ulid):
        ms = sum(CROCKFORD_ALPHABET.index(c) << 5 * (9 - i) for i, c in enumerate(ulid[:10]))
        return now - ms / 1000
    try:
        return now - path.lstat().st_mtime
    except OSError:  # removed meanwhile by a concurrent sweep
        return 0.0


def private_dir(data_root: Path) -> Path:
    """Sweep copies older than 24 h, then create ``<data_root>/.release/.verify-<ulid>/`` (0o700).
    NTFS ignores the mode: there the copy relies on the data root's ACL (U10-90)."""
    parent = data_root / ".release"
    parent.mkdir(parents=True, exist_ok=True)
    now = clock.now().timestamp()
    for old in parent.glob(f"{_PREFIX}*"):
        if _age_s(old, now) > _TTL_S:
            _log.info("deploy.install.swept", path=str(old))
            remove_private(old)
    path = parent / f"{_PREFIX}{new_ulid()}"
    path.mkdir(mode=0o700)
    return path


def private_dir_of(location: object, data_root: Path) -> Path:
    """The private copy named by a ``verify_bundle`` summary; anything else is refused."""
    path = Path(str(location))
    parent = (data_root / ".release").resolve()
    if not (path.name.startswith(_PREFIX) and path.parent.resolve() == parent):
        msg = "bundle changed after verification"
        raise FatalError(msg)
    return path


def remove_private(path: Path) -> None:
    """Remove a private copy; a failure is logged, not raised (it holds public release files)."""

    def failed(_func: object, _name: str, exc: BaseException) -> None:
        if not isinstance(exc, FileNotFoundError):
            _log.warning("deploy.install.cleanup_failed", path=str(path))

    shutil.rmtree(path, onexc=failed)


def _copy_one(rel: str, source: Path, root: Path, target: Path, max_bytes: int) -> None:
    try:
        inside = source.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        before = os.lstat(source)
        if not (inside and stat.S_ISREG(before.st_mode)):
            _invalid(rel)
        fd = os.open(source, _READ)
    except OSError:
        _invalid(rel)
    with os.fdopen(fd, "rb") as src:
        after = os.fstat(src.fileno())  # the file actually opened is the one checked above
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            _invalid(rel)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.unlink(missing_ok=True)
        with os.fdopen(os.open(tmp, _WRITE, 0o600), "wb") as dst:
            size = 0
            while chunk := src.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    break
                dst.write(chunk)
    if size > max_bytes:
        tmp.unlink()
        _invalid(rel)
    os.replace(tmp, target)


def copy_files(files: Mapping[str, Path], root: Path, dest: Path, max_bytes: int) -> None:
    """Copy ``{rel: file under root}`` to ``dest/rel`` (0o600, atomic); else ConfigError."""
    for rel, source in sorted(files.items()):
        _copy_one(rel, source, root, dest / rel, max_bytes)
