"""Tests for the egress JSONL writer and daily token counter (impl 10 U10-57; UT10-52)."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import pytest

from herness.core import egress_log as el
from herness.core import time as clock
from herness.core.egress_log import EgressLog
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def _allowed(egress_id: str, tokens: int) -> dict[str, object]:
    return {"egress_id": egress_id, "decision": "allowed", "tokens_in": tokens}


def _completed(egress_id: object, tokens_in: int, tokens_out: int | None) -> dict[str, object]:
    return {
        "egress_id": egress_id,
        "decision": "completed",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


@pytest.fixture
def log(tmp_path: Path) -> EgressLog:
    return EgressLog(tmp_path / "logs", "cfg_0123456789abcdef", "hybrid")


def _day_file(tmp_path: Path) -> Path:
    return tmp_path / "logs" / f"egress-{clock.utc_day(clock.now())}.jsonl"


def test_ut10_52_write_adds_ts_profile_and_config_hash(tmp_path: Path, log: EgressLog) -> None:
    """UT10-52 each line is canonical JSON in egress-<UTC day>.jsonl with ts, profile, hash."""
    log.write(_allowed("egr_1", 10))
    raw = _day_file(tmp_path).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    line = json.loads(raw)
    assert TS_RE.fullmatch(line["ts"])
    assert line["profile"] == "hybrid"
    assert line["config_hash"] == "cfg_0123456789abcdef"
    assert raw == json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def test_ut10_52_unknown_key_is_schema_violation(tmp_path: Path, log: EgressLog) -> None:
    """UT10-52 a key outside design 10 §4.5 is refused without echoing it; nothing written."""
    with pytest.raises(SchemaViolation) as info:
        log.write({"egress_id": "egr_1", "payload": "secret-text"})
    assert "payload" not in str(info.value)
    assert not _day_file(tmp_path).exists()


def test_ut10_52_tokens_today_allowed_and_completed(log: EgressLog) -> None:
    """UT10-52 allowed adds tokens_in; completed replaces it with in + out; blocked adds none."""
    now = clock.now()
    assert log.tokens_today(now) == 0
    log.write(_allowed("egr_a", 100))
    log.write({"egress_id": "egr_b", "decision": "blocked", "tokens_in": 5000})
    assert log.tokens_today(now) == 100
    log.write(_completed("egr_a", 120, 30))
    assert log.tokens_today(now) == 150
    log.write(_allowed("egr_c", 40))
    log.write(_completed("egr_c", 40, None))
    log.write(_completed("egr_unknown", 7, 3))  # no remembered allowed line: adds all of it
    assert log.tokens_today(now) == 200


def test_ut10_52_tokens_today_is_incremental_and_skips_bad_lines(
    tmp_path: Path, log: EgressLog
) -> None:
    """UT10-52 only new whole lines are read; torn, foreign and non-object lines add nothing."""
    now = clock.now()
    log.write(_allowed("egr_a", 10))
    assert log.tokens_today(now) == 10
    path = _day_file(tmp_path)
    with path.open("ab") as fh:
        fh.write(b"not json\n[1, 2]\n")
        fh.write(b'{"decision":"completed","egress_id":7,"tokens_in":true}\n')
        fh.write(b'{"decision":"allowed","egress_id":"egr_t","tokens_in":5')  # torn tail
    assert log.tokens_today(now) == 10
    with path.open("ab") as fh:
        fh.write(b"}\n")
    assert log.tokens_today(now) == 15


def test_ut10_52_tokens_today_resets_at_utc_date_change(log: EgressLog) -> None:
    """UT10-52 another UTC day starts from zero; returning re-reads the day from the start."""
    now = clock.now()
    log.write(_allowed("egr_a", 10))
    assert log.tokens_today(now) == 10
    assert log.tokens_today(now + datetime.timedelta(days=1)) == 0
    assert log.tokens_today(now) == 10


def test_ut10_52_map_is_bounded(monkeypatch: pytest.MonkeyPatch, log: EgressLog) -> None:
    """UT10-52 past the map limit a completed line adds its full figure (never under-counts)."""
    monkeypatch.setattr(el, "MAX_REMEMBERED", 1)
    now = clock.now()
    log.write(_allowed("egr_a", 10))
    log.write(_allowed("egr_b", 10))
    log.write(_completed("egr_a", 10, 0))
    log.write(_completed("egr_b", 10, 0))
    assert log.tokens_today(now) == 30


def test_ut10_52_two_instances_share_the_day_total(tmp_path: Path, log: EgressLog) -> None:
    """UT10-52 a second writer on the same logs dir is seen by the first (separate state)."""
    other = EgressLog(tmp_path / "logs", "cfg_other", "hybrid")
    other.write(_allowed("egr_x", 70))
    with log.locked():  # a write under the held lock must not wait for itself
        log.write(_allowed("egr_y", 5))
        assert log.tokens_today(clock.now()) == 75
    assert other.tokens_today(clock.now()) == 75
