"""Fixtures of the enrichment benchmarks (T03-36): the one `full` lake build all pipeline/IO
rows share, a writable connection of it, and the redaction key of the text stage."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from typing import Final

import duckdb
import pytest
from tests.bench.enrich._common import require_full
from tests.bench.enrich._lake import LakeBuild, build_lake
from tests.support.fake_keyring import MemoryKeyring

REDACT_KEY: Final = bytes(range(32))  # built at run time: no secret literal in the tree


@pytest.fixture(scope="session")
def lake(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[LakeBuild]:
    """Run the spec 02 `build` stage once on the seed 42 lake (pinned clock); every lake row
    uses this build and resets the `enrich.*` tables it writes."""
    require_full(request.config, "lake build")
    work = tmp_path_factory.mktemp("bt03")
    built, cleanup = build_lake(work)
    sys.stderr.write(
        f"BT03 lake build_s={built.build_s:.1f} incidents={built.incidents} "
        f"changes={built.changes} problems={built.problems} services={built.services} "
        f"teams={built.teams} scale={built.scale}\n"
    )
    try:
        yield built
    finally:
        cleanup()
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture
def wh(lake: LakeBuild) -> Iterator[duckdb.DuckDBPyConnection]:
    """A writable auto-commit connection of the lake build; closed afterwards."""
    con = lake.open()
    try:
        yield con
    finally:
        con.close()


@pytest.fixture
def redaction_key(fake_keyring: MemoryKeyring) -> None:
    """The (fake) keyring holds the redaction HMAC key: the real `redact_table` works."""
    fake_keyring.store[("herness", "redact.hmac_key")] = REDACT_KEY.hex()
