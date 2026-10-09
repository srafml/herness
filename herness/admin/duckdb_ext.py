"""Offline install of the pinned DuckDB extensions from a release bundle (impl 10 U10-112).

impl 01 O-3: the files connector reads ``.xlsx`` with ``autoinstall_known_extensions`` off,
so ``herness deploy install`` installs ``excel`` from the verified bundle. The venv's own
``duckdb`` runs ``EXT_INSTALL_SCRIPT`` in a subprocess: a local ``INSTALL '<path>'`` (no
download; unsigned extensions stay refused), ``LOAD``, and the ``duckdb_extensions()`` row.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from herness.admin.wsl import run_cmd
from herness.core.errors import ConfigError, FatalError
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.admin.attest import ReleaseBundle
    from herness.core.config import HernessConfig

__all__ = ["EXT_INSTALL_SCRIPT", "install_duckdb_extensions", "venv_python"]

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
    pins: dict[str, str] = {str(k): v for k, v in cfg.deploy.release.duckdb_extensions.items()}
    if "excel" not in pins:
        msg = "deploy.release.duckdb_extensions.excel is not pinned"
        raise ConfigError(msg)
    files: dict[str, Path] = {}
    for name in sorted(pins):  # every file is checked before any subprocess runs
        path = bundle.duckdb_extensions.get(name)
        if path is None:
            msg = f"release bundle invalid: duckdb/{name}.duckdb_extension"
            raise ConfigError(msg)
        if _sha256(path) != pins[name]:
            raise _refuse(name, "extension_hash", f"duckdb extension hash mismatch: {name}")
        files[name] = path
    python = str(venv_python(venv))
    versions: dict[str, str] = {}
    for name, path in files.items():
        argv = [python, "-I", "-c", EXT_INSTALL_SCRIPT, str(path), name]
        result = run_cmd(argv, timeout_s=120)
        if result.returncode != 0:
            raise _refuse(name, "extension_install", f"duckdb extension install failed: {name}")
        row = _installed_row(name, result.stdout)
        target = Path(str(row.get("install_path", "")))
        if not target.is_file() or _sha256(target) != pins[name]:
            msg = f"duckdb extension hash mismatch after install: {name}"
            raise _refuse(name, "extension_installed_hash", msg)
        version = str(row.get("extension_version"))
        _log.info(
            "deploy.install.extension_installed",
            name=name,
            extension_version=version,
            sha256=pins[name],
        )
        versions[name] = version
    return versions
