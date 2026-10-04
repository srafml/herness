"""Synthetic lake generator: CLI and importable entry (U11-23, U11-24; design §3.1, F11-01).

`uv run python tools/synth_data.py --seed INT --scale tiny|small|full|5m [options]`, or
the `pii-corpus` (`--seed`, `--n`, `--out`) and `api-pages` (`--seed`, `--source`,
`--entity`, `--rows`, `--out`) forms (parsing and both forms: `tools.synth._cli_subcommands`).
`generate` composes `tools.synth`. Exit codes (R-46): 0 success; 2 usage error found by
argparse itself; 3 `ConfigError` (`SynthUsageError`: bad argument value, params file or
`--overwrite` without the `.synth_root` marker; `RootNotEmpty`); 1 anything else,
`SchemaViolation` from `--verify` included. stdout carries one JSON summary line; output
names keys and paths only (TH11-07).
"""

import argparse
import dataclasses
import json
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError, SchemaViolation
from herness.core.logging import configure_logging, get_logger
from herness.eval.truth import TruthManifest
from tools.synth import _cli_subcommands as cli
from tools.synth._cli_subcommands import DEFAULT_END, DEFAULT_START, usage
from tools.synth._manifest import MARKER, build_manifest, write_marker
from tools.synth.catalog import build_catalog
from tools.synth.inbox import write_service_costs
from tools.synth.params import SOURCES, load_params, params_hash
from tools.synth.pii import build_name_list
from tools.synth.shards import cause_fields, plan_shards, run_all_shards
from tools.synth.truth_writer import write_name_directory, write_synth_mappings, write_truth
from tools.synth.verify import verify_root

__all__ = ["MARKER", "RootNotEmpty", "generate", "main"]

_log = get_logger("synth")


class RootNotEmpty(ConfigError):  # noqa: N818 - name fixed by U11-23
    """The synthetic root is not empty and `--overwrite` was not given (exit 3, R-46)."""


@dataclasses.dataclass(frozen=True, slots=True)
class _Options:
    """`generate` keyword overrides and their U11-23 defaults."""

    start: date = DEFAULT_START
    end: date = DEFAULT_END
    sources: Sequence[str] = SOURCES
    dirty: str = "default"
    fetch_mode: str = "initial"
    params_file: Path | None = None
    workers: int = dataclasses.field(default_factory=lambda: os.cpu_count() or 1)
    overwrite: bool = False
    verify: bool = False


def _options(seed: int, overrides: dict[str, object]) -> _Options:
    unknown = sorted(set(overrides) - {f.name for f in dataclasses.fields(_Options)})
    if unknown:
        raise usage(unknown[0], "is not a generate() option")
    opts = _Options(**overrides)  # type: ignore[arg-type]  # each value checked below
    checks: tuple[tuple[str, object, int], ...] = (("seed", seed, 0), ("workers", opts.workers, 1))
    for key, value, low in checks:
        if type(value) is not int or value < low:
            raise usage(key, f"must be an integer of at least {low}")
    return opts


def _refuse(base: Path, error: ConfigError) -> ConfigError:
    _log.warning("synth.generate.refused", root=base.as_posix(), reason=error.message)
    return error


def _prepare_root(root: Path, *, overwrite: bool) -> Path:
    """F11-01 step 2: refuse a non-empty root unless `overwrite` and the marker of a
    generated root are present; then delete its contents (links are unlinked, never
    followed). Returns the resolved root, created if missing."""
    base = root.resolve(strict=False)
    if base.exists() and not base.is_dir():
        raise _refuse(base, usage("root", "is not a directory"))
    if base.is_dir() and any(base.iterdir()):
        if not overwrite:
            msg = "the synthetic root is not empty; --overwrite replaces a generated root"
            raise _refuse(base, RootNotEmpty(msg, key="root", path=base.as_posix()))
        marker = base / MARKER
        if marker.is_symlink() or not marker.is_file():
            raise _refuse(
                base, usage("overwrite", f"needs the {MARKER} marker of a generated root")
            )
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _verify(base: Path) -> None:
    report = verify_root(base)
    if not report.ok:
        first, n = report.problems[0], len(report.problems)
        _log.error("synth.verify.failed", root=base.as_posix(), n_problems=n, first_problem=first)
        msg = f"the generated root failed --verify ({n} problems; first: {first})"
        raise SchemaViolation(msg, root=base.as_posix(), n_problems=n)


def generate(seed: int, scale: str, root: Path, **overrides: object) -> TruthManifest:
    """Generate a complete synthetic root (design §3.1 layout plus `.synth_root`) and
    return its truth manifest. On an error after the root check the root is left as is."""
    started = clock.monotonic()
    opts = _options(seed, overrides)
    params = load_params(
        scale,
        start=opts.start,
        end=opts.end,
        sources=tuple(opts.sources),
        dirty=opts.dirty,  # type: ignore[arg-type]  # validated by load_params
        fetch_mode=opts.fetch_mode,  # type: ignore[arg-type]  # validated by load_params
        params_file=opts.params_file,
    )
    base = _prepare_root(root, overwrite=opts.overwrite)
    phash = params_hash(params)
    cat = build_catalog(seed, params)
    fields = {"seed": seed, "scale": params.scale, "params_hash": phash}
    _log.info("synth.generate.started", root=base.as_posix(), workers=opts.workers, **fields)
    plan = plan_shards(cat, params)
    agg = run_all_shards(plan, cat, params, seed=seed, root=base, workers=opts.workers)
    if "files" in params.sources:
        write_service_costs(base, cat)
    write_name_directory(base, build_name_list(seed))
    write_synth_mappings(base, cat)
    manifest = build_manifest(seed, params, cat, agg, root=base)
    write_truth(base, manifest, parts_dir=agg.parts_dir)
    write_marker(base, manifest)
    if opts.verify:
        _verify(base)
    seconds = round(clock.monotonic() - started, 3)
    _log.info("synth.generate.completed", rows_total=agg.rows_written, seconds=seconds, **fields)
    return manifest


# --- CLI ----------------------------------------------------------------------------------


def _run(ns: argparse.Namespace) -> int:
    if getattr(ns, "command", None) is not None:
        return cli.run_subcommand(ns)
    started = clock.monotonic()
    seed = cli.value(int, ns.seed, "seed")
    normal = "full" if ns.scale == "5m" else ns.scale  # default root data/synth/<seed>-<scale>
    root = Path(ns.root) if ns.root else Path("data", "synth", f"{seed}-{normal}")
    manifest = generate(seed, ns.scale, root, **cli.overrides(ns))
    summary: dict[str, object] = {"root": root.resolve(strict=False).as_posix()}
    summary |= {"rows": sum(manifest.row_counts.values())}
    summary |= {"seconds": round(clock.monotonic() - started, 3)}
    summary |= {"params_hash": manifest.params_hash}
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


def _failed(ns: argparse.Namespace | None, exc: Exception, code: int) -> int:
    """Log `synth.generate.failed` and write a one-line reason (keys and paths only)."""
    seed, scale = getattr(ns, "seed", None), getattr(ns, "scale", None)
    kind = type(exc).__name__
    fields = {"seed": seed, "scale": scale, "error_type": kind, "exit_code": code}
    _log.error("synth.generate.failed", **fields, **cause_fields(exc))
    reason = exc.message if isinstance(exc, HernessError) else "unexpected error"
    sys.stderr.write(f"synth_data: {kind}: {reason}\n")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: run the parsed form and map the outcome to an R-46 exit code."""
    configure_logging("INFO")
    ns: argparse.Namespace | None = None
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        ns = cli.parse(args)
        return _run(ns)
    except SystemExit as exc:  # argparse: usage error (2) or --help (0)
        return exc.code if isinstance(exc.code, int) else 2
    except ConfigError as exc:
        return _failed(ns, exc, 3)
    except Exception as exc:  # noqa: BLE001 - R-46: every other failure exits 1
        return _failed(ns, exc, 1)


if __name__ == "__main__":
    sys.exit(main())
