"""Blue/green venv install of a verified release, and venv rollback (impl 10 U10-88, U10-112).

Size-forced private sibling of ``herness.admin.attest`` (Ruling T10-26c). ``install_bundle``
never writes into ``sys.prefix``: on Windows the running CLI's loaded ``.pyd`` files are locked
(DE-10-26). It builds ``<install root>/venvs/<version>/`` beside the running venv, installs the
verified private copy there, then repoints ``<install root>/current`` (a directory junction on
Windows, a symlink elsewhere) and records the switch in the deploy history. The previous venv is
kept, so ``rollback_venv`` can point ``current`` back at it. Any failure before the repoint
leaves ``current`` untouched and removes the new venv. The process must restart to run it.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.admin import wsl
from herness.admin.deploy import DeployHistory
from herness.admin.duckdb_ext import install_duckdb_extensions, venv_python
from herness.core import time as clock
from herness.core.audit import audit
from herness.core.config import HernessConfig, config_hash, get_config
from herness.core.errors import ConfigError, FatalError, StoreBusy
from herness.core.jobs import worker_alive
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.admin.attest import ReleaseBundle

__all__ = [
    "HISTORY_CLASS",
    "build_venv",
    "install_root",
    "rollback_venv",
    "staged_venv",
]

_log = get_logger("admin.deploy")
HISTORY_CLASS: Final = "herness-venv"  # DeployHistory key of venv switches; not a GPU class
# impl 00 O-05: a ``+cu<nnn>`` local-version pin (torch) is served only by the extra index;
# ``--require-hashes`` still binds every installed file to its pinned hash (Ruling T10-26c).
_CUDA_PIN: Final = re.compile(r"==[^\s;\\]+\+cu[0-9]+")
_NEW: Final = ".current-new"
_OLD: Final = ".current-old"
_NO_ROLLBACK: Final = "no previous venv to roll back to"


def _real(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _unlink(path: Path) -> None:
    """Remove the link ``path`` (never its target); anything else is left alone."""
    if path.is_junction():
        os.rmdir(path)
    elif path.is_symlink():
        path.unlink()


def _link(target: Path, link: Path) -> None:
    """A directory junction on Windows (no privilege needed), a directory symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415 - Windows only; os has no junction call

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def install_root(running: Path) -> Path:
    """The directory holding the running venv: ``<root>/current``, ``<root>/venvs/<v>`` and a
    plain ``<root>/<venv>`` all give ``<root>`` (Ruling T10-26c; wsl's ``_VENVS`` agrees)."""
    if os.path.normcase(venv_python(running)) != os.path.normcase(sys.executable):
        msg = "venv is not the running Herness venv"
        raise ConfigError(msg)
    in_venvs = os.path.normcase(running.parent.name) == "venvs"
    return running.parent.parent if in_venvs else running.parent


def _recover(root: Path) -> None:
    """Finish a Windows switch a crash interrupted, then drop stale temporary links."""
    current, old = root / "current", root / _OLD
    if _is_link(old) and not os.path.lexists(current):
        os.rename(old, current)
        _log.warning("deploy.install.marker_restored", path=str(current))
    _unlink(old)
    _unlink(root / _NEW)


def staged_venv(running: Path, version: str) -> Path:
    """``<install root>/venvs/<version>``: not under ``sys.prefix`` and not existing yet, so no
    kept venv is ever overwritten (Ruling T10-26c)."""
    root = install_root(running)
    _recover(root)
    target = root / "venvs" / version
    if Path(_real(target)).is_relative_to(_real(sys.prefix)):
        msg = "venv would be inside the running venv"
        raise FatalError(msg)
    if os.path.lexists(target):
        msg = f"venv already exists: {version}"
        raise FatalError(msg)
    return target


def _switch(root: Path, target: Path) -> Path | None:
    """Point ``root/current`` at ``target``: a link is created beside it, then renamed over it;
    returns the previous target. Windows cannot rename a junction over a junction, so there the
    old one steps aside first and is restored if the rename fails (``_recover`` after a crash)."""
    current, new, old = root / "current", root / _NEW, root / _OLD
    if os.path.lexists(current) and not _is_link(current):
        msg = "current is not a link"
        raise FatalError(msg)
    previous = Path(os.path.realpath(current)) if _is_link(current) else None
    _link(target, new)
    if os.name != "nt":
        os.replace(new, current)  # atomic on POSIX
        return previous
    if previous is not None:
        os.rename(current, old)
    try:
        os.rename(new, current)
    except OSError:
        if _is_link(old):
            os.rename(old, current)
        _unlink(new)
        raise
    _unlink(old)
    return previous


def _remove(target: Path) -> None:
    """Remove a partial venv this install created; a failure is logged, not raised."""

    def failed(_func: object, _name: str, exc: BaseException) -> None:
        if not isinstance(exc, FileNotFoundError):
            _log.warning("deploy.install.cleanup_failed", path=str(target))

    shutil.rmtree(target, onexc=failed)
    _log.info("deploy.install.venv_removed", path=str(target))


def _record(cfg: HernessConfig, venv: Path, previous: Path | None) -> None:
    pins = {"version": venv.name, "venv": str(venv), "previous": str(previous or "")}
    DeployHistory().record(HISTORY_CLASS, pins, config_hash(cfg), clock.now())
    _log.info("deploy.install.switched", venv=str(venv), previous=pins["previous"])


def build_venv(
    copy: ReleaseBundle, cfg: HernessConfig, target: Path, recheck: Callable[[], None]
) -> dict[str, str]:
    """Create ``target``, install ``copy`` there (U10-88 steps 3-4, U10-112), re-verify with
    ``recheck``, then switch ``current``; on any failure first remove ``target``."""
    python = str(venv_python(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        venv = [sys.executable, "-I", "-m", "venv", "--without-pip", str(target)]
        wsl.run_cmd(venv, timeout_s=300, check=True)
        sync = ["uv", "pip", "sync", "--require-hashes", "--python", python]
        if _CUDA_PIN.search(copy.requirements.read_text(encoding="utf-8")):
            sync += ["--extra-index-url", cfg.deploy.release.extra_index_url]
        sync.append(str(copy.requirements))
        wsl.run_cmd(sync, timeout_s=1800, check=True)
        wheel = ["uv", "pip", "install", "--no-deps", "--python", python, str(copy.wheel)]
        wsl.run_cmd(wheel, timeout_s=600, check=True)
        versions = install_duckdb_extensions(copy, cfg, venv=target)
        recheck()
        previous = _switch(target.parent.parent, target)
    except BaseException:
        _remove(target)
        raise
    _record(cfg, target, previous)
    return {
        "duckdb_extensions": ",".join(f"{n}={v}" for n, v in sorted(versions.items())),
        "venv": str(target),
        "previous": str(previous or ""),
        "restart": "required",
    }


def rollback_venv(running: Path, *, actor: str) -> dict[str, str]:
    """Point ``current`` back at the previous venv of the newest switch (Ruling T10-26c).
    Only a kept ``<install root>/venvs/<name>`` with an interpreter is accepted."""
    cfg = get_config()
    root = install_root(running)
    if worker_alive():
        msg = "stop the worker before switching venvs"
        raise StoreBusy(msg)
    _recover(root)
    current = root / "current"
    newest = next(iter(DeployHistory().last_promoted(HISTORY_CLASS, 1)), {})
    previous = Path(newest.get("previous") or root / "missing")
    ok = bool(newest) and _is_link(current) and _real(current) == _real(newest["venv"])
    ok = ok and _real(previous.parent) == _real(root / "venvs")
    if not (ok and venv_python(previous).is_file()):
        raise ConfigError(_NO_ROLLBACK)
    audit("admin_action", actor, action="deploy_rollback", target=f"venv:{previous.name}")
    rolled_from = _switch(root, previous)
    _record(cfg, previous, rolled_from)
    return {"venv": str(previous), "previous": str(rolled_from), "restart": "required"}
