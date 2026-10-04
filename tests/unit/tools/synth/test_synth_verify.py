"""Tests for tools.synth.verify (U11-22): UT11-26 plus review-focus checks.

One tiny root is generated per module through `tools.synth_data.generate`, with the spawn
pool replaced by an in-process runner (unit tests start no subprocess; the pool path is
covered by IT11-01 and IT11-02). Each test corrupts its own copy of that root.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.eval.truth import TruthManifest, load_truth
from tools import synth_data
from tools.synth.catalog import Catalog
from tools.synth.dirty import DirtyCounters
from tools.synth.params import SynthParams
from tools.synth.shards import AggregateResult, Shard, run_shard, worker_context
from tools.synth.verify import VerifyReport, content_hashes, verify_root

pytestmark = pytest.mark.unit

_SEED = 7
_HASH_LEN = 32
_DIRTY_KEYS = ("duplicate_rows", "later_versions", "tombstones")


def _inline_run(
    shards: Sequence[Shard],
    cat: Catalog,
    params: SynthParams,
    *,
    seed: int,
    root: Path,
    workers: int,
) -> AggregateResult:
    """In-process stand-in for `run_all_shards`: phase A (incidents) first, then the rest."""
    ctx = worker_context(root, seed, params, cat)
    out = AggregateResult(ctx.parts_dir, DirtyCounters())
    (ctx.parts_dir / "idx").mkdir(parents=True, exist_ok=True)
    for shard in sorted(shards, key=lambda s: (s.source, s.entity) != ("servicenow", "incident")):
        out.add(run_shard(shard, ctx))
    return out


@pytest.fixture(scope="module")
def tiny_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("verify") / "root"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(synth_data, "run_all_shards", _inline_run)
        synth_data.generate(_SEED, "tiny", root, workers=1)
    return root


@pytest.fixture
def root_copy(tiny_root: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "copy"
    shutil.copytree(tiny_root, copy)
    return copy


def _raw(root: Path, key: str) -> Path:
    return root / "data" / "raw" / key


def _files(root: Path, key: str) -> list[Path]:
    return sorted(_raw(root, key).rglob("*.parquet"))


def _truth(root: Path) -> TruthManifest:
    return load_truth(root / "truth")


def test_ut11_26_clean_tiny_root_verifies_ok(root_copy: Path) -> None:
    """UT11-26 a freshly generated tiny root passes: no problem, counts equal the truth."""
    report = verify_root(root_copy)
    truth = _truth(root_copy)

    assert isinstance(report, VerifyReport)
    assert report.problems == ()
    assert report.ok is True
    assert report.row_counts == truth.row_counts
    assert report.dirty == {k: truth.dirty[k] for k in _DIRTY_KEYS}
    assert truth.row_counts["servicenow/task_sla"] > 0
    assert truth.row_counts["servicenow/cmn_department"] > 0


def test_ut11_26_dot_prefixed_file_is_a_problem(root_copy: Path) -> None:
    """UT11-26 a dot-prefixed file left under data/raw is reported (the glob skips it)."""
    first = _files(root_copy, "servicenow/incident")[0]
    shutil.copy(first, first.with_name(".part-left-over.parquet"))

    report = verify_root(root_copy)

    assert report.ok is False
    assert any("dot-prefixed" in p for p in report.problems)
    assert report.row_counts == _truth(root_copy).row_counts  # the glob never reads it


def test_ut11_26_dot_prefixed_directory_is_a_problem(root_copy: Path) -> None:
    """UT11-26 a dot-prefixed directory left under data/raw (an aborted writer's temp dir)
    is reported even when it holds no file."""
    (_raw(root_copy, "servicenow/incident") / ".tmp").mkdir()

    report = verify_root(root_copy)

    assert report.ok is False
    assert any(".tmp" in p and "dot-prefixed" in p for p in report.problems), report.problems


@pytest.mark.parametrize(
    ("key", "column"),
    [
        ("servicenow/cmn_department", "_source_key"),  # single file: the column is gone
        ("servicenow/incident", "_payload"),  # one of many files: union fills NULLs
    ],
)
def test_ut11_26_dropped_column_is_a_problem(root_copy: Path, key: str, column: str) -> None:
    """UT11-26 a metadata column dropped from a lake file is reported."""
    path = _files(root_copy, key)[0]
    table = pq.read_table(path)
    pq.write_table(table.drop_columns([column]), path)

    report = verify_root(root_copy)

    assert report.ok is False
    assert any(key in p and column in p for p in report.problems), report.problems


def test_ut11_26_extra_row_is_a_problem(root_copy: Path) -> None:
    """UT11-26 one extra lake row breaks the row count against `truth.row_counts`."""
    source = _files(root_copy, "servicenow/problem")[0]
    row = pq.read_table(source).slice(0, 1)
    pq.write_table(row, source.with_name("part-extra.parquet"))

    report = verify_root(root_copy)

    assert report.ok is False
    truth = _truth(root_copy)
    assert report.row_counts["servicenow/problem"] == truth.row_counts["servicenow/problem"] + 1
    assert any("servicenow/problem" in p and "rows" in p for p in report.problems)


def test_rf_record_id_mismatch_is_a_problem(root_copy: Path) -> None:
    """U11-22 `_record_id` must equal `_source:_entity:_source_key`; the mismatch is reported
    by key and column name, never by the offending value."""
    path = _files(root_copy, "servicenow/sys_user_group")[0]
    table = pq.read_table(path)
    ids = table.column("_record_id").to_pylist()
    ids[0] = "servicenow:sys_user_group:not-the-key"
    idx = table.schema.get_field_index("_record_id")
    pq.write_table(table.set_column(idx, table.schema.field(idx), pa.array(ids)), path)

    report = verify_root(root_copy)

    assert report.ok is False
    assert any("_record_id" in p and "sys_user_group" in p for p in report.problems)
    assert not any("not-the-key" in p for p in report.problems)


def test_rf_wrong_metadata_type_is_a_problem(root_copy: Path) -> None:
    """U11-22 the 8 metadata columns must carry the spec 02 types."""
    path = _files(root_copy, "servicenow/cmn_department")[0]
    table = pq.read_table(path)
    idx = table.schema.get_field_index("_deleted")
    as_text = pa.array([str(v) for v in table.column("_deleted").to_pylist()])
    pq.write_table(table.set_column(idx, "_deleted", as_text), path)

    report = verify_root(root_copy)

    assert report.ok is False
    assert any("_deleted" in p and "type" in p for p in report.problems)


def test_rf_missing_truth_is_a_problem(root_copy: Path) -> None:
    """U11-22 a root without a readable truth manifest is not ok."""
    (root_copy / "truth" / "truth.json").unlink()

    report = verify_root(root_copy)

    assert report.ok is False
    assert any("truth" in p for p in report.problems)


def test_ut11_26_content_hashes_per_entity_and_sensitive_to_rows(root_copy: Path) -> None:
    """UT11-26 / design §5.1.8 `content_hashes`: one 32-hex md5 per `source/entity`, stable
    over re-reads and changed by one extra row."""
    hashes = content_hashes(root_copy)

    assert set(hashes) == set(_truth(root_copy).row_counts)
    assert all(len(h) == _HASH_LEN and int(h, 16) >= 0 for h in hashes.values())
    assert content_hashes(root_copy) == hashes

    source = _files(root_copy, "servicenow/problem")[0]
    pq.write_table(pq.read_table(source).slice(0, 1), source.with_name("part-extra.parquet"))
    changed = content_hashes(root_copy)
    assert changed["servicenow/problem"] != hashes["servicenow/problem"]
    assert {k: v for k, v in changed.items() if k != "servicenow/problem"} == {
        k: v for k, v in hashes.items() if k != "servicenow/problem"
    }


def test_ut11_26_content_hashes_react_to_payload(root_copy: Path) -> None:
    """UT11-26 / design §5.1.8 rewriting one live row's `_payload` (record id and timestamp
    unchanged) changes that entity's hash and no other."""
    hashes = content_hashes(root_copy)
    path = _files(root_copy, "servicenow/problem")[0]
    table = pq.read_table(path)
    payloads = table.column("_payload").to_pylist()
    row = next(i for i, value in enumerate(payloads) if value is not None)
    payloads[row] = payloads[row][:-1] + ',"edited":true}'  # still a JSON object
    idx = table.schema.get_field_index("_payload")
    pq.write_table(table.set_column(idx, table.schema.field(idx), pa.array(payloads)), path)

    changed = content_hashes(root_copy)

    assert changed["servicenow/problem"] != hashes["servicenow/problem"]
    assert {k: v for k, v in changed.items() if k != "servicenow/problem"} == {
        k: v for k, v in hashes.items() if k != "servicenow/problem"
    }
