"""Argument parsing and the `pii-corpus` / `api-pages` forms of `tools/synth_data.py`
(U11-24, flows F11-04 and F11-05).

Private sibling of `tools.synth_data` (T11-15 ruling), split off for the 250-line budget
of `synth_data.py`; imported only by `tools.synth_data`. A bad option value raises
`SynthUsageError` (exit 3, R-46); an option `argparse` rejects exits 2. Each form writes
one JSON summary line to stdout naming paths and counts only (TH11-07).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Final

from herness.core import time as clock
from tools.synth.api_pages import write_api_pages
from tools.synth.params import SOURCES, SynthUsageError
from tools.synth.pii_corpus import write_pii_corpus

DEFAULT_START: Final = date(2023, 9, 1)
DEFAULT_END: Final = date(2026, 8, 31)
SUBCOMMANDS: Final = ("pii-corpus", "api-pages")


def usage(key: str, reason: str) -> SynthUsageError:
    """`SynthUsageError` naming the option `key` (exit 3)."""
    return SynthUsageError(f"{key}: {reason}", key=key)


def _default_parser() -> argparse.ArgumentParser:
    epilog = "Other forms: synth_data.py pii-corpus|api-pages --help"
    parser = argparse.ArgumentParser(prog="synth_data.py", epilog=epilog)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--scale", required=True, help="tiny, small, full or 5m (= full)")
    parser.add_argument("--root", help="default data/synth/<seed>-<scale>/")
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--sources", default=",".join(SOURCES))
    parser.add_argument("--dirty", default="default", help="none, default or heavy")
    parser.add_argument("--fetch-mode", default="initial", help="initial or daily")
    parser.add_argument("--params", help="YAML file overriding generator parameters")
    parser.add_argument("--workers", help="default os.cpu_count(); never changes content")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--verify", action="store_true")
    return parser


def _sub_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synth_data.py")
    sub = parser.add_subparsers(dest="command", required=True)
    pii = sub.add_parser("pii-corpus", help="spec 10 redaction corpus (JSONL)")
    pii.add_argument("--n", default="5000", help="records, 2000..1000000")
    pages = sub.add_parser("api-pages", help="source-shaped JSON pages and manifest.json")
    for form, names in ((pii, "seed out"), (pages, "seed source entity rows out")):
        for name in names.split():
            form.add_argument(f"--{name}", required=True)
    return parser


def parse(args: Sequence[str]) -> argparse.Namespace:
    """Parse `args` as a subcommand form when the first argument names one, else as the
    default form; `SystemExit` for a usage error found by argparse itself."""
    sub = bool(args) and args[0] in SUBCOMMANDS
    return (_sub_parser() if sub else _default_parser()).parse_args(args)


def value[T](convert: Callable[[str], T], text: str, key: str) -> T:
    """A command-line value converted, or `SynthUsageError` naming the option (exit 3)."""
    try:
        return convert(text)
    except ValueError:
        raise usage(key, "has an invalid value") from None


def overrides(ns: argparse.Namespace) -> dict[str, object]:
    """The `generate` keyword overrides of the default form."""
    out: dict[str, object] = {"start": value(date.fromisoformat, ns.start, "start")}
    out["end"] = value(date.fromisoformat, ns.end, "end")
    out["sources"] = tuple(s.strip() for s in ns.sources.split(",") if s.strip())
    out |= {"dirty": ns.dirty, "fetch_mode": ns.fetch_mode, "overwrite": ns.overwrite}
    out |= {"params_file": Path(ns.params) if ns.params else None, "verify": ns.verify}
    if ns.workers is not None:
        out["workers"] = value(int, ns.workers, "workers")
    return out


def run_subcommand(ns: argparse.Namespace) -> int:
    """F11-04 / F11-05: write the corpus or the pages, then one JSON summary line
    (`out`, `rows`, `pages` for api-pages, `seconds`)."""
    started = clock.monotonic()
    seed, out = value(int, ns.seed, "seed"), Path(ns.out)
    summary: dict[str, object] = {"out": out.resolve(strict=False).as_posix()}
    if ns.command == "pii-corpus":
        summary["rows"] = write_pii_corpus(seed, value(int, ns.n, "n"), out)
    else:
        rows = value(int, ns.rows, "rows")
        summary["pages"] = write_api_pages(seed, ns.source, ns.entity, rows, out)
        summary["rows"] = rows
    summary["seconds"] = round(clock.monotonic() - started, 3)
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


__all__ = [
    "DEFAULT_END",
    "DEFAULT_START",
    "SUBCOMMANDS",
    "overrides",
    "parse",
    "run_subcommand",
    "usage",
    "value",
]
