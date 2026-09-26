"""IT00-01: the full pre-commit hook chain; ST00-12: the detect-secrets gate (TH00-10)."""

from __future__ import annotations

import os
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
UV = shutil.which("uv")

# PYTHONUTF8=1 is CI's convention (00-foundation.impl.md §3.8, U00-59 "Environment" row) and
# it materially changes detect-secrets' results on Windows: without it, file content is
# decoded with the process locale's codepage instead of UTF-8, which can silently hide real
# findings. Force it here so this test is deterministic regardless of the caller's shell.
_SUBPROCESS_ENV = {**os.environ, "PYTHONUTF8": "1"}


# The hook chain nests the unit suite (pytest-unit) and now runs past the global 300 s
# pytest-timeout on a loaded host; align the test timeout with the subprocess timeout below.
@pytest.mark.timeout(660)
def test_it00_01_pre_commit_run_all_files() -> None:
    """IT00-01 `uv run pre-commit run --all-files` exits 0 on a clean checkout."""
    assert UV is not None
    result = subprocess.run(  # noqa: S603
        [UV, "run", "pre-commit", "run", "--all-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=600,
        env=_SUBPROCESS_ENV,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _fake_private_key_header() -> str:
    # Built from fragments at runtime so this test file's own source line never contains
    # the contiguous denylisted pattern that detect-secrets' PrivateKeyDetector matches.
    return "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def _fake_aws_key() -> str:
    # AWSKeyDetector denylist: (?:A3T[A-Z0-9]|ABIA|ACCA|AKIA|ASIA)[0-9A-Z]{16}; the suffix
    # is generated at runtime so no matching literal ever appears in this file's source.
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
    return "AKIA" + suffix


def test_st00_12_detect_secrets_hook_flags_fake_credentials(tmp_path: Path) -> None:
    """ST00-12 detect-secrets-hook exits non-zero and reports a finding for planted secrets."""
    baseline_copy = tmp_path / ".secrets.baseline"
    shutil.copy(ROOT / ".secrets.baseline", baseline_copy)

    target = tmp_path / "leaked.txt"
    target.write_text(
        f"{_fake_private_key_header()}\n"
        f"aws_access_key_id = {_fake_aws_key()}\n"
        f"{'-----END RSA PRIVATE KEY-----'}\n",
        encoding="utf-8",
    )

    assert UV is not None
    result = subprocess.run(  # noqa: S603
        [
            UV,
            "run",
            "--frozen",
            "detect-secrets-hook",
            "--baseline",
            str(baseline_copy),
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
        env=_SUBPROCESS_ENV,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Private Key" in combined or "leaked.txt" in combined
