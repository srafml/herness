"""IT11-10: 10,000 ServiceNow rows of `api-pages` replayed into the ServiceNow connector
(U11-26; T01-16 `herness.connectors.servicenow.ServiceNowConnector`).

Spec note (T11-15): the spec replays "through respx"; respx patches only `httpx`, and the
connector's egress client is `httpx2` (T01-16, T01-25), so the pool transport is replaced
by an `httpx2.MockTransport` over the page files (`tests.unit.connectors._servicenow_env`,
as the ServiceNow flow tests do). The connector runs for real: config, OAuth, paging by
the `Link` header, `SyncRunner` and the real `LakeWriter`.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors import _servicenow_env as env
from tests.unit.connectors._http_data import bind_resilience

from herness.core import config as c
from herness.core.resilience import ProcessState
from tools.synth.api_pages import REPLAY_EPOCH, write_api_pages

pytestmark = pytest.mark.integration

ROWS = 10_000
AUDIT = "/api/now/table/sys_audit_delete"


@dataclass
class PageReplay:
    """Answers the Table API from an `api-pages` directory: the page at the request's
    `sysparm_offset` (with its manifest headers), an empty page past the end, and 403 for
    `sys_audit_delete` (deletes unreadable)."""

    out: Path
    manifest: dict[str, Any]
    served: list[str] = field(default_factory=list)
    past_end: int = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == AUDIT:
            return httpx2.Response(403, json={"error": {"message": "forbidden"}})
        pages = self.manifest["pages"]
        size = int(pages[0]["request"]["params"]["sysparm_limit"])
        offset = int(request.url.params.get("sysparm_offset", "0"))
        assert int(request.url.params["sysparm_limit"]) == size
        assert offset % size == 0, offset
        if offset // size >= len(pages):
            self.past_end += 1
            return httpx2.Response(200, json={"result": []})
        page = pages[offset // size]
        assert request.url.path == page["request"]["path"]
        assert page["request"]["params"]["sysparm_offset"] == str(offset)
        self.served.append(page["file"])
        body = (self.out / page["file"]).read_bytes()
        return httpx2.Response(200, headers=page["headers"], content=body)


def _sources_yaml() -> str:
    text = env.sources_yaml().replace("page_size: 100", "page_size: 1000")
    return text.replace("start: 2026-02-20", f"start: {REPLAY_EPOCH.date().isoformat()}")


@pytest.fixture
def replay(
    fake_keyring: MemoryKeyring,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[PageReplay, Path]]:
    del fake_keyring
    reset_process_state.sleep = lambda _s: None
    out = tmp_path / "pages"
    assert write_api_pages(11, "servicenow", "incident", ROWS, out) == 10
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    pages = PageReplay(out, manifest)
    env.store_secret()
    bind_resilience()
    env.install(monkeypatch, env.SnHost(pages))  # type: ignore[arg-type]
    yield pages, ops_store.data_root
    c.reset_config()


def test_it11_10_replay_lands_every_row_once(
    replay: tuple[PageReplay, Path], tmp_path: Path
) -> None:
    """IT11-10 the connector reads the 10 pages once each, in order (page 1 by offset, the
    rest by the `Link` header); the lake holds 10,000 rows, one per record."""
    pages, data_root = replay
    settings = env.load(tmp_path, _sources_yaml())
    clock = env.Clock(REPLAY_EPOCH + datetime.timedelta(days=1))
    result = env.runner(settings, clock, data_root).run_incremental("incident")

    files = [p["file"] for p in pages.manifest["pages"]]
    assert pages.served == files  # no duplicate or missed page
    assert pages.past_end == 1  # the last page is full: one empty page ends the listing
    rows = env.lake_rows(data_root)
    assert result.rows == ROWS
    assert len(rows) == ROWS
    assert len({r["_source_key"] for r in rows}) == ROWS
    numbers = sorted(str(r["number"]) for r in rows)
    assert numbers == [f"INC{i:07d}" for i in range(1, ROWS + 1)]
