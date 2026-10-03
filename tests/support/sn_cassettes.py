"""Seeded ServiceNow cassettes and a replaying fake Table API (impl 01 T01-16, §13 O-16).

T11-15 (``tools.synth.api_pages.write_api_pages``) is not on the tree, so this card-local
generator writes the ServiceNow fixtures (group w25-s01a ruling): records shaped like the
Table API answers with ``sysparm_display_value=all`` (every field a ``{value,
display_value}`` pair), on the synthetic host ``BASE_URL``; every credential and token
starts with ``synthetic`` (R-67). Two cassette kinds live in
``tests/fixtures/connectors/servicenow/``:

* table cassettes (``{"kind": "table", "tables": ..., "audit": ...}``) are served by
  ``FakeServiceNow``, which evaluates the encoded queries the connector sends (the clause
  forms of U01-67 and the ``sys_audit_delete`` query of U01-68), so windows, offsets and
  key listings are answered from one table state;
* page cassettes (``{"kind": "pages", "interactions": [...]}``) are replayed strictly in
  order by ``Replay``, which checks each request's path and listed parameters.

``python -m tests.support.sn_cassettes`` rewrites the JSON files; a unit test (UT01-68)
checks the committed files equal the generator output.
"""

from __future__ import annotations

import copy
import datetime
import json
import random
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx2

BASE_URL: Final = "https://synthetic-instance.example.com"
HOST: Final = "synthetic-instance.example.com"
FOREIGN_HOST: Final = "synthetic-elsewhere.example.net"
CASSETTE_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "connectors" / "servicenow"
T0: Final = datetime.datetime(2026, 2, 20, 0, 0, 0, tzinfo=datetime.UTC)
TABLE_PATH: Final = "/api/now/table/"
_SEED: Final = 1601
_FMT: Final = "%Y-%m-%d %H:%M:%S"
_CLAUSE: Final = re.compile(r"^([a-z_]+?)(>=|<|IN|=)(.*)$")
_PRIORITIES: Final = ("1 - Critical", "2 - High", "3 - Moderate", "4 - Low", "5 - Planning")
_STATES: Final = {"1": "New", "2": "In Progress", "6": "Resolved", "7": "Closed"}

type Record = dict[str, Any]


def sn_time(value: datetime.datetime) -> str:
    """ServiceNow ``YYYY-MM-DD HH:MM:SS`` (UTC) of an aware datetime."""
    return value.astimezone(datetime.UTC).strftime(_FMT)


def pair(value: str | None, display: str | None = None) -> dict[str, str | None]:
    """A ``{value, display_value}`` field (``display`` defaults to ``value``)."""
    return {"value": value, "display_value": value if display is None else display}


def sys_id(rng: random.Random) -> str:
    """A 32-hex ServiceNow ``sys_id``: a random 6-hex head, then mostly zeros, so the
    fixtures stay below the detect-secrets hex entropy limit."""
    return f"{rng.getrandbits(24):06x}{0:020x}{rng.getrandbits(24):06x}"


def incident(rng: random.Random, number: int, updated: datetime.datetime) -> Record:
    """One incident as the Table API returns it with ``sysparm_display_value=all``."""
    key, group = sys_id(rng), sys_id(rng)
    opened = updated - datetime.timedelta(hours=rng.randint(1, 48))
    state = rng.choice(sorted(_STATES))
    priority = rng.choice(_PRIORITIES)
    return {
        "sys_id": pair(key),
        "sys_updated_on": pair(sn_time(updated)),
        "number": pair(f"INC{number:07d}"),
        "opened_at": pair(sn_time(opened)),
        "priority": pair(priority[0], priority),
        "state": pair(state, _STATES[state]),
        "assignment_group": pair(group, f"Synthetic Group {rng.randint(1, 9)}"),
        "short_description": pair(f"Synthetic incident {number}"),
    }


def incidents(
    count: int, *, start: datetime.datetime = T0, step_s: int = 900, seed: int = _SEED
) -> list[Record]:
    """``count`` incidents updated every ``step_s`` seconds from ``start`` (seeded)."""
    rng = random.Random(seed)
    step = datetime.timedelta(seconds=step_s)
    return [incident(rng, 10_000 + i, start + i * step) for i in range(count)]


def audit_delete(table: str, key: str, at: datetime.datetime) -> Record:
    """One ``sys_audit_delete`` row (plain values: queried with display values off)."""
    return {"tablename": table, "documentkey": key, "sys_created_on": sn_time(at)}


def value_of(field_value: object) -> object:
    """The raw value of a pair, else the value itself."""
    return field_value.get("value") if isinstance(field_value, dict) else field_value


# --- the fake Table API ---------------------------------------------------------------------


def _match(record: Mapping[str, Any], clause: str) -> bool:
    found = _CLAUSE.match(clause)
    if found is None:
        msg = f"unsupported clause {clause!r}"
        raise AssertionError(msg)
    name, op, operand = found.groups()
    raw = value_of(record.get(name))
    text = "" if raw is None else str(raw)
    if op == ">=":
        return text >= operand
    if op == "<":
        return text < operand
    if op == "IN":
        return text in operand.split(",")
    return text == operand


def evaluate(rows: list[Record], query: str) -> list[Record]:
    """Rows matching the encoded ``query``, sorted by its ``ORDERBY`` clauses."""
    clauses = query.split("^") if query else []
    order = [c.removeprefix("ORDERBY") for c in clauses if c.startswith("ORDERBY")]
    found = [r for r in rows if all(_match(r, c) for c in clauses if not c.startswith("ORDERBY"))]
    return sorted(found, key=lambda r: tuple(str(value_of(r.get(f))) for f in order))


def _project(record: Record, fields: str, display: str) -> Record:
    names = fields.split(",") if fields else list(record)
    out: Record = {}
    for name in names:
        if name not in record:
            continue
        item = record[name]
        out[name] = item if display == "all" or not isinstance(item, dict) else item["value"]
    return out


@dataclass
class FakeServiceNow:
    """A Table API over ``tables`` (pair-shaped rows) and ``audit`` (plain rows).

    ``audit_status`` answers every ``sys_audit_delete`` request (403: unreadable);
    ``link_next`` adds a ``Link: rel=next`` header on full pages (``next_host`` overrides its
    host: a foreign link); ``after_page(table, offset)`` runs after each page is cut, so a
    test can change the table while the connector is paging; ``fail`` maps a table path to a
    status answered instead (``{}`` body).
    """

    tables: dict[str, list[Record]] = field(default_factory=dict)
    audit: list[Record] = field(default_factory=list)
    audit_status: int = 200
    link_next: bool = False
    next_host: str = HOST
    after_page: Callable[[str, int], None] | None = None
    fail: dict[str, int] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)

    @classmethod
    def from_cassette(cls, name: str, **kwargs: Any) -> FakeServiceNow:
        """A fake over the table cassette ``<name>.json`` (deep copies: tests may mutate)."""
        data = load(name)
        assert data["kind"] == "table"
        return cls(copy.deepcopy(data["tables"]), copy.deepcopy(data["audit"]), **kwargs)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host != HOST or not path.startswith(TABLE_PATH):
            return httpx2.Response(404, json={})
        if path in self.fail:
            return httpx2.Response(self.fail[path], json={})
        table = path.removeprefix(TABLE_PATH)
        params = request.url.params
        if table == "sys_audit_delete" and self.audit_status != 200:
            return httpx2.Response(self.audit_status, json={"error": {"message": "forbidden"}})
        rows = self.audit if table == "sys_audit_delete" else self.tables.get(table, [])
        found = evaluate(rows, params.get("sysparm_query", ""))
        offset, limit = int(params.get("sysparm_offset", "0")), int(params["sysparm_limit"])
        cut = found[offset : offset + limit]
        display = params.get("sysparm_display_value", "false")
        result = [_project(r, params.get("sysparm_fields", ""), display) for r in cut]
        headers: dict[str, str] = {}
        if self.link_next and len(cut) == limit:
            nxt = request.url.copy_merge_params({"sysparm_offset": str(offset + limit)})
            nxt = nxt.copy_with(host=self.next_host)
            headers["Link"] = f'<{nxt}>;rel="next"'
        if self.after_page is not None:
            self.after_page(table, offset)
        return httpx2.Response(200, headers=headers, json={"result": result})

    def table_requests(self, table: str) -> list[httpx2.Request]:
        """The requests made to ``table``, in order."""
        return [r for r in self.requests if r.url.path == TABLE_PATH + table]


@dataclass
class Replay:
    """Answers a page cassette's interactions strictly in order, checking each request's path
    and listed query parameters; ``extra`` collects requests past the end."""

    interactions: list[dict[str, Any]]
    seen: list[httpx2.Request] = field(default_factory=list)

    @classmethod
    def from_cassette(cls, name: str) -> Replay:
        data = load(name)
        assert data["kind"] == "pages"
        return cls(copy.deepcopy(data["interactions"]))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        index = len(self.seen)
        self.seen.append(request)
        assert index < len(self.interactions), f"unexpected request {index}: {request.url.path}"
        want, answer = self.interactions[index]["request"], self.interactions[index]["response"]
        assert request.url.host == want.get("host", HOST)
        assert request.url.path == want["path"], (index, request.url.path)
        for name, value in want.get("params", {}).items():
            assert request.url.params.get(name) == value, (index, name)
        return httpx2.Response(
            answer["status"], headers=answer.get("headers", {}), json=answer["body"]
        )

    @property
    def done(self) -> bool:
        return len(self.seen) == len(self.interactions)


# --- cassette builders ------------------------------------------------------------------------


def _page(path: str, offset: int, result: object, **extra: Any) -> dict[str, Any]:
    params = {"sysparm_offset": str(offset)}
    request: dict[str, Any] = {"path": path, "params": params} | extra.pop("request", {})
    response = {"status": 200, "headers": extra.pop("headers", {}), "body": {"result": result}}
    return {"request": request, "response": response}


def _audit_403() -> dict[str, Any]:
    body = {"error": {"message": "forbidden"}}
    return {
        "request": {"path": TABLE_PATH + "sys_audit_delete", "params": {"sysparm_limit": "1"}},
        "response": {"status": 403, "headers": {}, "body": body},
    }


def _pages(name: str, sizes: list[int], *, link: bool = False) -> dict[str, Any]:
    """Audit probe 403, then incident pages of ``sizes`` records (page size 100)."""
    rows = incidents(sum(sizes), seed=_SEED + len(name))
    path, out, offset = TABLE_PATH + "incident", [_audit_403()], 0
    for number, size in enumerate(sizes):
        result = rows[offset : offset + size]
        headers: dict[str, str] = {}
        request: dict[str, Any] = {}
        if link and size == 100:
            nxt = f"{BASE_URL}{path}?sysparm_offset={offset + size}&sysparm_limit=100"
            headers["Link"] = f'<{nxt}>;rel="next"'
        if link and number > 0:
            request = {"params": {"sysparm_offset": str(offset), "sysparm_limit": "100"}}
        out.append(_page(path, offset, result, headers=headers, request=request))
        offset += size
    return {"kind": "pages", "interactions": out}


def _incident_table() -> dict[str, Any]:
    """240 incidents every 15 min over 60 h from ``T0``; 4 audited deletes interleaved."""
    rows = incidents(240)
    gone = [
        audit_delete("incident", f"{0xDE1 + i:032x}", T0 + datetime.timedelta(hours=h))
        for i, h in enumerate((1, 25, 25, 49))
    ]
    gone.append(audit_delete("problem", f"{0xDE9:032x}", T0 + datetime.timedelta(hours=2)))
    return {"kind": "table", "tables": {"incident": rows}, "audit": gone}


CASSETTES: Final[dict[str, Callable[[], dict[str, Any]]]] = {
    "incident_table": _incident_table,
    "pages_empty": lambda: _pages("pages_empty", [0]),
    "pages_one_full_then_empty": lambda: _pages("pages_one_full_then_empty", [100, 0]),
    "pages_short_last": lambda: _pages("pages_short_last", [100, 37]),
    "pages_link_next": lambda: _pages("pages_link_next", [100, 100, 12], link=True),
}


def render(name: str) -> str:
    """The JSON text of cassette ``name`` (sorted keys, LF, trailing newline)."""
    return json.dumps(CASSETTES[name](), indent=1, sort_keys=True) + "\n"


def load(name: str) -> dict[str, Any]:
    """The committed cassette ``name``."""
    data: dict[str, Any] = json.loads((CASSETTE_DIR / f"{name}.json").read_text("utf-8"))
    return data


def write_all(out: Path = CASSETTE_DIR) -> list[Path]:
    """Write every cassette under ``out``; return the paths."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in CASSETTES:
        path = out / f"{name}.json"
        path.write_text(render(name), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def records_of(name: str, table: str = "incident") -> Iterator[Record]:
    """The rows of ``table`` in table cassette ``name``."""
    yield from load(name)["tables"][table]


if __name__ == "__main__":
    for written in write_all():
        sys.stdout.write(f"{written}\n")
