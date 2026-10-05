"""ST10-31: privacy delete rejects hostile record IDs before anything is enqueued (TH10-42).

Impl 10 U10-73: ``../../etc:x:y``, ``servicenow:incident:*`` and 101 IDs raise ``ConfigError``;
no job is enqueued and the message does not echo the value.
"""

from __future__ import annotations

from typing import Any

import pytest

from herness.admin import commands_data
from herness.admin.commands_data import cmd_privacy_delete
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

GOOD = "servicenow:incident:INC0001"


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def record(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        return "job_x"

    monkeypatch.setattr(commands_data, "enqueue", record)
    monkeypatch.setattr(commands_data, "worker_alive", lambda: True)
    return calls


@pytest.mark.parametrize(
    "bad", ["../../etc:x:y", "servicenow:incident:*", "a:b:c/../d", "UPPER:x:y", "a:b:" + "x" * 201]
)
def test_st10_31_hostile_record_id_rejected(bad: str, enqueued: list[Any]) -> None:
    """ST10-31 a bad ID (even after a good one) raises ConfigError, enqueues nothing, and is
    not echoed."""
    with pytest.raises(ConfigError) as caught:
        cmd_privacy_delete([GOOD, bad], reason_ref="DSR-1", actor="ops")
    assert str(caught.value) == "invalid record_id at position 2"
    assert bad not in str(caught.value)
    assert enqueued == []


def test_st10_31_over_100_ids_rejected(enqueued: list[Any]) -> None:
    """ST10-31 101 IDs raise ConfigError and nothing is enqueued; so do 0 IDs and a bare str."""
    for ids in ([GOOD] * 101, [], GOOD):
        with pytest.raises(ConfigError):
            cmd_privacy_delete(ids, reason_ref="DSR-1", actor="ops")
    assert enqueued == []
    cmd_privacy_delete([f"a:b:{n}" for n in range(100)], reason_ref="DSR-1", actor="ops")
    assert len(enqueued) == 100


def test_st10_31_bad_reason_ref_rejected(enqueued: list[Any]) -> None:
    """ST10-31 a reason_ref outside ``[A-Za-z0-9._:/-]{1,64}`` raises ConfigError, no enqueue."""
    for reason in ("", "has space", "x" * 65, "a;b"):
        with pytest.raises(ConfigError) as caught:
            cmd_privacy_delete([GOOD], reason_ref=reason, actor="ops")
        assert str(caught.value) == "invalid reason_ref"
    assert enqueued == []


@pytest.mark.parametrize("bad", ["a:b:x@y", "a:b:x+y", "a:b:" + "x" * 186])
def test_st10_31_idem_key_unsafe_id_rejected_before_enqueue(bad: str, enqueued: list[Any]) -> None:
    """ST10-31 an ID that passes the spec regex but whose ``privacy_delete:`` key JobSpec would
    refuse (``@``, ``+``, over 200 characters) raises ConfigError; nothing is enqueued."""
    with pytest.raises(ConfigError) as caught:
        cmd_privacy_delete([GOOD, bad], reason_ref="DSR-1", actor="ops")
    assert str(caught.value) == "invalid record_id at position 2"
    assert enqueued == []


def test_st10_31_non_str_inputs_raise_config_error(enqueued: list[Any]) -> None:
    """ST10-31 a non-str record ID or reason_ref raises ConfigError, never TypeError."""
    with pytest.raises(ConfigError):
        cmd_privacy_delete([GOOD, 5], reason_ref="DSR-1", actor="ops")  # type: ignore[list-item]
    with pytest.raises(ConfigError):
        cmd_privacy_delete([GOOD], reason_ref=None, actor="ops")  # type: ignore[arg-type]
    assert enqueued == []
