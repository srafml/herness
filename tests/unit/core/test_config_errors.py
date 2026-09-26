"""Tests for the R-19 attributes impl 10 declares on EgressBlocked and ConfigError (U10-108)."""

from __future__ import annotations

import json
import pickle  # noqa: TID251 - proves multiprocessing transport, as UT00-71

import pytest

from herness.core import errors as e
from herness.core.config import ConfigIssue

pytestmark = pytest.mark.unit


def test_ut10_80_egress_blocked_attributes() -> None:
    """UT10-80 EgressBlocked carries egress_id and reason; str() is the message only."""
    err = e.EgressBlocked("x", egress_id="egr_01", reason="host_not_allowed", hint="h")
    assert (err.egress_id, err.reason, err.hint) == ("egr_01", "host_not_allowed", "h")
    assert str(err) == "x"
    bare = e.EgressBlocked("x")
    assert (bare.egress_id, bare.reason) == (None, None)
    assert type(err).__mro__[1] is e.FatalError
    assert e.error_kind(err) == "fatal"
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert (back.egress_id, back.reason) == ("egr_01", "host_not_allowed")
    fields = e.to_log_fields(err)
    assert (fields["egress_id"], fields["reason"]) == ("egr_01", "host_not_allowed")
    detailed = e.EgressBlocked("x", reason="size_limit", details={"limit": "2000000"})
    assert dict(detailed.details) == {"limit": "2000000"}


def test_ut10_80_malformed_reason_reads_invalid() -> None:
    """UT10-80 a reason outside [a-z_]{1,40} reads as 'invalid'; a non-str egress_id as None."""
    assert e.EgressBlocked("x", reason="Host Not Allowed").reason == "invalid"
    assert e.EgressBlocked("x", reason="a" * 41).reason == "invalid"
    assert e.EgressBlocked("x", reason=3).reason  # type: ignore[arg-type] == "invalid"
    assert e.EgressBlocked("x", egress_id=7).egress_id is None  # type: ignore[arg-type]


def test_ut10_80_malformed_values_are_masked_where_stored() -> None:
    """UT10-80 a malformed reason or non-str egress_id never reaches context, logs or pickle."""
    payload = "POST /v1/messages body=SENTINEL-9f3a"
    err = e.EgressBlocked("x", egress_id=12345, reason=payload)  # type: ignore[arg-type]
    assert (err.egress_id, err.reason) == (None, "invalid")
    assert "egress_id" not in err.context
    assert "reason" not in err.context
    fields = e.to_log_fields(err)
    assert (fields["egress_id"], fields["reason"]) == (None, "invalid")
    assert "SENTINEL" not in json.dumps(fields)
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert "SENTINEL" not in repr(vars(back))


def test_ut10_80_config_error_issues() -> None:
    """UT10-80 ConfigError keeps issues as a tuple; default (); pickles; parent unchanged."""
    issue = ConfigIssue("error", "security.egress.enabled", "file-only", "herness.yaml")
    err = e.ConfigError("y", issues=[issue], hint="herness config validate", key="k")
    assert err.issues == (issue,)
    assert str(err) == "y"
    assert (err.hint, err.context["key"]) == ("herness config validate", "k")
    assert e.ConfigError("y").issues == ()
    assert type(err).__mro__[1] is e.FatalError
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert back.issues == (issue,)
    assert len(e.ConfigError("z", issues=[issue] * 1500).issues) == 1000
    json.dumps(e.to_log_fields(err))
