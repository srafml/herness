"""PT11-01 (subprocess part): shard_rng draws are identical in a fresh interpreter."""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from tools.synth.rng import shard_rng

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[4]

_CHILD = """
import json, sys
from tools.synth.rng import shard_rng
cases = json.loads(sys.stdin.read())
out = [shard_rng(seed, tuple(key)).random(16).tolist() for seed, key in cases]
sys.stdout.write(json.dumps(out))
"""


def _cases() -> list[tuple[int, list[str]]]:
    gen = random.Random(1105)  # test input generation only
    alphabet = "abcXYZ019-_: \x1fé"
    cases: list[tuple[int, list[str]]] = [
        (0, ["servicenow", "incident", "2024-01-01"]),
        (2**64 - 1, [""]),
        (7, ["jira", "issue", "2026-08-01"]),
    ]
    for _ in range(60):
        seed = gen.randrange(0, 2**64)
        key = [
            "".join(gen.choice(alphabet) for _ in range(gen.randrange(0, 10)))
            for _ in range(gen.randrange(1, 5))
        ]
        cases.append((seed, key))
    return cases


def test_pt11_01_shard_rng_repeats_in_fresh_subprocess() -> None:
    """PT11-01 the first 16 draws match between this process and a fresh interpreter."""
    cases = _cases()
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", _CHILD],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_REPO,
        env={**os.environ, "PYTHONHASHSEED": "random", "PYTHONUTF8": "1"},
        check=True,
        timeout=120,
    )
    child = json.loads(proc.stdout)
    local = [shard_rng(seed, tuple(key)).random(16).tolist() for seed, key in cases]
    assert child == local
