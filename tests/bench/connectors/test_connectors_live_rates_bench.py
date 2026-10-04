"""Live source rates against vendor sandboxes (impl 01 BT01-06; design 01 §8; T01-25).

Manual, Phase 6 gate. The vendor sandboxes (V-1, V-2 of impl 01 §13; open question (b)
items 22 and 23) do not exist yet, so the test is skipped unless
``HERNESS_BT01_06_SANDBOX`` names a config directory whose ``sources.yaml`` enables the
sandbox sources (credentials in the keyring as for production). With it set, the first entity
of each enabled source among ServiceNow, Jira, MongoDB, Snowflake, Dataverse and monitoring
is synced once from its backfill start with the production connector and runner; the committed
rows per second must reach the lower bound of the design range. The measured rates and bounds
are recorded in ``<paths.data>/bench/bt01_06-<UTC time>.json``.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Final

import pytest

from herness.connectors.factory import build_connector
from herness.connectors.runner import SyncRunner
from herness.core import config as c
from herness.core.logging import get_logger

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SANDBOX_ENV: Final = "HERNESS_BT01_06_SANDBOX"
# lower bound of each design range (design 01 §8), rows per second
LOWER_BOUNDS: Final = {
    "servicenow": 300,
    "jira": 150,
    "mongodb": 20_000,
    "snowflake": 100_000,
    "dataverse": 1_500,
    "monitoring": 500,
}
_log = get_logger("bench.connectors")


@pytest.mark.skipif(
    not os.environ.get(SANDBOX_ENV),
    reason=(
        f"{SANDBOX_ENV} not set: BT01-06 needs the vendor sandboxes (V-1, V-2 of impl 01 §13; "
        "open questions (b) 22/23), a Phase 6 gate"
    ),
)
def test_bt01_06_live_source_rates_reach_the_design_lower_bounds() -> None:
    """BT01-06 each live source syncs at or above the lower bound of its design range
    (ServiceNow 300 rows/s, Jira 150 issues/s, MongoDB 20k docs/s, Snowflake 100k rows/s,
    Dataverse 1.5k rows/s, events 500/s) against its vendor sandbox; results go to
    `data/bench/`."""
    cfg = c.init_config("local", config_dir=Path(os.environ[SANDBOX_ENV]), env=dict(os.environ))
    rates: dict[str, float] = {}
    for name, _settings in cfg.sources.enabled_sources():
        if name not in LOWER_BOUNDS:
            continue
        conn = build_connector(name, cfg)
        runner = SyncRunner(conn, cfg.sources.source(name), data_root=cfg.paths.data)
        entity = conn.entities[0]
        start = time.perf_counter()
        rows = runner.run_incremental(entity).rows
        seconds = max(time.perf_counter() - start, 1e-9)
        rates[name] = rows / seconds
        _log.info("bench.connectors.live_rate", source=name, rows=rows, rows_per_s=rates[name])
    now = datetime.datetime.now(datetime.UTC)
    record = {
        "id": "BT01-06",
        "at": now.isoformat(),
        "rows_per_s": rates,
        "lower_bounds": {name: LOWER_BOUNDS[name] for name in rates},
    }
    out = cfg.paths.data / "bench"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    (out / f"bt01_06-{stamp}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert rates, "no sandbox source of BT01-06 is enabled in the sandbox config"
    slow = {n: r for n, r in rates.items() if r < LOWER_BOUNDS[n]}
    assert not slow, f"below the design lower bound (rows/s): {slow}"
