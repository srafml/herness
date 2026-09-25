"""Repository-wide test-ID collection for traceability (F11-14)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


def test_it11_31_repo_test_ids_collected(tmp_path: Path) -> None:
    """IT11-31 collect-only with required IDs writes the index; none untagged.

    Several test functions may share one test ID (program ruling on U11-31).
    """
    out = tmp_path / "test_ids.json"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--collect-test-ids={out}",
        "--require-test-ids",
    ]
    result = subprocess.run(  # noqa: S603 - fixed argv, the running interpreter
        command, cwd=ROOT, capture_output=True, text=True, timeout=240, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    index = json.loads(out.read_text(encoding="utf-8"))
    assert index["schema"] == 1
    assert index["untagged"] == []
    assert "IT11-31" in index["ids"]
    functions = {nodeid.split("[", 1)[0] for ids in index["ids"].values() for nodeid in ids}
    assert functions
