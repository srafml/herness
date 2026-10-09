"""Truth fixtures (U11-47, design 11 §4.4): the only place tests load planted truth.

`load_truth_labels` reads `truth_labels.parquet` of a truth directory; the session
fixtures `truth_7_tiny` and `truth_42_tiny` parse the committed
`tests/fixtures/truth/<seed>-tiny/truth.json` into a `TruthManifest`. The directories are
the `truth/` output of `tools/synth_data.py --seed <seed> --scale tiny` (UT11-30 pins
`lake_small`, the raw output of seed 7). `dataset_root` is committed as the neutral `data`
(the generator writes the absolute path of the machine that produced the fixture).
Registered as a pytest plugin by `tests/conftest.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.eval.truth import TruthManifest, load_truth

__all__ = [
    "FIXTURES",
    "LAKE_SMALL",
    "TRUTH_DIR",
    "load_truth_labels",
    "truth_7_tiny",
    "truth_42_tiny",
]

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
LAKE_SMALL: Final = FIXTURES / "lake_small"
TRUTH_DIR: Final = FIXTURES / "truth"
_LABELS: Final = "truth_labels.parquet"


def load_truth_labels(truth_dir: Path) -> pa.Table:
    """`<truth_dir>/truth_labels.parquet` (record_id, content_hash, question, answer, pii_spans)."""
    return pq.read_table(truth_dir / _LABELS)


@pytest.fixture(scope="session")
def truth_7_tiny() -> TruthManifest:
    """Truth manifest of the committed seed 7 tiny run."""
    return load_truth(TRUTH_DIR / "7-tiny")


@pytest.fixture(scope="session")
def truth_42_tiny() -> TruthManifest:
    """Truth manifest of the committed seed 42 tiny run."""
    return load_truth(TRUTH_DIR / "42-tiny")
