"""Shared setup of the redaction quality gates (impl 10 T10-15: IT10-02, BT10-03, BT10-04).

The corpus is the spec 11 recipe (`pii-corpus --seed 11 --n 5000`) generated into a temp
directory; the name directory is built from the same invented name list the corpus draws its
PERSON values from, so PERSON detection is directory-based as designed (TH11-01: no real data).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Final

from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from tools.synth.pii import build_name_list
from tools.synth.pii_corpus import write_pii_corpus

__all__ = ["CORPUS_N", "CORPUS_SEED", "build_directory", "load_corpus", "make_redactor"]
__all__ += ["dotenv_config", "write_corpus"]

CORPUS_SEED: Final = 11
CORPUS_N: Final = 5_000
_KEY: Final = bytes(range(32))


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
