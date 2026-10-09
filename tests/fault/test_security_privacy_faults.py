"""Fault test of privacy deletion (impl 10 T10-29): FT10-03.

``os.replace`` raising ``PermissionError`` on a lake file (a scanner or reader holding it on
Windows) fails step 2 without touching the committed file; the retry completes the step.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from tests.support.privacy_env import (  # noqa: F401 - fixture import
    BASE,
    OTHER,
    RID,
    PrivacyEnv,
    privacy_env,
)

from herness.core.errors import StoreBusy

pytestmark = pytest.mark.integration


def test_ft10_03_lake_replace_permission_error_then_retry(
    privacy_env: PrivacyEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT10-03 os.replace on a lake file raises PermissionError: the request stays running
    with step 2 failed (StoreBusy, retryable) and the lake unchanged; the retry completes."""
    env = privacy_env
    real = os.replace
    failed: list[str] = []

    def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        target = Path(dst)
        if not failed and "raw" in target.parts and target.suffix == ".parquet":
            failed.append(target.name)
            raise PermissionError(errno.EACCES, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(StoreBusy):
        env.run(dict(BASE))
    assert failed
    req = env.request()
    assert req.status == "running"
    assert req.steps["1"]["status"] == "done"
    assert (req.steps["2"]["status"], req.steps["2"]["error"]) == ("failed", "StoreBusy")
    assert "3" not in req.steps
    assert sorted(env.lake_ids()) == sorted([RID, OTHER])  # the committed file is intact
    assert not [p for p in (env.data / "raw").rglob(".*") if p.is_file()]  # no temp left

    outcome = env.run(dict(BASE))
    assert "waiting_for" in outcome.result
    req = env.request()
    assert req.steps["2"]["status"] == "done"
    assert req.steps["2"]["counts"]["rows_removed"] == 1
    assert env.lake_ids() == [OTHER]
