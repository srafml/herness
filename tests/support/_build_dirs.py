"""Private sibling of `tests.support.builds` (U11-48, T11-17 spec note): the private per-user
cache directories and link-safe deletes, split off for the 350-line budget of `builds.py`,
which re-exports `private_dir`, `safe_rmtree` and `clear_build_outputs`.

`private_dir(path)` first checks the whole ancestor chain without resolving it (`os.lstat`,
OpenSSH StrictModes rule): a symlink component is refused unless it is owned by root or the
current user (Windows: any symlink or junction component is refused); on POSIX every
ancestor of the lexical and of the resolved path must be owned by root or the current user
and must not be group- or other-writable unless sticky (so `/tmp` passes). Missing
components are created one by one (`os.mkdir`, mode 0o700) and re-checked with `lstat`, so
no link is followed. The leaf must then be a directory owned by the current user (POSIX), and
is chmod-ed back to 0o700. Windows has no `st_uid`: the owner and mode checks are skipped
there, and a `TMP` on a shared folder is not private.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import NoReturn

from tests.support._build_cache import BuildFixtureError

__all__ = ["clear_build_outputs", "private_dir", "safe_rmtree"]

_POSIX = hasattr(os, "getuid")


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _refuse(why: str, path: Path) -> NoReturn:
    msg = f"refusing build cache path ({why}): {path}"
    raise BuildFixtureError(msg)


def _check_owner_mode(path: Path, info: os.stat_result) -> None:
    if not _POSIX:
        return
    if info.st_uid not in {0, os.getuid()}:
        _refuse("owned by another user", path)
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        _refuse("group- or other-writable ancestor without the sticky bit", path)


def _check_chain(path: Path) -> None:
    """Every existing ancestor of `path` (lexical, then resolved), root first."""
    for anc in reversed(path.absolute().parents):
        with contextlib.suppress(FileNotFoundError):
            info = os.lstat(anc)
            if stat.S_ISLNK(info.st_mode) or anc.is_junction():
                if sys.platform == "win32" or info.st_uid not in {0, os.getuid()}:
                    _refuse("symlink or junction in the parent chain", anc)
                continue  # a trusted link: its target is checked with the resolved chain
            _check_owner_mode(anc, info)
    if _POSIX:
        for anc in reversed(path.resolve().parents):
            with contextlib.suppress(FileNotFoundError):
                _check_owner_mode(anc, os.lstat(anc))


def _mkdir(path: Path) -> None:
    """Create `path` (0o700) without following a link; refuse a link or a non-directory."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if _linked(path) or not stat.S_ISDIR(info.st_mode):
        _refuse("symlink, junction or not a directory", path)


def private_dir(path: Path) -> Path:
    """Create `path` and its missing parents (0o700) or accept them after the chain check;
    refuse a link, a non-directory or (POSIX) a directory owned by another user."""
    path = path.absolute()
    _check_chain(path)
    for anc in reversed(path.parents):
        if not anc.exists():
            _mkdir(anc)
    _mkdir(path)
    info = os.lstat(path)
    if _POSIX:
        if info.st_uid != os.getuid():
            _refuse("owned by another user", path)
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
