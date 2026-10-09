"""Shared fixture data for the redaction rekey tests (impl 10 T10-30: UT10-70, IT10-07, FT10-02).

Not a test module. ``RekeyEnv`` plants a minimal ``CURRENT`` warehouse (``core.incident``,
``core.change``, ``core.problem`` and ``enrich.text_redacted`` hashed under the old key) and
label parts under ``data/labels/<qsv>/{human,gold,gold/_reviews,teacher}/`` in ``tmp_path``.
The keyring is the in-memory ``fake_keyring``; ``FakeCtx`` is a structural ``JobContext`` that
keeps its saved state in memory, round-tripped through JSON like the ops store does.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import JsonValue
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core.redact import Redactor
from herness.enrich.labels import LABEL_SCHEMAS
from herness.enrich.text import compose_text, content_hash

SVC = "herness"
OLD_KEY = "0a" * 32  # test key material, never a real secret
NEW_KEY = "0b" * 32
OLD_ID = hashlib.sha256(bytes.fromhex(OLD_KEY)).hexdigest()[:8]
NEW_ID = hashlib.sha256(bytes.fromhex(NEW_KEY)).hexdigest()[:8]
BUILD_ID = "20260920-120000-AAAAAA"
QSV = "qs-2026-09-01"
SHARED_OLD = "f" * 32  # one old hash held by two records whose new hashes differ (ambiguous)
GONE_OLD = "e" * 32  # a label whose record no longer exists (kept_old)

# (entity, record_id, short_description, body); every text holds an e-mail address, so its
# pseudonym (and its content_hash) changes with the key.
RECORDS: tuple[tuple[str, str, str | None, str], ...] = (
    ("incident", "sn:incident:INC1", "Printer jam", "Reported by alice@example.com"),
    ("incident", "sn:incident:INC2", "VPN down", "Caller bob@example.com cannot connect"),
    ("incident", "sn:incident:INC3", "VPN down", "Caller carol@example.com cannot connect"),
    ("change", "sn:change:CHG1", "Patch DB", "Owner dave@example.com"),
    ("problem", "sn:problem:PRB1", None, "Root cause found by erin@example.com"),
)
AMBIGUOUS_IDS = ("sn:incident:INC2", "sn:incident:INC3")


@dataclass
class FakeCtx:
    """A structural ``JobContext``: in-memory saved state and recorded heartbeats."""

    state: dict[str, JsonValue] = field(default_factory=dict)
    saves: list[dict[str, JsonValue]] = field(default_factory=list)
    beats: int = 0
    job: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(job_id="job_rekey", payload={"action": "rekey"})
    )

    def heartbeat(self, note: str | None = None) -> None:
        """Count the beat."""
        del note
        self.beats += 1

    def save_state(self, state: dict[str, JsonValue]) -> None:
        """Keep a JSON round-tripped copy for the next attempt."""
        self.saves.append(state)
        self.state = json.loads(json.dumps(state))

    def load_state(self) -> dict[str, JsonValue]:
        """The last saved state."""
        return dict(self.state)


def redactor(cfg: c.HernessConfig, key_hex: str) -> Redactor:
    """A redactor over ``key_hex`` with the config's rules and no name directory."""
    return Redactor(cfg.security.redaction, bytes.fromhex(key_hex))


def hash_under(cfg: c.HernessConfig, key_hex: str, short: str | None, body: str) -> str:
    """``content_hash`` of the composed text redacted under ``key_hex``."""
    result = redactor(cfg, key_hex).redact(compose_text(short, body))
    assert result is not None
    return content_hash(result.text)


@dataclass
class RekeyEnv:
    """The planted config, keyring, warehouse and labels of one test."""

    cfg: c.HernessConfig
    keyring: MemoryKeyring
    old: dict[str, str]  # record_id -> old content_hash in the warehouse
    new: dict[str, str]  # record_id -> expected new content_hash

    @property
    def data(self) -> Path:
        """The data root."""
        return Path(self.cfg.paths.data)

    @property
    def labels(self) -> Path:
        """``data/labels``."""
        return self.data / "labels"

    @property
    def map_path(self) -> Path:
        """The rekey map of the new key."""
        return self.data / "cache" / "rekey" / f"{NEW_ID}.parquet"

    @property
    def marker(self) -> Path:
        """The ``SWAPPED`` marker."""
        return self.data / "cache" / "rekey" / "SWAPPED"

    def key(self, name: str) -> str | None:
        """The keyring value of ``name``."""
        return self.keyring.store.get((SVC, name))

    def label_rows(self, kind: str) -> list[dict[str, object]]:
        """All rows of the live ``part-*.parquet`` files of ``kind``."""
        rows: list[dict[str, object]] = []
        for part in sorted((self.labels / QSV / kind).glob("part-*.parquet")):
            rows += pq.read_table(part, partitioning=None).to_pylist()
        return rows

    def label_bytes(self) -> dict[str, bytes]:
        """Every file under ``data/labels`` by relative path (unchanged-ness checks)."""
        return {
            p.relative_to(self.labels).as_posix(): p.read_bytes()
            for p in sorted(self.labels.rglob("*"))
            if p.is_file()
        }

    def audit_text(self) -> str:
        """All audit lines as text."""
        return "".join(p.read_text("utf-8") for p in Path(self.cfg.paths.logs).glob("audit-*"))

    def audit_lines(self) -> list[dict[str, object]]:
        """All audit lines, parsed."""
        return [json.loads(line) for line in self.audit_text().splitlines()]


def _plant_warehouse(env: RekeyEnv) -> None:
    folder = env.data / "warehouse"
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(folder / f"wh-{BUILD_ID}.duckdb"))
    try:
        con.execute("CREATE SCHEMA core; CREATE SCHEMA enrich")
        for table in ("incident", "change"):
            con.execute(
                f"CREATE TABLE core.{table} (record_id VARCHAR, short_description VARCHAR,"
                " description VARCHAR)"
            )
        con.execute("CREATE TABLE core.problem (record_id VARCHAR, root_cause_text VARCHAR)")
        con.execute(
            "CREATE TABLE enrich.text_redacted (record_id VARCHAR, entity VARCHAR,"
            " text VARCHAR, content_hash VARCHAR)"
        )
        for entity, rid, short, body in RECORDS:
            if entity == "problem":
                con.execute("INSERT INTO core.problem VALUES (?, ?)", [rid, body])
            else:
                sql = f"INSERT INTO core.{entity} VALUES (?, ?, ?)"  # noqa: S608 - fixed names
                con.execute(sql, [rid, short, body])
            con.execute(
                "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
                [rid, entity, "redacted text", env.old[rid]],
            )
    finally:
        con.close()
    (folder / "CURRENT").write_text(f"{BUILD_ID}\n", encoding="ascii")


def label_table(kind: str, pairs: list[tuple[str, str]]) -> pa.Table:
    """Label rows of ``kind`` for ``(record_id, content_hash)`` pairs (other columns filled)."""
    schema = LABEL_SCHEMAS[kind]
    n = len(pairs)
    columns: dict[str, list[object]] = {
        "record_id": [rid for rid, _ in pairs],
        "content_hash": [h for _, h in pairs],
    }
    for fld in schema:
        if fld.name in columns:
            continue
        if pa.types.is_string(fld.type):
            columns[fld.name] = [f"{fld.name}-{i}" for i in range(n)]
        elif pa.types.is_integer(fld.type):
            columns[fld.name] = [1] * n
        elif pa.types.is_boolean(fld.type):
            columns[fld.name] = [True] * n
        elif pa.types.is_map(fld.type):
            columns[fld.name] = [[("true", 1.0)]] * n
        else:
            columns[fld.name] = [None] * n
    return pa.table({f.name: pa.array(columns[f.name], f.type) for f in schema})


def write_labels(env: RekeyEnv, kind: str, name: str, pairs: list[tuple[str, str]]) -> Path:
    """Write ``part-<name>.parquet`` of ``kind`` (``gold/_reviews`` uses the human schema)."""
    folder = env.labels / QSV / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"part-{name}.parquet"
    schema_kind = "gold_reviews" if kind == "gold/_reviews" else kind
    pq.write_table(label_table(schema_kind, pairs), path)
    return path


@contextmanager
def open_rekey_env(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[RekeyEnv]:
    """Config, old key active and new key staged, warehouse planted; no labels yet.

    Each test module wraps it in its own ``rekey_env`` fixture (no fixture import to shadow).
    """
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    old = {rid: hash_under(cfg, OLD_KEY, short, body) for _, rid, short, body in RECORDS}
    old.update(dict.fromkeys(AMBIGUOUS_IDS, SHARED_OLD))
    new = {rid: hash_under(cfg, NEW_KEY, short, body) for _, rid, short, body in RECORDS}
    fake_keyring.store[(SVC, "redact.hmac_key")] = OLD_KEY
    fake_keyring.store[(SVC, "redact.hmac_key.next")] = NEW_KEY
    env = RekeyEnv(cfg, fake_keyring, old, new)
    _plant_warehouse(env)
    try:
        yield env
    finally:
        c.reset_config()
