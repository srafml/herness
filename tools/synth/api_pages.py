"""Source-shaped JSON pages for connector replay tests and benchmarks (U11-26).

`write_api_pages` writes `out/page-<nnnnn>.json` bodies and, last, `out/manifest.json` =
`{"source", "entity", "rows", "pages": [{"file", "rows", "headers", "request"}]}`.
ServiceNow pages are Table API answers with `sysparm_display_value=all` (`{"result":
[records]}`, every field a `{value, display_value}` pair) and carry
`Link: <...&sysparm_offset=N>;rel="next"` on all but the last page; Jira pages are Cloud
search answers (`POST /rest/api/3/search/jql`, spec 01 §5.8): `{"issues": [...],
"nextPageToken": "synthetic-page-<n>"` (absent on the last page), `"isLast": bool}`, the
token carrying the `synthetic` prefix (R-67). Hosts are the synthetic hosts of the
connector tests (`tests.support.sn_cassettes`, `tests.support.jira_pages`).

Records come from the U11-07/U11-08 generators over a `tiny`-class catalog whose seed is
drawn from `stream_rng(seed, STREAM_API_PAGES)`, page by page (at most one page, <= 2,000
records, in memory), with no PII (rate 0). Spec note (T11-15): the pages answer one
time-ordered listing (`ORDERBYsys_updated_on`, `ORDER BY updated ASC`), so each record's
`sys_updated_on` (Jira `updated`) is rewritten to `REPLAY_EPOCH` plus its row index in
seconds, after every other timestamp of the span; a connector replay then reads the whole
listing in one window. Jira search pages carry no `changelog` or `remotelinks` (Cloud
search returns neither; the connector fetches them separately).
"""

import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np

from tools.synth import jira, servicenow
from tools.synth.catalog import Catalog, build_catalog
from tools.synth.catalog_plan import span_months
from tools.synth.jira_changelog import jira_ts
from tools.synth.params import SOURCES, SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.rng import STREAM_API_PAGES, stream_rng
from tools.synth.servicenow_common import ts_pair
from tools.synth.shards import Shard
from tools.synth.text import TemplateBank

type Record = dict[str, Any]
type _Gen = Callable[[Catalog, SynthParams, Shard, np.random.Generator], list[Record]]

ROWS_MAX: Final = 5_000_000
PAGE_SIZE_MAX: Final = 2_000
JIRA_PAGE_SIZE_MAX: Final = 100
SN_BASE_URL: Final = "https://synthetic-instance.example.com"
JIRA_BASE_URL: Final = "https://jira.example.test"
JIRA_SEARCH_PATH: Final = "/rest/api/3/search/jql"
REPLAY_EPOCH: Final = datetime(2026, 9, 8, tzinfo=UTC)  # a week after the tiny span ends
MANIFEST: Final = "manifest.json"
SN_ENTITIES: Final = ("incident", "change_request", "problem")

_SPAN_END: Final = date(2026, 8, 31)  # the generator's default `--end` (U11-23)
_CATALOG_SEED_BOUND: Final = 2**31


def _usage(key: str, reason: str) -> SynthUsageError:
    return SynthUsageError(f"{key}: {reason}", key=key)


def _check(source: str, entity: str, rows: int, page_size: int) -> int:
    """Validate the arguments; return the effective page size (Jira capped at 100)."""
    if source not in ("servicenow", "jira"):
        error = _usage("source", "must be servicenow or jira")
        raise error
    if entity not in (SN_ENTITIES if source == "servicenow" else ("issue",)):
        error = _usage("entity", f"is not an api-pages entity of {source}")
        raise error
    for key, value, high in (("rows", rows, ROWS_MAX), ("page_size", page_size, PAGE_SIZE_MAX)):
        if type(value) is not int or not 1 <= value <= high:
            raise _usage(key, f"must be an integer in 1..{high}")
    return min(page_size, JIRA_PAGE_SIZE_MAX) if source == "jira" else page_size


def _params() -> SynthParams:
    """`tiny` parameters with the PII rates set to 0 (no PII in api pages)."""
    params = load_params(
        "tiny",
        start=_SPAN_END,  # ignored for tiny: start = end - 89 days
        end=_SPAN_END,
        sources=SOURCES,
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )
    pii = params.pii.model_copy(update={"incident_share": 0.0, "jira_share": 0.0})
    return params.model_copy(update={"pii": pii})


def _generator(seed: int, source: str, entity: str) -> _Gen:
    bank, names = TemplateBank(), build_name_list(seed)  # unused: PII rate 0
    if source == "jira":

        def issues(
            cat: Catalog, p: SynthParams, s: Shard, rng: np.random.Generator
        ) -> list[Record]:
            drop = ("changelog", "remotelinks")
            batch = jira.gen_issues(cat, p, s, rng, bank, names)
            return [{k: v for k, v in r.items() if k not in drop} for r in batch.records]

        return issues
    if entity == "incident":
        return lambda cat, p, s, rng: servicenow.gen_incidents(cat, p, s, rng, bank, names).records
    gen = servicenow.gen_changes if entity == "change_request" else servicenow.gen_problems
    return lambda cat, p, s, rng: gen(cat, p, s, rng, bank)


def _retime(source: str, records: Sequence[Record], first_row: int) -> None:
    """Rewrite the update stamps to `REPLAY_EPOCH + row` seconds (ascending listing)."""
    for i, record in enumerate(records):
        at = REPLAY_EPOCH + timedelta(seconds=first_row + i)
        if source == "jira":
            record["fields"]["updated"] = jira_ts(at)
        else:
            record["sys_updated_on"] = ts_pair(at)


def _page_meta(
    source: str, entity: str, number: int, offset: int, size: int, *, last: bool
) -> tuple[dict[str, str], dict[str, Any]]:
    """The response headers and the request of page `number` (1-based)."""
    if source == "jira":
        body: dict[str, Any] = {"maxResults": size}
        if number > 1:
            body["nextPageToken"] = f"synthetic-page-{number}"
        return {}, {"method": "POST", "path": JIRA_SEARCH_PATH, "json": body}
    path = f"/api/now/table/{entity}"
    params = {"sysparm_limit": str(size), "sysparm_offset": str(offset)}
    request = {"method": "GET", "path": path, "params": params}
    if last:
        return {}, request
    nxt = f"{SN_BASE_URL}{path}?sysparm_limit={size}&sysparm_offset={offset + size}"
    return {"Link": f'<{nxt}>;rel="next"'}, request


def _body(source: str, records: list[Record], number: int, *, last: bool) -> dict[str, Any]:
    if source == "servicenow":
        return {"result": records}
    body: dict[str, Any] = {"issues": records}
    if not last:
        body["nextPageToken"] = f"synthetic-page-{number + 1}"
    return body | {"isLast": last}


def _write_json(path: Path, data: object) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def _prepare(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        error = _usage("out", "must be a new or empty directory")
        raise error
    out.mkdir(parents=True, exist_ok=True)


def write_api_pages(
    seed: int,
    source: Literal["servicenow", "jira"],
    entity: str,
    rows: int,
    out: Path,
    *,
    page_size: int = 1000,
) -> int:
    """Write `rows` records of `source`/`entity` as pages under `out` (a new or empty
    directory), `manifest.json` last; return the number of pages written."""
    size = _check(source, entity, rows, page_size)
    rng = stream_rng(seed, STREAM_API_PAGES)  # SynthUsageError for a negative seed
    params = _params()
    cat = build_catalog(int(rng.integers(_CATALOG_SEED_BOUND)), params)
    months = [month for month, _ in span_months(params)]
    generate = _generator(seed, source, entity)
    _prepare(out)
    pages: list[dict[str, Any]] = []
    for offset in range(0, rows, size):
        number, count = len(pages) + 1, min(size, rows - offset)
        last = offset + count >= rows
        shard = Shard(source, entity, months[(number - 1) % len(months)], count, offset + 1, number)
        records = generate(cat, params, shard, rng)
        _retime(source, records, offset)
        name = f"page-{number:05d}.json"
        _write_json(out / name, _body(source, records, number, last=last))
        headers, request = _page_meta(source, entity, number, offset, size, last=last)
        pages.append({"file": name, "rows": count, "headers": headers, "request": request})
    manifest = {"source": source, "entity": entity, "rows": rows, "pages": pages}
    tmp = out / (MANIFEST + ".tmp")
    _write_json(tmp, manifest)
    os.replace(tmp, out / MANIFEST)
    return len(pages)


__all__ = [
    "JIRA_BASE_URL",
    "JIRA_PAGE_SIZE_MAX",
    "JIRA_SEARCH_PATH",
    "MANIFEST",
    "PAGE_SIZE_MAX",
    "REPLAY_EPOCH",
    "ROWS_MAX",
    "SN_BASE_URL",
    "SN_ENTITIES",
    "write_api_pages",
]
