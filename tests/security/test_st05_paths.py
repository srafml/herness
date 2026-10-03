"""Security test ST05-17 (TH05-17), `run_id` half: a run id with path separators never becomes
a file path (T05-27).

The `build_id` and symlink halves are in tests/security/test_st05_warehouse.py (T05-13). In spec
05 the run id reaches the filesystem only as the trace file name (`<traces_dir>/<run_id>.jsonl`);
the `Tracer` refuses any run id that is not `run_<ULID>` with `ConfigError` before it creates a
directory or opens a file, so nothing appears inside or outside the traces directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herness.core.errors import ConfigError
from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer

pytestmark = pytest.mark.unit

VALID = "run_01J8ST05170000000000000000"


@pytest.mark.parametrize(
    "run_id",
    [
        "../../x",
        "run_../../escape",
        "run_01J8ST0517000000000000/000",
        "run_01J8ST0517000000000000\\000",
        "/etc/passwd",
        "C:/Windows/win",
        f"{VALID}/../../x",
        f"..\\{VALID}",
        f"{VALID}\x00",
    ],
    ids=[
        "dotdot",
        "prefixed_dotdot",
        "slash",
        "backslash",
        "absolute",
        "drive",
        "suffix",
        "prefix_backslash",
        "nul",
    ],
)
def test_st05_17_run_id_with_separators_raises_config_error(tmp_path: Path, run_id: str) -> None:
    """ST05-17 a `run_id` with separators or absolute-path shapes: `ConfigError` from the
    Tracer, and no file or directory is created anywhere under the test root."""
    traces = tmp_path / "data" / "traces"
    with pytest.raises(ConfigError, match=r"^invalid trace run_id$"):
        Tracer(run_id, build_id=None, run_kind="eval", traces_dir=traces, settings=TraceSettings())
    assert list(tmp_path.rglob("*")) == []


def test_st05_17_valid_run_id_writes_only_inside_traces_dir(tmp_path: Path) -> None:
    """ST05-17 control: a `run_<ULID>` id writes exactly `<traces_dir>/<run_id>.jsonl`."""
    traces = tmp_path / "data" / "traces"
    tracer = Tracer(
        VALID, build_id=None, run_kind="eval", traces_dir=traces, settings=TraceSettings()
    )
    tracer.emit("budget", kind="warn", used={}, limit={}, message=None)
    tracer.close()
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files == [traces / f"{VALID}.jsonl"]
