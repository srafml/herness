"""Private copies of a release bundle for verification and install (impl 10 U10-87, U10-88).

Size-forced private sibling of ``herness.admin.attest`` (ruling T10-26b). ``verify_bundle``
copies the operator's download directory into ``<paths.data>/.release/.verify-<ulid>/`` before
any hash or ``gh`` call, and ``install_bundle`` installs only from that copy, so a writer of the
download directory cannot swap the trust root or a subject between check and use (TH10-36,
TH10-37, TH10-40). Only regular files are copied: a link or any other entry is refused, and a
link is never followed out of the bundle.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NoReturn

from herness.core.errors import ConfigError, FatalError
from herness.core.ids import new_ulid

__all__ = ["copy_files", "private_dir", "private_dir_of", "remove_private"]

_PREFIX: Final = ".verify-"
_CHUNK: Final = 1024 * 1024
# O_NONBLOCK: a FIFO swapped in after the lstat check must not block the open.
_READ: Final = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_WRITE: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _invalid(rel: str) -> NoReturn:
    msg = f"release bundle invalid: {rel}"
    raise ConfigError(msg)


def private_dir(data_root: Path) -> Path:
    """Create a fresh ``<data_root>/.release/.verify-<ulid>/`` (mode 0o700).

    POSIX modes are advisory on NTFS: on Windows the directory inherits the ACL of the Herness
    data root (U10-90), which is why the copy lives under that root and not in the OS temp dir.
    """
    parent = data_root / ".release"
    parent.mkdir(parents=True, exist_ok=True)
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
    """Remove a private copy (best effort: it holds only public release files)."""
    shutil.rmtree(path, ignore_errors=True)


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
    """Copy each ``{bundle-relative path: file under root}`` to ``dest`` (0o600, atomic per
    file); a file that left ``root``, is not regular, or exceeds ``max_bytes`` → ConfigError."""
    for rel, source in sorted(files.items()):
        _copy_one(rel, source, root, dest / rel, max_bytes)
