"""Tests for herness.core._log_pipeline (U00-40 … U00-43)."""

import datetime
import decimal
import json
import logging
import pathlib
from typing import Any

import pydantic
import pytest

from herness.core import _log_pipeline as lp
from herness.core import time as clock

pytestmark = pytest.mark.unit


class _Unprintable:
    def __str__(self) -> str:
        raise ValueError("no")  # noqa: EM101


def test_ut00_47_normalize_values() -> None:
    """UT00-47 values become JSON-native; too-deep containers become a placeholder."""
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    event: dict[str, Any] = {
        "dec": decimal.Decimal("1.5"),
        "path": pathlib.PureWindowsPath("C:/x/y"),
        "secret": pydantic.SecretStr("x"),
        "naive": datetime.datetime(2026, 9, 24, 10, 0),  # noqa: DTZ001
        "set": {"b", "a"},
        "deep": deep,
        "bad": _Unprintable(),
    }
    out = lp.normalize_values(None, "info", event)
    assert out["dec"] == "1.5"
    assert out["path"] == "C:/x/y"
    assert out["secret"] == "**********"  # noqa: S105
    assert out["naive"] == "2026-09-24T10:00:00 naive"
    assert out["set"] == ["a", "b"]
    assert out["deep"]["a"]["b"]["c"]["d"] == lp.TOO_DEEP
    leaf = lp.normalize_values(None, "info", {"v": {"a": {"b": {"c": {"d": 5}}}}})
    assert leaf["v"]["a"]["b"]["c"]["d"] == "5"
    assert out["bad"] == "<unprintable _Unprintable>"


def test_st00_02_guard_processor_level() -> None:
    """ST00-02 (processor level) secrets always omitted; free text omitted above DEBUG."""

    def event(level: str) -> dict[str, Any]:
        return {
            "level": level,
            "event": "core.test.guard",
            "prompt": "p",
            "description": "d",
            "api_key": "k",
            "nested": {"access_token": "t"},
        }

    info = lp.guard_sensitive(None, "info", event("info"))
    assert info["prompt"] == info["description"] == info["api_key"] == lp.OMITTED
    assert info["nested"]["access_token"] == lp.OMITTED
    debug = lp.guard_sensitive(None, "debug", event("debug"))
    assert (debug["prompt"], debug["description"]) == ("p", "d")
    assert debug["api_key"] == debug["nested"]["access_token"] == lp.OMITTED


def test_st00_02_guard_lists_and_deep_keys() -> None:
    """ST00-02 (processor level) keys inside lists and at nesting level 5 are guarded."""
    event: dict[str, Any] = {
        "level": "info",
        "event": "core.test.guard",
        "users": [{"api_key": "k"}, ["x", {"token": "t"}]],
        "items": [{"description": "d", "id": 1}],
        "a": {"b": {"c": {"d": {"password": "p", "e": {"secret": "s"}}}}},
    }
    out = lp.guard_sensitive(None, "info", lp.normalize_values(None, "info", event))
    assert out["users"] == [{"api_key": lp.OMITTED}, ["x", {"token": lp.OMITTED}]]
    assert out["items"] == [{"description": lp.OMITTED, "id": 1}]
    assert out["a"]["b"]["c"]["d"] == {"password": lp.OMITTED, "e": lp.TOO_DEEP}


def test_st00_03_render_json_no_line_forging() -> None:
    """ST00-03 (processor level) an injected newline cannot start a second line."""
    value = 'x"}\n{"level":"critical","event":"core.fake.injected"'
    line = lp.render_json(
        None,
        "info",
        {"ts": "t", "level": "info", "event": "core.test.inject", "component": "c", "v": value},
    )
    assert "\n" not in line
    assert json.loads(line)["event"] == "core.test.inject"


def test_st00_04_limit_and_drop() -> None:
    """ST00-04 (processor level) long fields cut; oversized lines drop the largest fields."""
    event: dict[str, Any] = {
        "ts": "t",
        "level": "info",
        "event": "core.test.big",
        "component": "c",
        "pid": 1,
        "huge": "h" * 100_000,
    }
    event.update({f"f{i:02d}": "x" * 1500 for i in range(20)})
    line = lp.render_json(None, "info", lp.limit_sizes(None, "info", event))
    out = json.loads(line)
    assert len(line.encode("utf-8")) <= lp.MAX_LINE_BYTES
    assert "huge" in out["dropped_fields"]
    for key in ("ts", "level", "event", "component", "pid"):
        assert key in out
    cut = lp.limit_sizes(None, "info", {"v": "h" * 100_000})["v"]
    assert cut == "h" * lp.MAX_FIELD_CHARS + "\u2026[truncated]"


def test_ft00_01_sink_failure_and_recovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FT00-01 a failed open drops lines for 60 s, then recovers and reports the count."""
    real_open = lp._open_append
    calls = {"n": 0}

    def flaky(path: pathlib.Path) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")  # noqa: EM101
        return real_open(path)

    now = [0.0]
    monkeypatch.setattr(lp, "_open_append", flaky)
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    handler = lp.DailyJsonlHandler(tmp_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for when, message in ((0.0, "one"), (10.0, "two"), (61.0, "three")):
        now[0] = when
        handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, message, None, None))
    handler.close()
    failed = [line for line in capsys.readouterr().err.splitlines() if "sink_failed" in line]
    assert len(failed) == 1
    (day_file,) = tmp_path.glob("herness-*.jsonl")
    lines = day_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["event"] == "core.logging.sink_recovered"
    assert first["dropped_lines"] == 2
    assert lines[1] == "three"
