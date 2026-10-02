"""ST01-05 at the HTTP/runner level (impl 01 TH01-05; T01-14).

A test-local minimal connector reads pages through ``SourceHttp`` and checks their shape the
way the source connectors do; an unexpected shape or a record without its timestamp raises
``SchemaViolation`` inside the runner: the watermark stays where it was and nothing is
committed. The ServiceNow-specific rows (``result`` as an object, a record without
``sys_updated_on`` through ``servicenow.py``) carry over to T01-16.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass, field

import pyarrow as pa
import pytest
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._http_data import bind_resilience, mock_client, reply
from tests.unit.connectors._runner_data import NOW, T, batch, make_runner, servicenow_cfg

from herness.connectors.http import SourceHttp
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.resilience import ProcessState
from herness.store.ops import get_watermark, set_watermark

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"


@dataclass
class PagedConnector:
    """Minimal ServiceNow-shaped connector over SourceHttp: one page of `result` records."""

    http: SourceHttp
    name: str = _SRC
    entities: tuple[str, ...] = (_ENT,)
    seen: list[str] = field(default_factory=list)

    def check(self) -> None:
        return None

    def watermark_field(self, entity: str) -> str:
        return "sys_updated_on"

    def sync(
        self, entity: str, since: datetime.datetime | None, until: datetime.datetime | None = None
    ) -> Iterator[pa.RecordBatch]:
        page = self.http.get_json(f"/api/now/table/{entity}")
        body = page.body
        if not isinstance(body, dict) or not isinstance(body.get("result"), list):
            msg = "unexpected response shape: result is not a list"
            raise SchemaViolation(msg, entity=entity)
        for record in body["result"]:
            if not isinstance(record, dict) or "sys_updated_on" not in record:
                msg = "record without sys_updated_on"
                raise SchemaViolation(msg, entity=entity)
            ts = clock.parse_iso(str(record["sys_updated_on"]))
            self.seen.append(str(record["sys_id"]))
            yield batch(_SRC, entity, [str(record["sys_id"])], ts)


@pytest.mark.parametrize(
    "body",
    [
        {"result": {"sys_id": "a"}},  # result as an object
        {"result": [{"sys_id": "a", "sys_updated_on": "2026-03-01T10:30:00Z"}, {"sys_id": "b"}]},
    ],
)
def test_st01_05_unexpected_shape_keeps_watermark_and_commits_nothing(
    body: dict[str, object],
    ops_store: OpsStoreHandle,
    lake: FakeLake,
    reset_process_state: ProcessState,
) -> None:
    """ST01-05 `result` as an object, or a record without `sys_updated_on` after a good one:
    SchemaViolation; the watermark is unchanged and no lake file is committed."""
    del reset_process_state
    bind_resilience()
    set_watermark(_SRC, _ENT, "sys_updated_on", T, now=NOW)
    http = SourceHttp(mock_client(lambda _r: reply(body=body)), breaker_key=_SRC, auth=None)
    runner = make_runner(PagedConnector(http), servicenow_cfg(), lake, ops_store.data_root)

    with pytest.raises(SchemaViolation):
        runner.run_incremental(_ENT)

    wm = get_watermark(_SRC, _ENT)
    assert wm is not None
    assert wm.value == T
    assert "commit" not in lake.kinds()
