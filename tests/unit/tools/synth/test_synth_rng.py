"""Tests for tools.synth.rng (U11-03): UT11-04 and the in-process part of PT11-01."""

import hashlib

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.synth.params import SynthUsageError
from tools.synth.rng import (
    STREAM_API_PAGES,
    STREAM_CATALOG,
    STREAM_PII_CORPUS,
    STREAM_PLANTS,
    STREAM_TEXT,
    shard_key_hash,
    shard_rng,
    stream_rng,
)

pytestmark = pytest.mark.unit

# Recorded test vector: first 8 bytes (big-endian) of SHA-256 over the "\x1f"-joined key.
_KEY = ("servicenow", "incident", "2024-01-01")
_KEY_HASH = 6187925209284608427  # 0x55dfed315c1801ab
# First three integers in [0, 2**63) of shard_rng(42, _KEY).
_SHARD_42_DRAWS = [8843852543722984549, 8592184503998904627, 5071733276488946829]


def test_ut11_04_shard_key_hash_is_stable_and_matches_vector() -> None:
    """UT11-04 the same key hashes to the same value, equal to the recorded vector."""
    first = shard_key_hash(_KEY)
    second = shard_key_hash(_KEY)
    assert first == second == _KEY_HASH
    digest = hashlib.sha256("\x1f".join(_KEY).encode("utf-8")).digest()
    assert first == int.from_bytes(digest[:8], "big")


def test_ut11_04_shard_rng_matches_recorded_draws() -> None:
    """UT11-04 shard_rng uses SeedSequence(seed, spawn_key=(hash,)) with PCG64."""
    assert shard_rng(42, _KEY).integers(0, 2**63, size=3).tolist() == _SHARD_42_DRAWS
    expected = np.random.Generator(
        np.random.PCG64(np.random.SeedSequence(42, spawn_key=(_KEY_HASH,)))
    )
    assert shard_rng(42, _KEY).random() == expected.random()


def test_ut11_04_streams_differ_and_repeat() -> None:
    """UT11-04 different streams give different first draws; the same stream repeats."""
    streams = [STREAM_CATALOG, STREAM_PLANTS, STREAM_TEXT, STREAM_PII_CORPUS, STREAM_API_PAGES]
    assert streams == ["catalog", "plants", "text", "pii_corpus", "api_pages"]
    firsts = [stream_rng(7, name).random() for name in streams]
    assert len(set(firsts)) == len(streams)
    assert stream_rng(7, STREAM_CATALOG).random() == firsts[0]
    assert stream_rng(7, STREAM_CATALOG).random() == shard_rng(7, (STREAM_CATALOG,)).random()


def test_ut11_04_negative_seed_is_rejected() -> None:
    """UT11-04 a negative seed raises SynthUsageError for both constructors."""
    with pytest.raises(SynthUsageError):
        stream_rng(-1, STREAM_CATALOG)
    with pytest.raises(SynthUsageError):
        shard_rng(-1, _KEY)


@given(
    seed=st.integers(min_value=0, max_value=2**64 - 1),
    key=st.lists(st.text(max_size=12), min_size=1, max_size=4).map(tuple),
)
def test_pt11_01_shard_rng_repeats_in_process(seed: int, key: tuple[str, ...]) -> None:
    """PT11-01 two calls with the same seed and key give the same first 16 draws."""
    first = shard_rng(seed, key).random(16).tolist()
    second = shard_rng(seed, key).random(16).tolist()
    assert first == second
