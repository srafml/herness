"""Supplementary coverage for herness.core.errors.JobStateError (T08-01).

Full behavioral coverage (UT08-61, UT08-69) lands with the cards that raise this error
from real job/task operations; this file only proves the class itself (R-19).
"""

import pickle  # noqa: TID251 - proves multiprocessing transport, as UT00-08 does

import pytest

from herness.core import errors as e

pytestmark = pytest.mark.unit


def test_job_state_error_is_fatal() -> None:
    """JobStateError is a FatalError; no retry, dead-letter (R-19)."""
    assert e.JobStateError.__bases__ == (e.FatalError,)
    assert e.error_kind(e.JobStateError("bad state")) == "fatal"


def test_job_state_error_identifiers_default_to_none() -> None:
    """job_id, task_id and run_id default to None and are set as attributes."""
    err = e.JobStateError("bad state")
    assert err.job_id is None
    assert err.task_id is None
    assert err.run_id is None
    full = e.JobStateError("bad state", job_id="job_x", task_id="task_y", run_id="run_z")
    assert (full.job_id, full.task_id, full.run_id) == ("job_x", "task_y", "run_z")


def test_job_state_error_to_log_fields_and_pickle() -> None:
    """to_log_fields flattens the identifiers and the class survives pickle."""
    err = e.JobStateError("job job_x is not failed", job_id="job_x")
    fields = e.to_log_fields(err)
    assert fields["error_type"] == "JobStateError"
    assert fields["job_id"] == "job_x"
    assert fields["task_id"] is None
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert back.job_id == "job_x"
    assert back.message == err.message
