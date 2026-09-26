"""Acceptance check of T10-05: two processes appending 1,000 audit lines each (U10-61)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config

from herness.core import audit as a
from herness.core import config as c

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
WRITER = """
import sys
from pathlib import Path
from herness.core.audit import audit
from herness.core.config import init_config
init_config(config_dir=Path(sys.argv[1]), env={})
for n in range(1000):
    audit("egress", "system", egress_id=f"egr_{sys.argv[2]}_{n}", reason="allowed")
"""


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()


def test_ut10_57_two_processes_keep_one_valid_chain(tmp_path: Path) -> None:
    """UT10-57 (acceptance) two writer processes x 1,000 lines give one valid 2,000-line chain."""
    cfg_dir = write_full_config(tmp_path)
    logs = c.load_config(config_dir=cfg_dir, env={}).paths.logs
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", WRITER, str(cfg_dir), str(n)], cwd=REPO
        )
        for n in range(2)
    ]
    assert [p.wait(timeout=240) for p in procs] == [0, 0]
    report = a.verify_chain(logs)
    assert report.ok, report.first_break
    assert report.lines == 2000
