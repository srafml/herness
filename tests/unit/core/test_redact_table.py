"""Tests for redact_table and its process pool (impl 10 T10-11, U10-47).

The HMAC keys are built at runtime so the detect-secrets baseline does not change.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import _redact_pool as pool
from herness.core import config as c
from herness.core import redact as r
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.unit

KEY = bytes(range(32))
POISON = "x" * (r.MAX_TEXT_CHARS + 1)  # RedactionFailed("text too long") in any process


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    c.reset_config()
    yield
    c.reset_config()
    reset_logging()


def _keyring_config(tmp_path: Path, fake_keyring: MemoryKeyring, **build: Any) -> None:
    cfg_dir = write_full_config(tmp_path)
    if build:
        (cfg_dir / "sources.yaml").write_text(
            "version: 1\nbuild: " + json.dumps(build) + "\n", "utf-8"
        )
    fake_keyring.store[("herness", "redact.hmac_key")] = KEY.hex()
    c.init_config("local", config_dir=cfg_dir, env={})


def dotenv_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config whose key lives in ``<root>/.env`` so that spawned workers can resolve it."""
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace(
        "security:\n", "security:\n  secrets: {backend: dotenv}\n"
    )
    herness.write_text(text, "utf-8")
    (tmp_path / ".env").write_text(f"HERNESS_SECRET__REDACT_HMAC_KEY={KEY.hex()}\n", "utf-8")
    monkeypatch.setenv("HERNESS_ENV", "dev")
    c.init_config("local", config_dir=cfg_dir)
    return cfg_dir


def _events(capsys: pytest.CaptureFixture[str], name: str) -> list[dict[str, Any]]:
    lines = [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]
    return [line for line in lines if line["event"] == name]


# --- UT10-43 joined text, poisoned row NULL -----------------------------------------------------


def test_ut10_43_joins_columns_and_fails_poisoned_row_closed(
    tmp_path: Path, fake_keyring: MemoryKeyring, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-43 two text columns: joined with a blank line; the poisoned row is NULL, failed=1."""
    _keyring_config(tmp_path, fake_keyring)
    configure_logging("INFO")
    capsys.readouterr()
    tbl = pa.table(
        {
            "record_id": ["INC1", "INC2", "INC3", "INC4"],
            "short": ["mail ann@corp.test", None, None, "fine"],
            "body": ["call 212-555-7788", "only body", None, POISON],
        }
    )
    out = r.redact_table(tbl, ["short", "body"], workers=1)
    assert out.schema.names == ["record_id", "text"]
    assert out.schema.field("text").type == pa.string()
    assert out.column("record_id").to_pylist() == ["INC1", "INC2", "INC3", "INC4"]
    red = r.get_redactor()
    email = red.pseudonym("EMAIL", "ann@corp.test")
    phone = red.pseudonym("PHONE", "212-555-7788")
    assert out.column("text").to_pylist() == [
        f"mail {email}\n\ncall {phone}",
        "only body",
        None,
        None,
    ]
    err = capsys.readouterr().err
    assert "ann@corp.test" not in err
    assert "xxxxxxxx" not in err
    lines = [json.loads(text) for text in err.splitlines() if text.strip()]
    failed = [line for line in lines if line["event"] == "redact.record.failed"]
    assert [(f["record_id"], f["error_type"]) for f in failed] == [("INC4", "RedactionFailed")]
    done = [line for line in lines if line["event"] == "redact.table.completed"]
    assert len(done) == 1
    assert (done[0]["rows"], done[0]["failed_rows"], done[0]["workers"]) == (4, 1, 1)
    assert isinstance(done[0]["duration_ms"], int)


def test_ut10_43_large_string_id_and_empty_inputs(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-43 large_string columns are accepted; no rows or no text columns give NULL text."""
    _keyring_config(tmp_path, fake_keyring)
    ids = pa.array(["a", "b"], type=pa.large_string())
    tbl = pa.table({"id": ids, "t": pa.array(["x", None], type=pa.large_string())})
    out = r.redact_table(tbl, [], id_col="id", workers=1)
    assert out.schema.names == ["id", "text"]
    assert out.column("id").type == pa.string()
    assert out.column("text").to_pylist() == [None, None]
    empty = r.redact_table(tbl.slice(0, 0), ["t"], id_col="id", workers=1)
    assert empty.num_rows == 0
    assert empty.schema.names == ["id", "text"]


@pytest.mark.parametrize(
    ("columns", "text_cols", "missing"),
    [
        ({"record_id": ["1"], "t": ["x"]}, ["u"], "u"),
        ({"t": ["x"]}, ["t"], "record_id"),
        ({"record_id": ["1"], "t": [1]}, ["t"], "t"),
        ({"record_id": [1], "t": ["x"]}, ["t"], "record_id"),
    ],
)
def test_ut10_43_missing_or_non_string_column_is_schema_violation(
    columns: dict[str, list[Any]], text_cols: list[str], missing: str
) -> None:
    """UT10-43 a missing or non-string column raises SchemaViolation naming the column."""
    with pytest.raises(SchemaViolation, match=rf"^redact_table: missing column {missing}$"):
        r.redact_table(pa.table(columns), text_cols, workers=1)


def test_ut10_43_duplicate_column_name_is_schema_violation() -> None:
    """UT10-43 a duplicated column name is ambiguous and treated as missing."""
    tbl = pa.Table.from_arrays(
        [pa.array(["1"]), pa.array(["x"]), pa.array(["y"])], names=["record_id", "t", "t"]
    )
    with pytest.raises(SchemaViolation, match=r"^redact_table: missing column t$"):
        r.redact_table(tbl, ["t"], workers=1)


def test_ut10_43_missing_key_is_config_error_before_any_worker(tmp_path: Path) -> None:
    """UT10-43 without the HMAC key redact_table raises ConfigError, not StoreBusy."""
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    tbl = pa.table({"record_id": ["1"] * 3, "t": ["x"] * 3})
    with pytest.raises(ConfigError, match=r"^secret not found: "):
        r.redact_table(tbl, ["t"], workers=4)


def test_ut10_43_worker_count_defaults_to_build_threads_then_cpu_count(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-43 workers: sources.build.threads, else os.cpu_count() or 1; never below 1."""
    _keyring_config(tmp_path / "a", fake_keyring, threads=3)
    assert pool.worker_count(None) == 3
    assert pool.worker_count(0) == 1
    assert pool.worker_count(5) == 5
    c.reset_config()
    _keyring_config(tmp_path / "b", fake_keyring)
    monkeypatch.setattr(pool.os, "cpu_count", lambda: 6)
    assert pool.worker_count(None) == 6
    monkeypatch.setattr(pool.os, "cpu_count", lambda: None)
    assert pool.worker_count(None) == 1


def test_ut10_43_spawn_args_name_the_cached_profile_and_config_dir(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-43 the worker initializer gets the parent's profile and config dir, no overrides."""
    _keyring_config(tmp_path, fake_keyring)
    assert pool.spawn_args() == ("local", (), str(tmp_path.resolve() / "config"))


def test_ut10_43_init_worker_loads_config_and_builds_the_redactor(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-43 _init_worker calls init_config with the spawn args and caches a redactor."""
    fake_keyring.store[("herness", "redact.hmac_key")] = KEY.hex()
    cfg_dir = write_full_config(tmp_path)
    pool._init_worker("local", (), str(cfg_dir))
    assert c._Cache.config is not None
    assert r._State.redactor is not None
    assert pool._redact_chunk((["1"], [["mail ann@corp.test"]]))[1] == []


def test_ut10_43_redact_chunks_in_process_for_one_chunk(
    tmp_path: Path, fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-43 one chunk with several workers runs in-process (no pool is started)."""
    _keyring_config(tmp_path, fake_keyring)

    def no_pool(*args: object, **kwargs: object) -> None:
        msg = "a process pool was started"
        raise AssertionError(msg)

    monkeypatch.setattr(pool, "ProcessPoolExecutor", no_pool)
    tbl = pa.table({"record_id": ["1", "2"], "t": ["a", POISON]})
    out = r.redact_table(tbl, ["t"], workers=8)
    assert out.column("text").to_pylist() == ["a", None]


# --- UT10-44 workers=1 and workers=4 give identical output -------------------------------------


def _rows(count: int) -> pa.Table:
    ids = [f"INC{i:07d}" for i in range(count)]
    short = [
        None if i % 11 == 0 else f"user{i}@corp{i % 7}.test reports outage" for i in range(count)
    ]
    body = [
        POISON if i == count - 3 else f"call 212-555-{i % 10000:04d} from 10.0.{i % 256}.{i % 200}"
        for i in range(count)
    ]
    return pa.table({"record_id": ids, "short": short, "body": body})


def test_ut10_44_workers_1_and_4_give_identical_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-44 50k rows: workers=1 and workers=4 (spawned pool) give an identical table."""
    dotenv_config(tmp_path, monkeypatch)
    tbl = _rows(50_000)
    single = r.redact_table(tbl, ["short", "body"], workers=1)
    multi = r.redact_table(tbl, ["short", "body"], workers=4)
    assert multi.equals(single)
    assert single.num_rows == 50_000
    assert single.column("text")[50_000 - 3].as_py() is None
    assert single.column("text").null_count == 1


class _RaisingRedactor:
    """Stand-in redactor whose ``redact`` raises an unexpected (non-RedactionFailed) error."""

    def redact(self, text: str | None) -> Any:
        if text == "boom":
            msg = "unexpected"
            raise ValueError(msg)
        return None if text is None else r.RedactionResult(text, {})


def test_ut10_43_any_exception_in_a_row_fails_that_row_closed() -> None:
    """UT10-43 a row whose redaction raises any exception is NULL and counted as failed."""
    chunk: pool.Chunk = (["1", "2", "3"], [["ok", "boom", None]])
    out, failed = pool.redact_rows(_RaisingRedactor(), chunk)  # type: ignore[arg-type]
    assert out == ["ok", None, None]
    assert failed == ["2"]


def test_ut10_43_pool_keeps_at_most_two_chunks_per_worker_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-43 more chunks than 2 x workers: the pool drains in order, same output as 1 worker."""
    dotenv_config(tmp_path, monkeypatch)
    monkeypatch.setattr(pool, "CHUNK_ROWS", 2)
    tbl = _rows(11)  # 6 chunks, workers=2 -> at most 4 in flight
    single = r.redact_table(tbl, ["short", "body"], workers=1)
    multi = r.redact_table(tbl, ["short", "body"], workers=2)
    assert multi.equals(single)
    assert multi.column("text").null_count == 1
