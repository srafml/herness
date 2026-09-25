"""Tests for settings_base source, entity, reconcile and backfill models (U01-02 … U01-05)."""

import datetime
from collections.abc import Mapping
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from herness.connectors import settings_base as sb
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

UTC = datetime.UTC


class _ServiceNow(sb.SourceSettings):
    SOURCE: ClassVar[str] = "servicenow"
    entities: Mapping[str, sb.EntitySettings] = {}


class _ServiceNowChild(_ServiceNow):
    pass


class _Jira(sb.SourceSettings):
    SOURCE: ClassVar[str] = "jira"


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 9, 25, 14, 30, tzinfo=UTC)


# UT01-05 --------------------------------------------------------------------


def _source(**extra: Any) -> _ServiceNow:
    data: dict[str, Any] = {
        "overlap_minutes": 45,
        "page_size": 500,
        "backfill": {"start": datetime.date(2024, 1, 1), "slice_days": 30},
        "entities": {
            "incident": {"overlap_minutes": 5, "page_size": 50, "backfill": {"slice_days": 7}},
            "problem": {},
        },
    }
    data.update(extra)
    return _ServiceNow.model_validate(data)


def test_ut01_05_entity_overrides_win() -> None:
    """UT01-05 entity overlap and page size win; unset fields inherit."""
    src = _source()
    assert src.overlap_for("incident") == datetime.timedelta(minutes=5)
    assert src.overlap_for("problem") == datetime.timedelta(minutes=45)
    assert src.page_size_for("incident") == 50
    assert src.page_size_for("problem") == 500


def test_ut01_05_backfill_merged_field_by_field() -> None:
    """UT01-05 backfill_for replaces only the fields the entity sets."""
    src = _source()
    merged = src.backfill_for("incident")
    assert merged.start == datetime.date(2024, 1, 1)
    assert merged.slice_days == 7
    assert src.backfill_for("problem") == src.backfill


def test_ut01_05_unknown_entity() -> None:
    """UT01-05 entity() raises ConfigError for an unknown name."""
    with pytest.raises(ConfigError, match="unknown entity nope for source servicenow") as info:
        _source().entity("nope")
    assert info.value.context == {"source": "servicenow", "entity": "nope"}


def test_ut01_05_resolve_start_default_and_date() -> None:
    """UT01-05 start=None gives now - 1096 d at midnight UTC; a date gives midnight."""
    assert sb.BackfillSettings().resolve_start(_now()) == datetime.datetime(2023, 9, 25, tzinfo=UTC)
    start = sb.BackfillSettings(start=datetime.date(2024, 2, 3)).resolve_start(_now())
    assert start == datetime.datetime(2024, 2, 3, tzinfo=UTC)
    other_tz = _now().astimezone(datetime.timezone(datetime.timedelta(hours=-12)))
    assert sb.BackfillSettings().resolve_start(other_tz) == datetime.datetime(
        2023, 9, 25, tzinfo=UTC
    )


def test_ut01_05_resolve_start_errors() -> None:
    """UT01-05 future start and naive now raise ConfigError."""
    with pytest.raises(ConfigError, match=r"backfill.start is in the future"):
        sb.BackfillSettings(start=datetime.date(2026, 9, 26)).resolve_start(_now())
    midnight = datetime.datetime(2026, 9, 25, tzinfo=UTC)
    with pytest.raises(ConfigError, match=r"backfill.start is in the future"):
        sb.BackfillSettings(start=datetime.date(2026, 9, 25)).resolve_start(midnight)
    with pytest.raises(ConfigError, match="naive datetime"):
        sb.BackfillSettings().resolve_start(datetime.datetime(2026, 9, 25))  # noqa: DTZ001


@pytest.mark.parametrize(
    "data",
    [
        {"start": datetime.date(1969, 12, 31)},
        {"slice_days": 0},
        {"slice_days": 367},
        {"start": "2024-01-01"},
    ],
)
def test_ut01_05_backfill_constraints(data: dict[str, Any]) -> None:
    """UT01-05 backfill start >= 1970-01-01 and 1 <= slice_days <= 366."""
    with pytest.raises(ValidationError):
        sb.BackfillSettings.model_validate(data)


def test_ut01_05_reconcile_defaults_and_constraints() -> None:
    """UT01-05 reconcile defaults; schedule shape and max_delete_pct bounds."""
    rec = sb.ReconcileSettings()
    assert (rec.schedule, rec.max_delete_pct) == ("0 3 * * SUN", 2.0)
    assert sb.ReconcileSettings.model_validate({"max_delete_pct": 100}).max_delete_pct == 100.0
    assert sb.ReconcileSettings.model_validate({"schedule": "*/5 0-6 1,15 * MON"}).schedule
    for bad in ({"max_delete_pct": 0}, {"max_delete_pct": 100.5}):
        with pytest.raises(ValidationError, match=r"reconcile\.max_delete_pct"):
            sb.ReconcileSettings.model_validate(bad)
    for wrong_type in (True, "2.0"):
        with pytest.raises(ValidationError, match="max_delete_pct"):
            sb.ReconcileSettings.model_validate({"max_delete_pct": wrong_type})
    for sched in ("0 3 * *", "0 3 * * SUN *", "0 3 * * S?N", "0 3 * *\nSUN"):
        with pytest.raises(ValidationError, match=r"reconcile\.schedule"):
            sb.ReconcileSettings.model_validate({"schedule": sched})


def test_ut01_05_entity_constraints() -> None:
    """UT01-05 entity overlap bounds; unset entity fields are None."""
    ent = sb.EntitySettings()
    assert (ent.overlap_minutes, ent.page_size, ent.backfill) == (None, None, None)
    assert sb.EntitySettings.model_validate({"overlap_minutes": 1440}).overlap_minutes == 1440
    with pytest.raises(ValidationError, match="overlap_minutes"):
        sb.EntitySettings.model_validate({"overlap_minutes": 1441})


def test_ut01_05_source_defaults() -> None:
    """UT01-05 common source defaults match the table."""
    src = _ServiceNow()
    assert src.enabled is False
    assert (src.batch_rows, src.checkpoint_rows) == (10000, 500000)
    assert (src.overlap_minutes, src.settle_seconds, src.timeout_s) == (30, 60, 60.0)
    assert (src.schedule, src.base_url, src.auth, src.verify, src.hosts) == (
        None, None, None, None, (),
    )  # fmt: skip
    assert src.reconcile == sb.ReconcileSettings()
    assert src.backfill == sb.BackfillSettings()
    assert _ServiceNow.model_validate({"base_url": None, "schedule": None}).base_url is None


@pytest.mark.parametrize(
    "data",
    [
        {"batch_rows": 999},
        {"batch_rows": 100001},
        {"batch_rows": 20000, "checkpoint_rows": 19999},
        {"checkpoint_rows": 5000001},
        {"overlap_minutes": -1},
        {"settle_seconds": 3601},
        {"timeout_s": 0.5},
        {"timeout_s": 601},
        {"schedule": "daily"},
        {"page_size": 0},
        {"enabled": "yes"},
        {"surprise": 1},
    ],
)
def test_ut01_05_source_constraints(data: dict[str, Any]) -> None:
    """UT01-05 source-level constraints reject out-of-range values."""
    with pytest.raises(ValidationError):
        _ServiceNow.model_validate(data)


def test_ut01_05_source_accepts_int_timeout_and_schedule() -> None:
    """UT01-05 a YAML int is accepted for a float field; a schedule is kept."""
    src = _ServiceNow.model_validate({"timeout_s": 600, "schedule": "15 * * * *"})
    assert src.timeout_s == 600.0
    assert src.schedule == "15 * * * *"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.service-now.com/", "https://acme.service-now.com"),
        ("https://acme.example.com/api", "https://acme.example.com/api"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://localhost/", "http://localhost"),
        ("http://[::1]:9090", "http://[::1]:9090"),
    ],
)
def test_ut01_05_base_url_accepted(url: str, expected: str) -> None:
    """UT01-05 base_url https, or http on loopback; trailing slash stripped."""
    assert _ServiceNow.model_validate({"base_url": url}).base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.example.com",
        "ftp://acme.example.com",
        "https://user:synthetic-pw@acme.example.com",
        "https://acme.example.com/?token=synthetic-q",
        "https://acme.example.com/#frag",
        "https:///path",
        "acme.example.com",
        "https://acme.example.com:99999",
        "https://acme.example.com/a b",
        "https://acme.example.com/\tx",
    ],
)
def test_ut01_05_base_url_rejected(url: str) -> None:
    """UT01-05 base_url shape violations fail without echoing the URL."""
    with pytest.raises(ValidationError, match="base_url") as info:
        _ServiceNow.model_validate({"base_url": url})
    assert "synthetic" not in str(info.value)


def test_ut01_05_hosts_rules() -> None:
    """UT01-05 hosts are lower-cased; IP, port, duplicates and > 50 entries fail."""
    src = _ServiceNow.model_validate({"hosts": ["SSO.Example.com", "api.example.com"]})
    assert src.hosts == ("sso.example.com", "api.example.com")
    for bad in (["10.0.0.1"], ["a.example.com:443"], ["a.example.com", "A.example.com"],
                ["localhost"], [f"h{i}.example.com" for i in range(51)], [7],
                "a.example.com"):  # fmt: skip
        with pytest.raises(ValidationError, match="hosts"):
            _ServiceNow.model_validate({"hosts": bad})
    assert _ServiceNow.model_validate({"hosts": ("a.example.com",)}).hosts == ("a.example.com",)


def test_ut01_05_source_is_frozen() -> None:
    """UT01-05 source models are immutable."""
    src = _ServiceNow()
    with pytest.raises(ValidationError):
        src.enabled = True  # type: ignore[misc]


def test_ut01_05_entities_mapping_is_read_only() -> None:
    """UT01-05 entities cannot be mutated in place; they still serialize as a dict."""
    src = _source()
    with pytest.raises(TypeError, match="read-only"):
        src.entities["x"] = sb.EntitySettings()  # type: ignore[index]
    with pytest.raises(TypeError, match="read-only"):
        src.entities.pop("incident")  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="read-only"):
        _ServiceNow().entities.clear()  # type: ignore[attr-defined]
    assert src.model_dump()["entities"]["problem"] == {
        "overlap_minutes": None, "page_size": None, "backfill": None,
    }  # fmt: skip
    assert '"entities":{"incident"' in src.model_dump_json()
    assert _ServiceNow.model_validate(src.model_dump()) == src


def test_ut01_04_concurrency_default_is_a_real_default() -> None:
    """UT01-04 the per-source default is a field default, not a set value."""
    assert _ServiceNow().model_dump(exclude_unset=True) == {}
    assert _ServiceNow().max_concurrency == 4
    assert _ServiceNowChild().max_concurrency == 4
    assert _Jira().max_concurrency == 2
    schema = _ServiceNow.model_json_schema()["properties"]["max_concurrency"]
    assert schema["default"] == 4
    assert sb.SourceSettings.model_fields["max_concurrency"].is_required()


def test_ut01_05_unbounded_range_message() -> None:
    """UT01-05 a lower-bound-only violation names the bound without 'inf'."""
    with pytest.raises(ValidationError, match="page_size must be >= 1") as info:
        _ServiceNow.model_validate({"page_size": 0})
    assert "<= inf" not in str(info.value)
    with pytest.raises(ValidationError, match=r"timeout_s must be >= 1 and <= 600"):
        _ServiceNow.model_validate({"timeout_s": 601})
