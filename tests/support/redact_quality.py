"""Shared setup of the redaction quality gates (impl 10 T10-15: IT10-02, BT10-03, BT10-04).

The corpus is the spec 11 recipe (`pii-corpus --seed 11 --n 5000`) generated into a temp
directory; the name directory is built from the same invented name list the corpus draws its
PERSON values from, so PERSON detection is directory-based as designed (TH11-01: no real data).

T10-11b adds `addendum_records` (test-local rows for the T09-15d addendum: account-shaped
digit runs, 7-digit local phones, verbatim-safe shapes to keep; ruling: not in the spec 11
generator, whose output is pinned by its own tests and fixtures) and the per-type floors
that IT10-02 asserts as a regression table.
"""

from __future__ import annotations

import csv
import json
import random
import string
from pathlib import Path
from typing import Any, Final

from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from tools.synth.pii import build_name_list
from tools.synth.pii_corpus import write_pii_corpus

__all__ = ["CORPUS_N", "CORPUS_SEED", "build_directory", "load_corpus", "make_redactor"]
__all__ += ["ADDENDUM_N", "EXACT_FLOOR", "RECALL_FLOOR", "addendum_records", "dotenv_config"]
__all__ += ["write_corpus"]

CORPUS_SEED: Final = 11
CORPUS_N: Final = 5_000
ADDENDUM_N: Final = 400
_KEY: Final = bytes(range(32))
# IT10-02 regression table: strict same-type recall and exact-span recall measured at T10-11b
# (every type 1.0 on the seed-11 corpus); a later change may raise a floor, never lower it.
RECALL_FLOOR: Final = dict.fromkeys(
    ("CARD", "CREDENTIAL", "EMAIL", "EMPLOYEE_ID", "IP", "PERSON", "PHONE", "URL_TOKEN"), 1.0
)
EXACT_FLOOR: Final = dict(RECALL_FLOOR)
_CROCKFORD: Final = "".join(c for c in string.digits + string.ascii_uppercase if c not in "ILOU")


def write_corpus(directory: Path) -> Path:
    """Write the spec 11 fixture recipe corpus under `directory`; return its path."""
    path = directory / "pii_corpus.jsonl"
    write_pii_corpus(CORPUS_SEED, CORPUS_N, path)
    return path


def load_corpus(path: Path) -> list[dict[str, Any]]:
    """The corpus records, in file order."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def build_directory(directory: Path, extra: int = 0, *, initials: bool = False) -> NameDirectory:
    """Directory CSV of the corpus names, then `extra` more deterministic invented names.

    Each row is a display name (variants `First Last`, `Last, First`); with `initials` its
    `alt_names` also lists the `F. Last` form. Extras are `Zq<n>a Xv<n>b` tokens that cannot be
    real names.
    """
    rows = [(first, last) for first, last in build_name_list(CORPUS_SEED)]
    rows += [(f"Zq{i:06d}a", f"Xv{i:06d}b") for i in range(extra)]
    path = directory / "directory.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["display_name", "alt_names"])
        for first, last in rows:
            writer.writerow([f"{first} {last}", f"{first[0]}. {last}" if initials else ""])
    return NameDirectory.from_files(path, (), None)


def make_redactor(directory: NameDirectory) -> Redactor:
    """A redactor with a fixed key over `directory`."""
    return Redactor(RedactionConfig(directory_file=None), _KEY, directory)


def dotenv_config(tmp_path: Path, monkeypatch: Any) -> Path:
    """`init_config` over a full tree whose redaction key lives in `<root>/.env`.

    Spawned `redact_table` workers can resolve a dotenv key, unlike the in-memory keyring.
    """
    from tests.support.config_tree import write_full_config  # noqa: PLC0415

    from herness.core import config as c  # noqa: PLC0415

    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8").replace(
        "security:\n", "security:\n  secrets: {backend: dotenv}\n"
    )
    herness.write_text(text, "utf-8")
    (tmp_path / ".env").write_text(f"HERNESS_SECRET__REDACT_HMAC_KEY={_KEY.hex()}\n", "utf-8")
    monkeypatch.setenv("HERNESS_ENV", "dev")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "tests.support.fake_keyring.MemoryKeyring")
    c.init_config("local", config_dir=cfg_dir)
    return cfg_dir


def _keep_value(rng: random.Random) -> str:
    """One T09-15d verbatim-safe shape: build id, ULID, date, timestamp, time, query id,
    decimal or small integer."""
    day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    clock = f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
    suffix = "".join(rng.choice(_CROCKFORD) for _ in range(6))
    ulid = rng.choice("01234567") + "".join(rng.choice(_CROCKFORD) for _ in range(24)) + "Z"
    shapes = (
        f"{day.replace('-', '')}-{clock.replace(':', '')}-{suffix}",
        f"{day.replace('-', '')}-{clock.replace(':', '')}-{rng.randint(0, 999_999):06d}",
        ulid,
        day,
        f"{day}T{clock}Z",
        f"{day}T{clock}.{rng.randint(0, 999):03d}+02:00",
        clock,
        "q_" + "".join(rng.choice("0123456789abcdef") for _ in range(16)),
        f"{rng.randint(0, 99_999)}.{rng.randint(0, 99):02d}",
        str(rng.randint(0, 999)),
    )
    return rng.choice(shapes)


def addendum_records(n: int = ADDENDUM_N, seed: int = CORPUS_SEED) -> list[dict[str, Any]]:
    """`n` labelled rows: a 16-19 digit account run (CARD), a `NXX-XXXX` local phone (PHONE)
    and two verbatim-safe values (`keep`), in the corpus record format."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for index in range(n):
        account = str(rng.randint(1, 9))
        account += "".join(rng.choice(string.digits) for _ in range(rng.randint(15, 18)))
        local = f"{rng.randint(2, 9)}{rng.randint(0, 99):02d}-{rng.randint(0, 9999):04d}"
        first, second = _keep_value(rng), _keep_value(rng)
        parts = [f"Note {index}: account ", account, " is on file; call ", local, ". Build ",
                 first, " at ", second, " ok."]  # fmt: skip
        spans: list[dict[str, Any]] = []
        keep: list[dict[str, Any]] = []
        at = 0
        for pos, part in enumerate(parts):
            if pos in (1, 3):
                spans.append(
                    {"start": at, "end": at + len(part), "type": ("CARD", "PHONE")[pos == 3]}
                )
            elif pos in (5, 7):
                keep.append({"start": at, "end": at + len(part), "kind": "verbatim_safe"})
            at += len(part)
        out.append({"id": f"add-{index:06d}", "text": "".join(parts), "spans": spans, "keep": keep})
    return out
