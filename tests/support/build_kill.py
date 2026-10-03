"""Child process of the build pipeline crash tests (impl 02 FT02-03, FT02-04; T02-21).

Run as ``python -m tests.support.build_kill <config_dir> <data_root> <state.json>`` from the
repository root. The child loads the config tree written by the parent (``paths.data`` =
``data_root``), binds the parent's migrated ops store and runs the full ``build_pipeline``
(``build`` … ``promote``) once with fake spec 03 / 04 hooks and a job context whose saved
state lives in ``state.json`` (the ops store ``job.result.state`` in a real worker), so a
retried job resumes from it. The fault plan comes only from the environment
(``HERNESS_ENV=test`` and ``HERNESS_FAULTS``): with a ``kill`` rule the process ends itself
at the fault point and never prints its result line. Otherwise it prints one line
``BUILD_RESULT <json>`` (``status`` and the job ``result``) and exits 0.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, JsonValue
from tests.support.build_harness import FakeJobContext

RESULT_PREFIX: Final = "BUILD_RESULT "
FULL: Final = ["build", "enrich", "score", "dq", "promote"]
_USAGE: Final = "usage: python -m tests.support.build_kill <config_dir> <data_root> <state.json>\n"


class _Report(BaseModel):
    """Stand-in for impl 03 `EnrichReport` / impl 04 `ScoringReport`."""

    name: str


def _enrichment(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="enrich")


def _scoring(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="scoring")


class FileJobContext(FakeJobContext):
    """`FakeJobContext` for the full pipeline whose job state is a JSON file."""

    def __init__(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8") if path.exists() else "{}"
        super().__init__({"stages": list(FULL)}, state=json.loads(text))
        self._path = path

    def save_state(self, state: dict[str, JsonValue]) -> None:
        super().save_state(state)
        self._path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def run(config_dir: Path, data_root: Path, state_path: Path) -> dict[str, object]:
    """Load the config, bind the ops store and run the pipeline; the outcome as a dict."""
    from herness.core.config import init_config  # noqa: PLC0415 - child process only
    from herness.model import _build_stages as stages  # noqa: PLC0415 - child process only
    from herness.model.build import run_build_pipeline  # noqa: PLC0415 - child process only
    from herness.store.ops import reset_connections  # noqa: PLC0415 - child process only

    init_config("local", config_dir=config_dir, env={"HERNESS_PATHS__DATA": str(data_root)})
    reset_connections(path=data_root / "ops.sqlite")
    stages._load_run_enrichment = lambda: _enrichment  # type: ignore[assignment,return-value]
    stages._load_run_scoring = lambda: _scoring  # type: ignore[assignment,return-value]
    outcome = run_build_pipeline(FileJobContext(state_path))
    return {"status": outcome.status, "result": outcome.result}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        sys.stderr.write(_USAGE)
        return 2
    outcome = run(Path(args[0]), Path(args[1]), Path(args[2]))
    sys.stdout.write(RESULT_PREFIX + json.dumps(outcome, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
