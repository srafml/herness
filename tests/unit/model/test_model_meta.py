"""Unit tests for `herness.model.meta` (UT02-67 `git_sha`, UT02-69 `dataset_kind`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import structlog

from herness.model import meta
from herness.model.lakeinfo import LakeInventory
from herness.model.meta import dataset_kind, git_sha

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SHA40 = "0123456789abcdef" * 2 + "01234567"  # built at run time (detect-secrets)


def _inventory(*, from_synth: bool) -> LakeInventory:
    return LakeInventory(root=Path("raw"), entities={}, from_synth=from_synth)


@pytest.mark.parametrize(
    ("profile", "from_synth", "expected"),
    [
        ("synth", False, "synthetic"),
        ("synth", True, "synthetic"),
        ("local", True, "synthetic"),
        ("local", False, "real"),
        ("hybrid", False, "real"),
    ],
)
def test_ut02_69_dataset_kind(profile: str, from_synth: bool, expected: str) -> None:
    """UT02-69 `synthetic` when the profile is `synth` or the lake is synthetic, else `real`."""
    assert dataset_kind(profile, _inventory(from_synth=from_synth)) == expected


def test_ut02_67_git_sha_from_env() -> None:
    """UT02-67 a valid `HERNESS_GIT_SHA` wins: its first 12 characters, no subprocess."""
    assert git_sha(env={"HERNESS_GIT_SHA": SHA40}, repo_dir=Path("nowhere")) == SHA40[:12]
    assert git_sha(env={"HERNESS_GIT_SHA": "abcdef1"}, repo_dir=Path("nowhere")) == "abcdef1"


def test_ut02_67_git_sha_from_git() -> None:
    """UT02-67 with git present (this checkout) the HEAD SHA is returned, 12 lower-hex chars;
    an invalid env value is ignored."""
    sha = git_sha(env={"HERNESS_GIT_SHA": "not-a-sha"}, repo_dir=REPO)
    assert len(sha) == 12
    assert all(ch in "0123456789abcdef" for ch in sha)


def test_ut02_67_git_missing_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UT02-67 git missing (empty PATH): `unknown`, logged `model.build.git_sha_unknown`."""
    monkeypatch.setenv("PATH", "")
    with structlog.testing.capture_logs() as logs:
        assert git_sha(env={}, repo_dir=tmp_path) == "unknown"
    assert [e["event"] for e in logs] == ["model.build.git_sha_unknown"]
    assert logs[0]["log_level"] == "warning"


def test_ut02_67_git_failures_are_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UT02-67 a timeout, a non-zero exit or non-SHA output all give `unknown`."""

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(meta.subprocess, "run", timeout)
    assert git_sha(env={}, repo_dir=tmp_path) == "unknown"
    for code, out in ((1, SHA40), (0, "garbage\n")):
        done = subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")
        monkeypatch.setattr(meta.subprocess, "run", lambda *_a, done=done, **_k: done)
        assert git_sha(env={}, repo_dir=tmp_path) == "unknown"
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=SHA40 + "\n", stderr="")
    monkeypatch.setattr(meta.subprocess, "run", lambda *_a, **_k: ok)
    assert git_sha(env={}, repo_dir=tmp_path) == SHA40[:12]
