"""``herness init`` steps (impl 09 U09-93 row ``init``; OI-04, OI-05); private sibling of
``cmd_system`` (T09-22, budget split).

The config templates are the repository's shipped ``config/`` tree (T10-13: ``*.yaml``,
``injection_patterns.txt``, ``profiles/*.yaml``), resolved from the checkout root: the wheel
packages only ``herness/``, so ``init`` runs from a Herness checkout (OI-04 default).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.errors import ConfigError

if TYPE_CHECKING:
    from herness.cli import GlobalOptions

__all__ = ["DATA_SUBDIRS", "TEMPLATE_DIR", "copy_templates", "run_init", "template_files"]

TEMPLATE_DIR: Path = Path(__file__).resolve().parents[2] / "config"  # a seam for tests
DATA_SUBDIRS: Final = (
    "inbox", "raw", "cache/decisions", "labels", "warehouse", "reports", "traces", "logs",
    "config_snapshots", "models", "vectors", "bench", "synth", "locks",
)  # fmt: skip  # spec 00 §4


def template_files() -> list[Path]:
    """The config template set: ``*.yaml``, ``injection_patterns.txt``, ``profiles/*.yaml``."""
    if not (TEMPLATE_DIR / "herness.yaml").is_file():
        msg = "config templates not found (run `herness init` from a Herness checkout)"
        raise ConfigError(msg, hint="Run `herness init` from the Herness checkout.")
    found = [*TEMPLATE_DIR.glob("*.yaml"), *TEMPLATE_DIR.glob("profiles/*.yaml")]
    patterns = TEMPLATE_DIR / "injection_patterns.txt"
    return sorted([*found, *([patterns] if patterns.is_file() else [])])


def copy_templates(config_dir: Path, *, force: bool) -> tuple[list[str], list[str]]:
    """Copy each absent template; with ``force`` an existing file is first renamed to
    ``<name>.bak-<YYYYMMDDTHHMMSSZ>`` (UTC) and then replaced. Returns (copied, backed_up)."""
    copied: list[str] = []
    backed_up: list[str] = []
    stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
    for src in template_files():
        rel = src.relative_to(TEMPLATE_DIR)
        dst = config_dir / rel
        if dst.exists():
            if not force or dst.resolve() == src.resolve():
                continue
            backup = dst.with_name(f"{dst.name}.bak-{stamp}")
            if backup.exists():  # never overwrite an earlier backup
                msg = f"backup {backup.name} already exists"
                raise ConfigError(msg, hint="Run `herness init --force` again in a second.")
            dst.rename(backup)
            backed_up.append(backup.relative_to(config_dir).as_posix())
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied.append(rel.as_posix())
    return copied, backed_up


def _data_root(opts: GlobalOptions) -> Path:
    """``--data-dir``, else the loaded ``paths.data``, else ``data`` beside the config dir
    (the shipped ``paths.data``, resolved as ``load_config`` resolves it)."""
    if opts.data_dir is not None:
        return opts.data_dir.resolve()
    if (opts.config_dir / "herness.yaml").is_file():
        return opts.config(startup_validation=False).paths.data
    return opts.config_dir.resolve().parent / "data"


def run_init(opts: GlobalOptions, *, force: bool) -> dict[str, object]:
    """OI-05 order: data folders, templates, ``migrate()``; idempotent (U09-93)."""
    from herness.store.ops import migrate  # noqa: PLC0415 - lazy (BT09-07)

    root = _data_root(opts)
    created = [sub for sub in DATA_SUBDIRS if not (root / sub).is_dir()]
    for sub in DATA_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    copied, backed_up = copy_templates(opts.config_dir, force=force)
    opts.config(startup_validation=False)  # binds the ops store to `paths.data`
    report = migrate()
    return {"created_dirs": created, "copied": copied, "backed_up": backed_up,
            "migrations_applied": list(report.applied)}  # fmt: skip
