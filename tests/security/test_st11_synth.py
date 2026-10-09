"""ST11-01 (TH11-01) and ST11-03 (TH11-02): committed and generated synthetic data is clean.

ST11-01 scans a freshly generated tiny root (lake, inbox) and the committed fixtures with
the fixture scanner of `python -m herness.core.redact --scan`: a finding is any detected
span outside the reserved synthetic ranges (R-56, R-67). ST11-03 resolves the data paths of
the `synth` profile for a generated root and requires that none lies under `<root>/truth`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.support.truth import FIXTURES

from herness.core import redact_scan
from herness.core.config import load_config
from tools import synth_data
from tools.synth.pii_corpus import write_pii_corpus
from tools.synth.shards import CONFIG_DIR
from tools.synth.truth_writer import SYNTH_HMAC_KEY

pytestmark = pytest.mark.integration


def _scan(*paths: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    argv = [arg for path in paths for arg in ("--scan", str(path))]
    capsys.readouterr()  # drop the generator's log lines; the scanner prints findings only
    code = redact_scan.main(argv)
    return code, capsys.readouterr().out


def test_st11_01_generated_data_and_fixtures_scan_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ST11-01 scan of generated tiny output, the corpus and tests/fixtures reports no finding."""
    root = tmp_path / "root"
    synth_data.generate(7, "tiny", root, workers=2)
    corpus = tmp_path / "corpus.jsonl"
    assert write_pii_corpus(11, 2000, corpus) == 2000

    code, out = _scan(root / "data", root / "truth", corpus, capsys=capsys)
    assert (code, out) == (0, "")
    code, out = _scan(FIXTURES, capsys=capsys)
    assert (code, out) == (0, "")


def test_st11_03_truth_is_outside_every_configured_data_path(tmp_path: Path) -> None:
    """ST11-03 no data path of profile synth with paths.data=<root>/data resolves under truth."""
    root = (tmp_path / "root").resolve()
    cfg = load_config(
        "synth", overrides=(f"paths.data={(root / 'data').as_posix()}",), config_dir=CONFIG_DIR
    )
    truth = root / "truth"
    assert cfg.paths.data == root / "data"
    for path in (cfg.paths.data, cfg.paths.logs, cfg.paths.backup_target):
        resolved = Path(path).resolve()
        assert truth != resolved
        assert truth not in resolved.parents, resolved
    assert not (cfg.paths.data / "inbox").resolve().is_relative_to(truth)
    # the truth labels are hashed with the pinned synth key, 32 bytes of a fixed label
    assert hashlib.sha256(b"herness synth profile fixed test key").digest() == SYNTH_HMAC_KEY
    assert len(SYNTH_HMAC_KEY.hex()) == 64
