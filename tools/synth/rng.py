"""Deterministic RNG streams and per-shard generators (U11-03, design §5.1.8).

Every generator derives from `numpy.random.SeedSequence(seed)` with a spawn key taken
from a SHA-256 hash of the stream name or shard key, so content never depends on the
worker count, the scheduling order or Python's salted `hash()`. `numpy` is used for
simulation only (ENG §5.7).
"""

import hashlib
from typing import Final

import numpy as np

from tools.synth.params import SynthUsageError

STREAM_CATALOG: Final = "catalog"
STREAM_PLANTS: Final = "plants"
STREAM_TEXT: Final = "text"
STREAM_PII_CORPUS: Final = "pii_corpus"
STREAM_API_PAGES: Final = "api_pages"

_KEY_SEPARATOR: Final = "\x1f"


def shard_key_hash(key: tuple[str, ...]) -> int:
    """Return the first 8 bytes (big-endian, unsigned) of SHA-256 over the joined key."""
    digest = hashlib.sha256(_KEY_SEPARATOR.join(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def shard_rng(seed: int, key: tuple[str, ...]) -> np.random.Generator:
    """Return the PCG64 generator for `key` under `seed`; equal inputs give equal state."""
    if seed < 0:
        msg = "seed must be >= 0"
        raise SynthUsageError(msg, key="seed")
    sequence = np.random.SeedSequence(seed, spawn_key=(shard_key_hash(key),))
    return np.random.Generator(np.random.PCG64(sequence))


def stream_rng(seed: int, stream: str) -> np.random.Generator:
    """Return the generator of a named seed stream (for example `STREAM_CATALOG`)."""
    return shard_rng(seed, (stream,))


__all__ = [
    "STREAM_API_PAGES",
    "STREAM_CATALOG",
    "STREAM_PII_CORPUS",
    "STREAM_PLANTS",
    "STREAM_TEXT",
    "shard_key_hash",
    "shard_rng",
    "stream_rng",
]
