"""Files connector listing, fingerprint, check and contract tests (T01-09)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs
from tests.unit.connectors import _files_data as d

from herness.connectors import files
from herness.connectors.base import (
    KEY_SCHEMA,
    METADATA_FIELDS,
    METADATA_SCHEMA,
    Connector,
    SupportsKeyListing,
)
from herness.connectors.files import (
    MAX_INBOX_FILE_BYTES,
    FilesConnector,
    InboxFile,
    InboxFileChanged,
    fingerprint_file,
)
from herness.core import registry
from herness.core.errors import ConfigError, SchemaViolation, SourceUnavailable

pytestmark = pytest.mark.unit

_CSV = "id,name\n1,a\n2,b\n"


def _rejects(logs: list[dict[str, Any]]) -> dict[str, str]:
    return {e["file"]: e["reason"] for e in logs if e["event"] == "connectors.files.rejected"}


def _warned(logs: list[dict[str, Any]]) -> dict[str, str]:
    rejected = [e for e in logs if e["event"] == "connectors.files.rejected"]
    return {e["file"]: e["reason"] for e in rejected if e["log_level"] == "warning"}


def _symlink(target: Path, link: Path, *, is_dir: bool = False) -> bool:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except OSError:  # Windows without the symlink privilege
        return False
    return True


def _junction(target: Path, link: Path) -> bool:
    winapi = pytest.importorskip("_winapi") if sys.platform == "win32" else None
    if winapi is None or not hasattr(winapi, "CreateJunction"):
        return False
    winapi.CreateJunction(str(target), str(link))
    return True


def test_ut01_45_candidates_keep_only_eligible_files(tmp_path: Path) -> None:
    """UT01-45 dotfiles, ~$ locks, links, junctions, settling and wrong suffixes are skipped."""
    root = tmp_path / "inbox"
    d.drop(root, "teams/b.csv", _CSV, age_s=7200)
    d.drop(root, "teams/a.csv", _CSV, age_s=7200)
    d.drop(root, "teams/c.csv", _CSV, age_s=9000)
    d.drop(root, "teams/.hidden.csv", _CSV)
    d.drop(root, "teams/~$x.csv", _CSV)
    d.drop(root, "teams/fresh.csv", _CSV, age_s=2)
    d.drop(root, "teams/notes.txt", "x")
    (root / "teams" / "dir.csv").mkdir()
    outside = d.drop(tmp_path, "outside/secret.csv", _CSV)
    linked = _symlink(outside, root / "teams" / "link.csv")
    junction = _junction(outside.parent, root / "teams" / "junc.csv")
    with capture_logs() as logs:
        found = d.connector(root).candidates("teams")
    assert [f.rel_path for f in found] == ["teams/c.csv", "teams/a.csv", "teams/b.csv"]
    first = found[0]
    assert first.path == (root / "teams" / "c.csv").resolve()
    assert first.size_bytes == len(_CSV)
    assert first.mtime_ns == d.mtime_ns(9000)
    assert first.mtime.tzinfo is not None
    expected = {"teams/dir.csv": "not_regular"}
    if linked:
        expected["teams/link.csv"] = "symlink"
    if junction:
        expected["teams/junc.csv"] = "symlink"
    assert _warned(logs) == expected
    assert _rejects(logs)["teams/fresh.csv"] == "settling"  # DEBUG only
    assert all(e["log_level"] == "debug" for e in logs if e.get("reason") == "settling")


def test_ut01_45_candidates_skip_xlsx_lock_file(tmp_path: Path) -> None:
    """UT01-45 an Excel ``~$`` lock file never becomes a candidate."""
    d.drop(tmp_path, "sheets/~$x.xlsx", b"lock")
    d.drop(tmp_path, "sheets/t.xlsx", b"PK")
    entities = {"sheets": {"pattern": "*.xlsx", "key_field": ["id"], "sheet": "Teams"}}
    with capture_logs() as logs:
        found = d.connector(tmp_path, entities).candidates("sheets")
    assert [f.rel_path for f in found] == ["sheets/t.xlsx"]
    assert _rejects(logs) == {}


def test_ut01_45_missing_folder_and_unknown_entity(tmp_path: Path) -> None:
    """UT01-45 a missing entity folder lists nothing; an unknown entity is a ConfigError."""
    conn = d.connector(tmp_path)
    assert conn.candidates("teams") == []
    with pytest.raises(ConfigError, match="unknown files entity"):
        conn.candidates("nope")


def test_ut01_45_unreadable_folder_is_source_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-45 an OSError while listing the folder raises SourceUnavailable."""
    (tmp_path / "teams").mkdir()

    def boom(self: Path, pattern: str) -> list[Path]:
        raise PermissionError(pattern)

    monkeypatch.setattr(Path, "glob", boom)
    with pytest.raises(SourceUnavailable, match="inbox unreadable"):
        d.connector(tmp_path).candidates("teams")


def test_ut01_45_file_error_rejects_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-45 a file that vanishes between listing and lstat is rejected as unreadable."""
    d.drop(tmp_path, "teams/a.csv", _CSV)
    real = Path.lstat

    def gone(self: Path) -> os.stat_result:
        if self.name == "a.csv":
            raise FileNotFoundError(self.name)
        return real(self)

    monkeypatch.setattr(Path, "lstat", gone)
    with capture_logs() as logs:
        assert d.connector(tmp_path).candidates("teams") == []
    assert _warned(logs) == {"teams/a.csv": "unreadable"}


def test_ut01_45_linked_entity_folder_is_rejected(tmp_path: Path) -> None:
    """UT01-45 an entity folder that is itself a junction or symlink is not listed."""
    target = tmp_path / "elsewhere"
    d.drop(target, "a.csv", _CSV)
    root = tmp_path / "inbox"
    root.mkdir()
    if not (_junction(target, root / "teams") or _symlink(target, root / "teams", is_dir=True)):
        pytest.skip("cannot create a junction or directory symlink here")
    with capture_logs() as logs:
        assert d.connector(root).candidates("teams") == []
    assert _warned(logs) == {"teams": "symlink"}


def test_ut01_45_check_inbox_and_entity_folders(tmp_path: Path) -> None:
    """UT01-45 check() needs a real inbox directory and warns about missing entity folders."""
    with pytest.raises(ConfigError, match="inbox missing or not a directory"):
        d.connector(tmp_path / "missing").check()
    (tmp_path / "file").write_text("x")
    with pytest.raises(ConfigError, match="inbox missing or not a directory"):
        d.connector(tmp_path / "file").check()
    root = tmp_path / "inbox"
    (root / "teams").mkdir(parents=True)
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"]},
        "sites": {"pattern": "*.csv", "key_field": ["id"]},
    }
    with capture_logs() as logs:
        d.connector(root, entities).check()
    missing = [e for e in logs if e["event"] == "connectors.files.entity_folder_missing"]
    assert [(e["entity"], e["log_level"]) for e in missing] == [("sites", "warning")]
    link = tmp_path / "link"
    if _junction(root, link) or _symlink(root, link, is_dir=True):
        with pytest.raises(ConfigError, match="inbox missing or not a directory"):
            d.connector(link).check()


def test_ut01_45_watermark_field(tmp_path: Path) -> None:
    """UT01-45 the watermark field is the updated_field, else ``mtime``."""
    entities = {
        "teams": {"pattern": "*.csv", "key_field": ["id"]},
        "sites": {"pattern": "*.csv", "key_field": ["id"], "updated_field": "Last Modified"},
    }
    conn = d.connector(tmp_path, entities)
    assert conn.watermark_field("teams") == "mtime"
    assert conn.watermark_field("sites") == "Last Modified"
    assert conn.name == "files"
    assert conn.entities == ("teams", "sites")


def _inbox_file(path: Path, rel: str = "teams/a.csv") -> InboxFile:
    st = path.stat()
    return InboxFile(path, rel, st.st_size, files._mtime(st.st_mtime_ns), st.st_mtime_ns)


def test_ut01_46_fingerprint_same_bytes_same_digest(tmp_path: Path) -> None:
    """UT01-46 renamed files with identical bytes give the same SHA-256."""
    data = b"id,name\n" + b"1,x\n" * 300_000  # spans several chunks
    one = d.drop(tmp_path, "teams/one.csv", data)
    two = d.drop(tmp_path, "teams/two.csv", data)
    digest = fingerprint_file(_inbox_file(one, "teams/one.csv"))
    assert digest == fingerprint_file(_inbox_file(two, "teams/two.csv"))
    assert digest == hashlib.sha256(data).hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()


def test_ut01_46_fingerprint_detects_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT01-46 a file appended while it is hashed raises InboxFileChanged."""
    path = d.drop(tmp_path, "teams/a.csv", _CSV)
    f = _inbox_file(path)

    class Appending(io.BytesIO):
        """The first read appends one row, as a writer still copying the file would."""

        appended = False

        def read(self, size: int | None = -1) -> bytes:
            if not self.appended:
                self.appended = True
                with path.open("ab") as fh:
                    fh.write(b"3,c\n")
                pos = self.tell()
                self.seek(0, io.SEEK_END)
                self.write(b"3,c\n")
                self.seek(pos)
            return super().read(size)

    monkeypatch.setattr(
        files, "open", lambda *_a, **_k: Appending(path.read_bytes()), raising=False
    )
    with pytest.raises(InboxFileChanged) as info:
        fingerprint_file(f)
    assert isinstance(info.value, SchemaViolation)
    assert info.value.context == {"entity": "teams", "file": "teams/a.csv"}


def test_ut01_46_fingerprint_detects_mtime_change(tmp_path: Path) -> None:
    """UT01-46 a changed ``mtime_ns`` after hashing raises InboxFileChanged."""
    path = d.drop(tmp_path, "teams/a.csv", _CSV)
    f = _inbox_file(path)
    os.utime(path, ns=(d.mtime_ns(10), d.mtime_ns(10)))
    with pytest.raises(InboxFileChanged):
        fingerprint_file(f)


def test_ut01_46_fingerprint_missing_file(tmp_path: Path) -> None:
    """UT01-46 an unreadable file raises SourceUnavailable."""
    path = d.drop(tmp_path, "teams/a.csv", _CSV)
    f = _inbox_file(path)
    path.unlink()
    with pytest.raises(SourceUnavailable, match="inbox file unreadable"):
        fingerprint_file(f)


def test_ut01_50_sparse_file_over_limit_rejected(tmp_path: Path) -> None:
    """UT01-50 a 1 GiB + 1 byte (sparse) file is rejected as too_large."""
    big = d.sparse(tmp_path / "teams" / "big.csv", MAX_INBOX_FILE_BYTES + 1)
    edge = d.sparse(tmp_path / "teams" / "edge.csv", MAX_INBOX_FILE_BYTES)
    try:
        with capture_logs() as logs:
            found = d.connector(tmp_path).candidates("teams")
        assert [f.rel_path for f in found] == ["teams/edge.csv"]
        assert _warned(logs) == {"teams/big.csv": "too_large"}
    finally:
        big.unlink()
        edge.unlink()


def test_st01_08_symlink_to_system_file_skipped(tmp_path: Path) -> None:
    """ST01-08 a symlink to a system file is skipped; ``..`` patterns are rejected."""
    target = Path(os.environ.get("SYSTEMROOT", "C:/Windows"), "win.ini")
    if sys.platform != "win32":
        target = Path("/etc/passwd")
    d.drop(tmp_path, "teams/ok.csv", _CSV)
    if not _symlink(target, tmp_path / "teams" / "win.csv"):
        pytest.skip("symlink privilege missing; the pattern part runs in the next test")
    with capture_logs() as logs:
        found = d.connector(tmp_path).candidates("teams")
    assert [f.rel_path for f in found] == ["teams/ok.csv"]
    assert _warned(logs) == {"teams/win.csv": "symlink"}


@pytest.mark.parametrize("pattern", ["..\\*.csv", "../*.csv", "sub/*.csv"])
def test_st01_08_escaping_pattern_rejected(pattern: str) -> None:
    """ST01-08 a pattern that leaves the entity folder fails settings validation."""
    with pytest.raises(ValidationError, match="pattern must be a glob"):
        d.settings({"teams": {"pattern": pattern, "key_field": ["id"]}})


def test_st01_08_glob_escape_caught_by_containment(tmp_path: Path) -> None:
    """ST01-08 even an unvalidated ``../`` glob cannot read outside the entity folder."""
    d.drop(tmp_path, "up.csv", _CSV)
    (tmp_path / "teams").mkdir()
    cfg = d.settings()
    unsafe = cfg.entities["teams"].model_copy(update={"pattern": "../*.csv"})  # no validation
    cfg = cfg.model_copy(update={"entities": {"teams": unsafe}})
    conn = FilesConnector(cfg, inbox_root=tmp_path, clock=lambda: d.NOW)
    with capture_logs() as logs:
        assert conn.candidates("teams") == []
    assert list(_warned(logs).values()) == ["outside_root"]


def test_st01_09_sparse_csv_rejected_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST01-09 a 1.1 GiB sparse CSV is rejected by size before any read."""
    big = d.sparse(tmp_path / "teams" / "big.csv", int(1.1 * 1024**3))

    def no_read(*_a: object, **_k: object) -> None:
        pytest.fail("the file must not be opened")

    monkeypatch.setattr(files.duckdb, "connect", no_read)
    monkeypatch.setattr(files, "open", no_read, raising=False)
    try:
        with capture_logs() as logs:
            assert list(d.connector(tmp_path).sync("teams", None)) == []
        assert _warned(logs) == {"teams/big.csv": "too_large"}
    finally:
        big.unlink()


def test_ut01_94_files_connector_contract(tmp_path: Path) -> None:
    """UT01-94 FilesConnector satisfies Connector and SupportsKeyListing."""
    d.drop(tmp_path, "teams/a.csv", _CSV)
    conn: Connector = d.connector(
        tmp_path, {"teams": {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"}}
    )
    assert isinstance(conn, SupportsKeyListing)
    for name in ("check", "sync", "watermark_field", "list_keys"):
        assert callable(getattr(conn, name))
    conn.check()
    (batch,) = list(conn.sync("teams", None))
    assert batch.schema.names[: len(METADATA_FIELDS)] == list(METADATA_FIELDS)
    for i, field in enumerate(METADATA_SCHEMA):
        assert batch.schema.field(i).type == field.type
    (keys,) = list(conn.list_keys("teams"))
    assert keys.schema == KEY_SCHEMA
    assert keys.column(0).to_pylist() == ["1", "2"]


def test_ut01_94_registered_as_files_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT01-94 importing the module registers the class as connector ``files``."""
    spec = importlib.util.spec_from_file_location("_files_probe", files.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_files_probe", probe)  # dataclasses look it up
    spec.loader.exec_module(probe)  # the autouse fixture reset the registry before this test
    cls = registry.get("connector", "files")
    assert cls is probe.FilesConnector
    assert cls.__qualname__ == FilesConnector.__qualname__


def test_ut01_94_key_batches_are_strings(tmp_path: Path) -> None:
    """UT01-94 list_keys yields KEY_SCHEMA string batches."""
    d.drop(tmp_path, "teams/a.csv", _CSV)
    entities = {"teams": {"pattern": "*.csv", "key_field": ["id"], "mode": "snapshot"}}
    batches = list(d.connector(tmp_path, entities).list_keys("teams"))
    assert all(b.schema == KEY_SCHEMA and pa.types.is_string(b.column(0).type) for b in batches)
