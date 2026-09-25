"""Tests for herness.core.errors (U00-01 … U00-08)."""

import datetime
import json
import math
import pickle  # noqa: TID251 - proves multiprocessing transport (UT00-08)
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import errors as e

pytestmark = pytest.mark.unit

PARENTS = {
    e.RetryableError: e.HernessError,
    e.RecoverableError: e.HernessError,
    e.FatalError: e.HernessError,
    e.SourceUnavailable: e.RetryableError,
    e.RateLimited: e.RetryableError,
    e.ModelUnavailable: e.RetryableError,
    e.StoreBusy: e.RetryableError,
    e.CircuitOpen: e.RetryableError,
    e.OutputValidationError: e.RecoverableError,
    e.ToolInputError: e.RecoverableError,
    e.QueryError: e.RecoverableError,
    e.ModelRefused: e.RecoverableError,
    e.PolicyViolation: e.RecoverableError,
    e.ReportContractError: e.RecoverableError,
    e.NotFound: e.RecoverableError,
    e.ConfigError: e.FatalError,
    e.AuthError: e.FatalError,
    e.SchemaViolation: e.FatalError,
    e.BudgetExceeded: e.FatalError,
    e.PermissionDenied: e.FatalError,
    e.EgressBlocked: e.FatalError,
}
RETRY_AT = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)


def _instance(cls: type[e.HernessError]) -> e.HernessError:
    if cls is e.RateLimited:
        return e.RateLimited("rl", retry_after=2.5, source="jira")
    if cls is e.CircuitOpen:
        return e.CircuitOpen("open", key="jira", retry_at=RETRY_AT, source="jira")
    if cls is e.ModelRefused:
        return e.ModelRefused("refused", category="cyber", model="m")
    return cls("boom", job_id="job_x", n=3)


def test_ut00_01_class_parents() -> None:
    """UT00-01 each class has the design 00 §7 parent; 3 categories and 18 leaves."""
    for cls, parent in PARENTS.items():
        assert cls.__bases__ == (parent,), cls.__name__
    leaves = [c for c, p in PARENTS.items() if p is not e.HernessError]
    assert len(leaves) == 18
    assert e.NotFound.__bases__ == (e.RecoverableError,)


def test_ut00_02_bounds_message_and_context() -> None:
    """UT00-02 message and string context are bounded; objects become type tags."""
    err = e.HernessError("m" * 1500, job_id="job_x", obj=object(), long="a" * 300)
    assert err.message == "m" * 1000 + "…"
    assert err.context["obj"] == "<object>"
    assert err.context["long"] == "a" * 200 + "…"
    assert str(err) == err.message
    with pytest.raises(TypeError):
        err.context["job_id"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    ("given_value", "expected"),
    [(7.0, 7.0), (-3, 0.0), (math.nan, None), (math.inf, None), (None, None)],
)
def test_ut00_03_rate_limited_retry_after(given_value: Any, expected: float | None) -> None:
    """UT00-03 retry_after is normalised to None or a finite float >= 0."""
    assert e.RateLimited("rl", retry_after=given_value).retry_after == expected


def test_ut00_04_circuit_open_retry_at_utc() -> None:
    """UT00-04 retry_at is stored as UTC; naive values are read as UTC; key kept."""
    plus2 = datetime.datetime(
        2026, 9, 24, 14, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
    )
    aware = e.CircuitOpen("x", key="k", retry_at=plus2)
    assert aware.retry_at == RETRY_AT
    assert aware.retry_at.tzinfo is datetime.UTC
    naive = e.CircuitOpen("x", key="k", retry_at=datetime.datetime(2026, 9, 24, 12, 0))  # noqa: DTZ001
    assert naive.retry_at == RETRY_AT
    assert naive.key == "k"


def test_ut00_05_model_refused_category() -> None:
    """UT00-05 category is cut to 64 characters; None stays None."""
    assert e.ModelRefused("x", category="c" * 100).category == "c" * 64
    assert e.ModelRefused("x").category is None


def test_ut00_06_error_kind() -> None:
    """UT00-06 error_kind classifies by category; bare and foreign errors are unknown."""
    for cls in PARENTS:
        kind = e.error_kind(_instance(cls))
        if issubclass(cls, e.RetryableError):
            assert kind == "retryable"
        elif issubclass(cls, e.RecoverableError):
            assert kind == "recoverable"
        else:
            assert kind == "fatal"
    assert e.error_kind(e.HernessError("x")) == "unknown"
    assert e.error_kind(ValueError()) == "unknown"


def test_ut00_07_to_log_fields_circuit_open() -> None:
    """UT00-07 to_log_fields flattens attributes and prefixes colliding context keys."""
    err = e.CircuitOpen("open", key="jira", retry_at=RETRY_AT, error_type="x", source="jira")
    out = e.to_log_fields(err)
    assert out["error_type"] == "CircuitOpen"
    assert out["error_kind"] == "retryable"
    assert out["error_message"] == "open"
    assert out["key"] == "jira"
    assert out["retry_at"] == RETRY_AT.isoformat()
    assert out["ctx_error_type"] == "x"
    assert out["source"] == "jira"
    assert out["cause_type"] is None


def test_ut00_08_pickle_round_trip() -> None:
    """UT00-08 every class survives pickle with message, context and extra attributes."""
    for cls in PARENTS:
        err = _instance(cls)
        back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
        assert type(back) is cls
        assert back.message == err.message
        assert dict(back.context) == dict(err.context)
        for name in cls._extra_attrs:
            assert getattr(back, name) == getattr(err, name)


def test_ut00_71_not_found_hint_and_details() -> None:
    """UT00-71 hint and details are bounded, survive pickle and appear in log fields."""
    details: dict[str, Any] = {"a b": "x", "run_id": "run_x", "n": 5}
    details.update({f"k{i}": "v" for i in range(60)})
    err = e.NotFound("run not found", hint="h" * 600, details=details, run_id="run_x")
    assert err.hint == "h" * 500 + "…"
    assert len(err.details) == 50
    assert err.details["key_0"] == "x"
    assert err.details["n"] == "<int>"
    assert e.error_kind(err) == "recoverable"
    fields = e.to_log_fields(err)
    assert fields["error_hint"] == err.hint
    assert fields["detail_run_id"] == "run_x"
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert back.hint == err.hint
    assert dict(back.details) == dict(err.details)
    plain = e.ConfigError("x")
    assert plain.hint is None
    assert dict(plain.details) == {}


_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=300))


@given(
    message=st.text(max_size=2000),
    context=st.dictionaries(
        st.from_regex(r"[a-z][a-z_]{0,10}", fullmatch=True).filter(
            lambda k: k not in {"hint", "details"}
        ),
        st.one_of(_scalars, st.builds(object)),
        max_size=5,
    ),
)
def test_pt00_01_log_fields_are_json_safe(message: str, context: dict[str, Any]) -> None:
    """PT00-01 to_log_fields output is JSON-serialisable and the message is bounded."""
    out = e.to_log_fields(e.SourceUnavailable(message, **context))
    json.dumps(out)
    assert len(str(out["error_message"])) <= 1001


def test_st00_13_foreign_and_cause_messages_never_logged() -> None:
    """ST00-13 third-party and cause messages never reach the log fields."""
    try:
        try:
            raise ValueError("https://x/?token=abc")  # noqa: EM101, TRY301
        except ValueError as inner:
            raise e.SourceUnavailable("fetch failed") from inner  # noqa: EM101
    except e.SourceUnavailable as err:
        out = e.to_log_fields(err)
    assert "abc" not in json.dumps(out)
    assert out["cause_type"] == "ValueError"
    foreign = e.to_log_fields(RuntimeError("password=abc"))
    assert "abc" not in json.dumps(foreign)
    assert foreign["error_message"] == ""
