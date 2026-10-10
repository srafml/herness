"""Blue/green venv fakes for ``install_bundle`` tests (impl 10 U10-88; Ruling T10-26c).

``use_install_root`` makes ``<root>/<running>`` play the running Herness venv: ``sys.prefix``,
``sys.executable`` and the running-interpreter constant of ``herness.admin.wsl`` point
into ``root``, so ``<root>/venvs/<version>/`` is the only place an install may create a venv and no
test can reach the real ``.venv``. ``venv_answer`` stands in for ``python -m venv <dir>`` in
``fake_runner``: it creates the interpreter file and ``pyvenv.cfg`` the real command would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from herness.admin import wsl
from herness.admin.duckdb_ext import venv_python
from herness.admin.wsl import CmdResult

__all__ = ["ext_prefix", "make_venv", "use_install_root", "venv_answer"]


def make_venv(venv: Path) -> Path:
    """An empty venv-shaped directory (interpreter file and ``pyvenv.cfg``); its interpreter."""
    python = venv_python(venv)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"")
    (venv / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")
    return python


def use_install_root(monkeypatch: pytest.MonkeyPatch, root: Path, running: str = ".venv") -> Path:
    """Make ``root/running`` the running Herness venv (a plain venv unless ``running`` is
    ``venvs/<v>`` or ``current``) and return it."""
    venv = root / running
    python = make_venv(venv) if not venv.exists() else venv_python(venv)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(wsl, "_VENV_PY", os.path.normcase(str(python)))
    return venv


def ext_prefix(venv: Path) -> list[str]:
    """The ``fake_runner`` prefix of U10-112's extension install in ``venv`` (via uv)."""
    return ["uv", "run", "--no-project", "--offline", "--python", str(venv_python(venv))]


def venv_answer(argv: list[str]) -> CmdResult:
    """``python -I -m venv --without-pip <dir>``: create the venv's interpreter and config."""
    make_venv(Path(argv[-1]))
    return CmdResult(0, "", "", 0.1)
