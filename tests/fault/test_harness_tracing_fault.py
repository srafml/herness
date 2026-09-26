"""Fault test for the trace write pipeline (FT05-01, flow F05-10)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fault

REPO = Path(__file__).resolve().parents[2]
RUN_ID = "run_01J8FT05010000000000000000"
WRITER = """
import sys
from pathlib import Path
from herness.harness.llm.settings import TraceSettings
from herness.harness.tracing import Tracer
tracer = Tracer(
    sys.argv[1], build_id=None, run_kind="chat", traces_dir=Path(sys.argv[2]),
    settings=TraceSettings(), flush_interval_s=0.01,
)
i = 0
while True:
    tracer.emit("tool_call", step=i, tool="run_sql", note="x" * 3000)
    i += 1
"""


def test_ft05_01_killed_writer_leaves_only_the_last_line_truncated(tmp_path: Path) -> None:
    """FT05-01 subprocess writing traces killed mid-run: every complete line is valid JSON;
    at most the last line is truncated."""
    path = tmp_path / f"{RUN_ID}.jsonl"
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", WRITER, RUN_ID, str(tmp_path)], cwd=REPO
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and (
            not path.exists() or path.stat().st_size < 1_000_000
        ):
            time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait(timeout=30)
    data = path.read_bytes().decode("utf-8", errors="replace")
    lines = data.split("\n")
    complete, tail = lines[:-1], lines[-1]
    assert len(complete) > 100
    events = [json.loads(line) for line in complete]  # no garbage in the middle
    steps = [e["step"] for e in events if e["type"] == "tool_call"]
    assert steps == sorted(steps)
    assert all(e["run_id"] == RUN_ID for e in events)
    if tail:  # a truncated last line is allowed, and only that one
        with pytest.raises(json.JSONDecodeError):
            json.loads(tail)
