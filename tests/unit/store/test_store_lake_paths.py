"""UT02-13 `partition_dir` containment with the Windows path shapes seen in T11-14c.

The check resolves both sides with `Path.resolve(strict=False)`. On Windows that is
CPython's `ntpath.realpath`, which looks the path up twice when it does not exist: once to
fail, once after stripping the `\\\\?\\` prefix. If the partition's parent directory appears
between the two lookups (another process creating it), the error codes differ and the
prefix is kept, so the partition reads as outside the raw root. The tests below replay that
interleaving deterministically by creating a directory inside the first failed lookup.
They pin CPython 3.12 `ntpath` behaviour (the project's interpreter, R-58).
"""

import ctypes
import datetime
import ntpath
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from herness.core.errors import ConfigError
from herness.store import lake

pytestmark = pytest.mark.unit

_DAY = datetime.date(2026, 6, 2)
_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows path shapes")


def _raw(tmp_path: Path) -> Path:
    raw = (tmp_path / "Long Directory Name" / "raw").resolve()
    raw.mkdir(parents=True)
    return raw


def _short(path: Path) -> Path:
    """The 8.3 short form of an existing path (the long form if 8.3 names are off)."""
    buffer = ctypes.create_unicode_buffer(1024)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]  # Windows only
    size = kernel32.GetShortPathNameW(str(path), buffer, 1024)
    assert size > 0
    return Path(buffer.value)


def _creating(monkeypatch: pytest.MonkeyPatch, target: Path, appear: Path) -> Callable[[], bool]:
    """Patch `ntpath._getfinalpathname` so that `appear` is created right after the first
    failed lookup of `target`, the moment a concurrent worker's `mkdir` lands in production.
    Returns a probe telling whether the injection happened."""
    real = ntpath._getfinalpathname  # type: ignore[attr-defined]
    fired: list[bool] = []

    def lookup(path: str) -> str:
        try:
            return str(real(path))
        except OSError:
            if not fired and os.path.normcase(path) == os.path.normcase(str(target)):
                fired.append(True)
                appear.mkdir(parents=True)
            raise

    monkeypatch.setattr(ntpath, "_getfinalpathname", lookup)
    return lambda: bool(fired)


def test_ut02_13_missing_parents_resolve_inside(tmp_path: Path) -> None:
    """UT02-13 a partition whose source, entity and `dt=` directories do not exist yet is
    inside the raw root (nothing created by the check)."""
    raw = _raw(tmp_path)
    part = lake.partition_dir(raw, "servicenow", "incident", _DAY)
    assert part == raw / "servicenow" / "incident" / "dt=2026-06-02"
    assert list(raw.iterdir()) == []


@_windows
def test_ut02_13_short_name_and_prefixed_raw_roots(tmp_path: Path) -> None:
    """UT02-13 a raw root given in 8.3 short form or with the `\\\\?\\` prefix contains its
    partitions, existing or not."""
    raw = _raw(tmp_path)
    (raw / "jira" / "issue").mkdir(parents=True)
    for root in (_short(raw), Path("\\\\?\\" + str(raw))):
        for entity in ("issue", "missing_entity"):
            part = lake.partition_dir(root, "jira", entity, _DAY)
            assert part == root / "jira" / entity / "dt=2026-06-02"


@_windows
def test_ut02_13_parent_appearing_mid_resolve_reads_as_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-13 / T11-14c root cause: the entity directory appears between the two lookups
    of a missing partition, `resolve` keeps the `\\\\?\\` prefix, and the check (correctly
    strict about its input) rejects the partition. This is why synth creates the entity
    directories before its workers start (`tools.synth.shards.prepare_lake`)."""
    raw = _raw(tmp_path)
    target = raw / "servicenow" / "incident" / "dt=2026-06-02"
    fired = _creating(monkeypatch, target, raw / "servicenow" / "incident")
    assert str(target.resolve(strict=False)).startswith("\\\\?\\")
    assert fired()
    monkeypatch.undo()
    (raw / "servicenow" / "incident").rmdir()
    fired = _creating(monkeypatch, target, raw / "servicenow" / "incident")
    with pytest.raises(ConfigError, match="outside the raw root"):
        lake.partition_dir(raw, "servicenow", "incident", _DAY)
    assert fired()


@_windows
def test_ut02_13_leaf_appearing_mid_resolve_stays_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-13 with the entity directory present only the `dt=` leaf can appear between
    the lookups; `resolve` then strips the prefix and the partition is inside."""
    raw = _raw(tmp_path)
    (raw / "servicenow" / "incident").mkdir(parents=True)
    target = raw / "servicenow" / "incident" / "dt=2026-06-02"
    fired = _creating(monkeypatch, target, target)
    assert lake.partition_dir(raw, "servicenow", "incident", _DAY) == target
    assert fired()


@pytest.mark.parametrize("form", ["plain", "short", "prefixed"])
def test_ut02_13_dotdot_escape_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """UT02-13 / TH02-01 past name validation, `..` segments that leave the raw root are
    rejected for every raw root shape, with or without existing parents."""
    if form != "plain" and sys.platform != "win32":
        pytest.skip("Windows path shapes")
    raw = _raw(tmp_path)
    (raw.parent / "outside").mkdir()
    root = {"plain": raw, "short": None, "prefixed": None}[form]
    if form == "short":
        root = _short(raw)
    elif form == "prefixed":
        root = Path("\\\\?\\" + str(raw))
    assert root is not None
    monkeypatch.setattr(lake, "validate_name", lambda kind, value: value)
    for source, entity in (("..", "outside"), ("..", "missing"), ("x", "../../..")):
        with pytest.raises(ConfigError, match="outside the raw root"):
            lake.partition_dir(root, source, entity, _DAY)
