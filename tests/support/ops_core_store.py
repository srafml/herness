"""Interim `ops_store` fixture (impl 02 §3.5, spec 11 §11 fixtures) added by T02-04.

T11-40 replaces this plugin with `tests.support.ops_store` (U11-78), which also migrates the
store; until then the fixture only points the ops core at a fresh `ops.sqlite` under
`tmp_path` and closes every connection afterwards.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from herness.store.ops import reset_connections


@pytest.fixture
def ops_store(tmp_path: Path) -> Iterator[Path]:
    """Yield the path of a fresh, empty `ops.sqlite`; reset the ops connections around it."""
    db_path = tmp_path / "ops.sqlite"
    reset_connections(path=db_path)
    yield db_path
    reset_connections()
