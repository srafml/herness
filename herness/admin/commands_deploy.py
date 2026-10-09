"""``herness deploy *`` behavior: thin wrappers over the deploy units (impl 10 U10-72).

Bodies only: impl 09 checks the role (``install`` is admin (OS)), renders the
``CommandResult`` and maps the errors raised here to exit codes (R-46, R-47).
"""

from __future__ import annotations

import sys
from pathlib import Path

from herness._cli.output import CommandResult
from herness.admin.attest import ReleaseBundle, install_bundle, verify_bundle
from herness.admin.deploy_pull import run_pull
from herness.core.config import HernessConfig

__all__ = ["cmd_deploy_install", "cmd_deploy_pull"]


def _venv_dir() -> Path:
    """The Herness virtual environment: the one this CLI runs in (R-58)."""
    return Path(sys.prefix)


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
