"""ST06-18 (TH06-18): a crafted run id cannot place `draft.json` outside the reports root."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.report_drafts import make_draft

from herness.core.errors import ConfigError
from herness.harness.swarm import writer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "run_id", ["run_../../x", "run_..\\..\\x", "run_" + "A" * 25 + "/", "/etc/passwd", "run_%2e%2e"]
)
def test_st06_18_crafted_run_id_rejected_nothing_written(tmp_path: Path, run_id: str) -> None:
    """ST06-18 run id `run_../../x` (and variants) → ConfigError; nothing is written anywhere
    under the test directory, inside or outside the reports root."""
    root = tmp_path / "reports"
    root.mkdir()

    def write() -> None:
        writer.write_draft_atomic(writer.draft_path(run_id, root), make_draft())

    with pytest.raises(ConfigError):
        write()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
