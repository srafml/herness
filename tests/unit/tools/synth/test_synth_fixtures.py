"""UT11-30 (design 11 §4.4): the committed `lake_small` is what seed 7 tiny generates today.

The lake is regenerated into a tmp dir (the spawn pool replaced by an in-process runner, as
in `test_synth_verify`) and its `content_hashes` compared with those of the committed
`tests/fixtures/lake_small/raw`. A silent generator change fails here; regenerate the
fixtures (`tools/synth_data.py --seed 7 --scale tiny`, copy `data/raw`, `data/inbox` and
`truth/`) when the change is intended.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.support.truth import LAKE_SMALL, TRUTH_DIR, load_truth_labels

from herness.eval.truth import load_truth
from tools import synth_data
from tools.synth.catalog import Catalog
from tools.synth.dirty import DirtyCounters
from tools.synth.params import SynthParams
from tools.synth.shards import AggregateResult, Shard, run_shard, worker_context
from tools.synth.verify import content_hashes

pytestmark = pytest.mark.unit


def _inline_run(
    shards: Sequence[Shard],
    cat: Catalog,
    params: SynthParams,
    *,
    seed: int,
    root: Path,
    workers: int,
) -> AggregateResult:
    """In-process stand-in for `run_all_shards`: the incidents shards first, then the rest."""
    ctx = worker_context(root, seed, params, cat)
    out = AggregateResult(ctx.parts_dir, DirtyCounters())
    (ctx.parts_dir / "idx").mkdir(parents=True, exist_ok=True)
    for shard in sorted(shards, key=lambda s: (s.source, s.entity) != ("servicenow", "incident")):
        out.add(run_shard(shard, ctx))
    return out


def test_ut11_30_regenerated_hashes_equal_committed_lake_small(tmp_path: Path) -> None:
    """UT11-30 regenerate seed 7 tiny: content_hashes and truth equal the committed copies."""
    fresh = tmp_path / "fresh"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(synth_data, "run_all_shards", _inline_run)
        synth_data.generate(7, "tiny", fresh, workers=1)
    committed = tmp_path / "committed"
    shutil.copytree(LAKE_SMALL / "raw", committed / "data" / "raw")
    expected = content_hashes(committed)
    assert expected, "the committed lake_small holds no entities"
    assert content_hashes(fresh) == expected

    truth = TRUTH_DIR / "7-tiny"
    # dataset_root is machine-specific; the committed copy carries the neutral "data".
    assert load_truth(fresh / "truth").model_copy(update={"dataset_root": "data"}) == load_truth(
        truth
    )
    assert load_truth_labels(fresh / "truth").equals(load_truth_labels(truth))
