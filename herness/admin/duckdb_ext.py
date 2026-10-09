"""Offline install of the pinned DuckDB extensions from a release bundle (impl 10 U10-112).

impl 01 O-3: ``.xlsx`` is read with autoinstall off, so the excel extension ships in the bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.admin.wsl import run_cmd
from herness.core.errors import ConfigError, FatalError
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.admin.attest import ReleaseBundle
    from herness.core.config import HernessConfig

__all__ = ["EXT_INSTALL_SCRIPT", "check_extensions", "install_duckdb_extensions", "venv_python"]

_log = get_logger("admin.deploy")

# Run as ``python -I -c EXT_INSTALL_SCRIPT <path> <name>``. ``NO_PROXY=*`` is set here, before
# ``duckdb`` loads, because U10-76 ``run_cmd`` takes no ``env`` (T10-26 deviation).
EXT_INSTALL_SCRIPT: Final = """\
import json, os, re, sys
os.environ["NO_PROXY"] = "*"
path, name = sys.argv[1], sys.argv[2]
if re.fullmatch(r"[a-z0-9_]{1,40}", name) is None:
    sys.exit(2)
import duckdb
con = duckdb.connect(":memory:", config={
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_unsigned_extensions": False,
})
con.execute("INSTALL '" + path.replace("'", "''") + "'")
con.execute("LOAD " + name)
row = con.execute(
    "SELECT loaded, installed, extension_version, install_path"
    " FROM duckdb_extensions() WHERE extension_name = ?", [name]
).fetchone()
keys = ("loaded", "installed", "extension_version", "install_path")
sys.stdout.write(json.dumps(dict(zip(keys, row))))
"""


def venv_python(venv: Path) -> Path:
    """The interpreter of virtual environment ``venv``."""
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def _sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _refuse(name: str, reason: str, message: str) -> FatalError:
    _log.error("deploy.install.refused", reason=reason, file=f"duckdb/{name}.duckdb_extension")
    return FatalError(message)


def check_extensions(
    bundle: ReleaseBundle, cfg: HernessConfig, *, venv: Path
) -> dict[str, tuple[Path, str]]:
    """U10-112 pins, files and hashes, and ``venv`` is the running venv; before any change."""
    pins = cfg.deploy.release.duckdb_extensions
    if "excel" not in pins:
        msg = "deploy.release.duckdb_extensions.excel is not pinned"
        raise ConfigError(msg)
    files: dict[str, tuple[Path, str]] = {}
    for name, pin in sorted(pins.items()):
        path = bundle.duckdb_extensions.get(name)
        if path is None:
            msg = f"release bundle invalid: duckdb/{name}.duckdb_extension"
            raise ConfigError(msg)
        if _sha256(path) != pin:
            raise _refuse(name, "extension_hash", f"duckdb extension hash mismatch: {name}")
        files[name] = (path, pin)
    if os.path.normcase(venv_python(venv)) != os.path.normcase(sys.executable):
        msg = "venv is not the running Herness venv"
        raise ConfigError(msg)
    return files


def _installed_row(name: str, stdout: str) -> dict[str, Any]:
    try:
        row = json.loads(stdout)
    except ValueError:
        row = None
    if not (isinstance(row, dict) and row.get("loaded") is True and row.get("installed") is True):
        raise _refuse(name, "extension_install", f"duckdb extension install failed: {name}")
    return row


def install_duckdb_extensions(
    bundle: ReleaseBundle, cfg: HernessConfig, *, venv: Path
) -> dict[str, str]:
    """Install each pinned extension from the bundle; name → installed version (U10-112)."""
    files = check_extensions(bundle, cfg, venv=venv)  # all checked before any subprocess
    versions: dict[str, str] = {}
    for name, (path, pin) in files.items():
        argv = [str(venv_python(venv)), "-I", "-c", EXT_INSTALL_SCRIPT, str(path), name]
        result = run_cmd(argv, timeout_s=120)
        if result.returncode != 0:
            raise _refuse(name, "extension_install", f"duckdb extension install failed: {name}")
        row = _installed_row(name, result.stdout)
        target = Path(str(row.get("install_path", "")))
        if not target.is_file() or _sha256(target) != pin:
            msg = f"duckdb extension hash mismatch after install: {name}"
            raise _refuse(name, "extension_installed_hash", msg)
        versions[name] = version = str(row.get("extension_version"))
        _log.info(
            "deploy.install.extension_installed", name=name, extension_version=version, sha256=pin
        )
    return versions
