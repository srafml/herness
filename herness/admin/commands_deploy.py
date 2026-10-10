"""``herness deploy *`` behavior: thin wrappers over the deploy units (impl 10 U10-72).

Bodies only: impl 09 checks the role (``install`` is admin (OS)), renders the
``CommandResult`` and maps the errors raised here to exit codes (R-46, R-47).
"""

from __future__ import annotations

import sys
from pathlib import Path

from herness._cli.output import CommandResult
from herness.admin.attest import ReleaseBundle, install_bundle, verify_bundle
from herness.admin.deploy import (
    DeployClass,
    class_down,
    class_up,
    prune,
    render_env,
    rollback_class,
)
from herness.admin.deploy_pull import run_pull
from herness.core import time as clock
from herness.core.config import HernessConfig

__all__ = ["cmd_deploy_down", "cmd_deploy_install", "cmd_deploy_prune", "cmd_deploy_pull"]
__all__ += ["cmd_deploy_render", "cmd_deploy_rollback", "cmd_deploy_up"]


def _venv_dir() -> Path:
    """The Herness virtual environment: the one this CLI runs in (R-58)."""
    return Path(sys.prefix)


def _ok(data: dict[str, object]) -> CommandResult:
    return CommandResult(ok=True, data=data, warnings=[], exit_code=0)


def cmd_deploy_render(*, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy render`` (U10-72): U10-79 ``render_env`` (key names only)."""
    return _ok(dict(render_env(cfg, actor=actor)))


def cmd_deploy_up(gpu_class: DeployClass, *, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy up <class>`` (U10-72): U10-84 ``class_up``."""
    return _ok(dict(class_up(cfg, gpu_class, actor=actor, now=clock.now())))


def cmd_deploy_down(gpu_class: DeployClass, *, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy down <class>`` (U10-72): U10-84 ``class_down``."""
    return _ok(dict(class_down(cfg, gpu_class, actor=actor)))


def cmd_deploy_rollback(gpu_class: DeployClass, *, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy rollback <class>`` (U10-72): U10-85 ``rollback_class``."""
    return _ok(dict(rollback_class(cfg, gpu_class, actor=actor, now=clock.now())))


def cmd_deploy_prune(*, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy prune`` (U10-72): U10-85 ``prune``; ``data`` holds the counts."""
    return _ok(dict(prune(cfg, actor=actor)))


def cmd_deploy_pull(*, cfg: HernessConfig, actor: str, allow_download: bool) -> CommandResult:
    """``herness deploy pull --allow-download`` (U10-72): U10-92 ``run_pull``."""
    data: dict[str, object] = dict(run_pull(cfg, allow_download=allow_download, actor=actor))
    return CommandResult(ok=True, data=data, warnings=[], exit_code=0)


def cmd_deploy_install(bundle_dir: Path, *, cfg: HernessConfig, actor: str) -> CommandResult:
    """``herness deploy install BUNDLE`` (U10-72; F10-07): load, verify, then install."""
    bundle = ReleaseBundle.load(bundle_dir)
    summary = verify_bundle(bundle, cfg)
    installed = install_bundle(bundle, summary, venv=_venv_dir(), actor=actor)
    data: dict[str, object] = dict(installed)
    return CommandResult(ok=True, data=data, warnings=[], exit_code=0)
