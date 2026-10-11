"""Fixture builds pin the clock (impl 11 U11-48, T11-39).

UT11-119: `ensure_build` pins `herness.core.time.now` to the synth end + 1 day only during the
build, restores the real clock on every path, and the fingerprint carries the pinned date.
(IT11-33, tests/integration/support/test_builds_clock.py: two builds on two wall-clock days.)
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from tests.support import _build_cache, builds
from tests.support._build_cache import BuildFixtureError, fingerprint
from tests.support.builds import REPO, ensure_build

from herness.core import time as clock
from tools.synth._cli_subcommands import DEFAULT_END

pytestmark = pytest.mark.unit

_PIN = datetime.datetime.combine(DEFAULT_END, datetime.time(), datetime.UTC) + datetime.timedelta(
    days=1
)


def test_ut11_119_clock_is_pinned_during_the_build_and_restored_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-119 the build reads synth end + 1 day from `clock.now`, and the real clock is
    restored when the build raises."""
    real = clock.now
    during: list[datetime.datetime] = []

    def boom(_root: Path, _data_root: Path) -> str:
        during.append(clock.now())
        msg = "forced"
        raise BuildFixtureError(msg)

    monkeypatch.setattr(builds, "_run_build", boom)
    with pytest.raises(BuildFixtureError, match="forced"):
        ensure_build(7, "tiny", repo_root=REPO, cache_dir=tmp_path)
    assert during == [_PIN]
    assert clock.now is real


def test_ut11_119_fingerprint_carries_the_pinned_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-119 the fingerprint changes with the pinned date (stale caches rebuild)."""
    (tmp_path / "config" / "profiles").mkdir(parents=True)
    (tmp_path / "config" / "profiles" / "synth.yaml").write_text("v1", encoding="utf-8")
    base = fingerprint(tmp_path, "sha256:p")
    monkeypatch.setattr(_build_cache, "DEFAULT_END", DEFAULT_END + datetime.timedelta(days=1))
    assert fingerprint(tmp_path, "sha256:p") != base
