"""Raw ticket text exposure of the built warehouse (impl 02 ST02-13 TH02-13; T02-22).

The `lake_small` build is inspected through the catalog: no `stg` table keeps `_payload` (the
raw JSON of the record), and every `core` VARCHAR column that holds raw ticket text (the
column names design 02 §7.6 classifies as personal: `short_description`, `description`,
`close_notes`, `root_cause_text`, `summary`) is in the default
`config/models.yaml harness.sql.blocked_columns`, which the spec 05 SQL guard denies to agents.

`core.work_item.summary` is raw ticket text per §7.6 but is not blocked: spec 05 D9 redacts it on
read (`REDACT_ON_READ_COLUMNS`), so the second test counts that list as covered (T05-04b).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle

from herness.core.resilience import ProcessState
from herness.harness.llm.settings import SqlSettings
from herness.harness.sql_guard import REDACT_ON_READ_COLUMNS
from herness.model.build import run_build_pipeline
from herness.store import warehouse
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
# design 02 §7.6 "personal" ticket text columns of `core.*`
RAW_TEXT_NAMES: Final = frozenset(
    {"short_description", "description", "close_notes", "root_cause_text", "summary"}
)
_COLUMNS = (
    "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
    " WHERE table_schema IN ('stg', 'core') ORDER BY 1, 2, ordinal_position"
)


@pytest.fixture
def columns(
    tmp_path: Path, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> list[tuple[str, str, str, str]]:
    """Every `stg` / `core` column of a built `lake_small` warehouse."""
    del reset_process_state
    install(ops_store.data_root)
    load_config(tmp_path / "cfgroot", ops_store.data_root)
    outcome = run_build_pipeline(FakeJobContext({"stages": ["build"]}))
    assert outcome.status == "done"
    layout = DataLayout.from_root(ops_store.data_root)
    with warehouse.open_readonly(str(outcome.result["build_id"]), layout=layout) as con:
        rows = con.execute(_COLUMNS).fetchall()
    return [(str(a), str(b), str(c), str(d)) for a, b, c, d in rows]


def _published_default() -> set[str]:
    text = (REPO / "config" / "models.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    published = loaded["harness"]["sql"]["blocked_columns"]
    assert isinstance(published, list)
    return {str(c) for c in published}


def _raw_text(columns: list[tuple[str, str, str, str]]) -> set[str]:
    return {
        f"core.{t}.{c}"
        for s, t, c, dtype in columns
        if s == "core" and dtype == "VARCHAR" and c in RAW_TEXT_NAMES
    }


def test_st02_13_no_stg_payload_and_blocked_list_matches_catalog(
    columns: list[tuple[str, str, str, str]],
) -> None:
    """ST02-13 TH02-13 no `stg` table has `_payload`; the published `config/models.yaml`
    blocked list equals the settings model default and names only raw text `core` VARCHAR
    columns that exist in the built warehouse."""
    stg = [c for c in columns if c[0] == "stg"]
    assert {t for _, t, _, _ in stg}, "the build created no stg tables"
    assert [f"stg.{t}.{c}" for _, t, c, _ in stg if c == "_payload"] == []
    published = _published_default()
    assert set(SqlSettings().blocked_columns) == published
    assert sorted(published - _raw_text(columns)) == []


def test_st02_13_every_raw_text_core_column_is_blocked(
    columns: list[tuple[str, str, str, str]],
) -> None:
    """ST02-13 TH02-13 every `core` VARCHAR column that holds raw text per design 02 §7.6 is
    in the default `harness.sql.blocked_columns` or in the guard's `REDACT_ON_READ_COLUMNS`
    (spec 05 D9: `core.work_item.summary` is redacted on read)."""
    raw_text = _raw_text(columns)
    assert raw_text, "no raw text column found in core"
    assert sorted(raw_text - (_published_default() | set(REDACT_ON_READ_COLUMNS))) == []
