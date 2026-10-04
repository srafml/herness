"""Tests for tools.synth_data (U11-23 `generate`, U11-24 `main`): UT11-27..UT11-29.

Generation runs through `generate` with the spawn pool replaced by an in-process runner
(unit tests start no subprocess; the pool path is covered by IT11-01 and IT11-02).
"""

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from herness.core.errors import ConfigError
from herness.core.logging import reset_logging
from herness.eval.truth import TruthManifest, load_truth
from tools import synth_data
from tools.synth import GENERATOR_VERSION
from tools.synth.catalog import Catalog
from tools.synth.dirty import DirtyCounters
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.pii import build_name_list
from tools.synth.shards import AggregateResult, Shard, run_shard, worker_context
from tools.synth.verify import content_hashes
from tools.synth_data import RootNotEmpty, generate, main

pytestmark = pytest.mark.unit

_SEED = 7
_SUMMARY_KEYS = {"root", "rows", "seconds", "params_hash"}


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


@pytest.fixture
def inline_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(synth_data, "run_all_shards", _inline_run)
    yield
    reset_logging()  # `main` configures process logging


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, TruthManifest]:
    root = tmp_path_factory.mktemp("generate") / "root"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(synth_data, "run_all_shards", _inline_run)
        manifest = generate(_SEED, "tiny", root, workers=1)
    return root, manifest


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class _Recorder:
    """Stand-in for the module logger: records `(level, event, fields)`."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(event: str, **fields: object) -> None:
            self.events.append((level, event, fields))

        return record


def _args(root: Path, *extra: str) -> list[str]:
    return ["--seed", str(_SEED), "--scale", "tiny", "--root", str(root), "--workers", "1", *extra]


# --- U11-23 generate --------------------------------------------------------------------


def test_ut11_27_non_empty_root_without_overwrite_is_refused(tmp_path: Path) -> None:
    """UT11-27 `generate` on a non-empty root without `overwrite` raises `RootNotEmpty`
    (a `ConfigError`) and leaves every file unchanged."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "keep.txt").write_text("operator data\n", encoding="utf-8")
    (root / "sub" / "more.bin").write_bytes(b"\x00\x01")
    before = _snapshot(root)

    with pytest.raises(RootNotEmpty) as info:
        generate(_SEED, "tiny", root)

    assert isinstance(info.value, ConfigError)
    assert _snapshot(root) == before


def test_ut11_27_overwrite_without_marker_is_refused(tmp_path: Path) -> None:
    """UT11-27 / TH11-07 `overwrite=True` on a non-empty root without `.synth_root` raises
    `SynthUsageError` and deletes nothing."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("operator data\n", encoding="utf-8")
    before = _snapshot(root)

    with pytest.raises(SynthUsageError):
        generate(_SEED, "tiny", root, overwrite=True)

    assert _snapshot(root) == before


def test_rf_generate_rejects_unknown_override(tmp_path: Path) -> None:
    """U11-23 precondition: an unknown override key raises `SynthUsageError`, nothing written."""
    root = tmp_path / "root"

    with pytest.raises(SynthUsageError):
        generate(_SEED, "tiny", root, colour="blue")

    assert not root.exists()


def test_rf_generate_writes_layout_marker_and_manifest(
    generated: tuple[Path, TruthManifest],
) -> None:
    """U11-23 postconditions: design §3.1 layout, `.synth_root` marker, a manifest whose
    counts equal the lake and whose plant ids name generated records."""
    root, manifest = generated
    marker = json.loads((root / ".synth_root").read_text(encoding="utf-8"))
    assert marker == {
        "seed": _SEED,
        "scale": "tiny",
        "generator_version": GENERATOR_VERSION,
        "params_hash": manifest.params_hash,
    }
    for rel in (
        "data/inbox/service_costs/service_costs.csv",
        "name_directory.csv",
        "synth_mappings.yaml",
        "truth/truth.json",
        "truth/truth_labels.parquet",
        "truth/t2_members.parquet",
        "truth/t3_pairs.parquet",
    ):
        assert (root / rel).is_file(), rel
    assert not (root / "truth" / ".parts").exists()
    assert load_truth(root / "truth") == manifest
    assert manifest.generator_version == "2.0.0"
    assert manifest.question_set_version == "qs-2026-10-01.1"
    assert manifest.seed == _SEED
    assert manifest.scale == "tiny"
    assert manifest.params_hash.startswith("sha256:")


def _lake_ids(root: Path, key: str) -> set[str]:
    ids: set[str] = set()
    for path in (root / "data" / "raw" / key).rglob("*.parquet"):
        ids.update(pq.read_table(path, columns=["_record_id"]).column(0).to_pylist())
    return ids


def test_rf_manifest_plants_name_generated_entities(generated: tuple[Path, TruthManifest]) -> None:
    """U11-23 step 6: the `Plants` fields are assembled from the catalog and the run, so
    each id names a record in the lake (no placeholders)."""
    root, manifest = generated
    p = manifest.plants
    teams = _lake_ids(root, "servicenow/sys_user_group")
    services = {
        i.replace("cmdb_ci_service", "cmdb_ci")
        for i in _lake_ids(root, "servicenow/cmdb_ci_service")
    }
    cis = _lake_ids(root, "servicenow/cmdb_ci")
    issues = _lake_ids(root, "jira/issue")
    assert p.T1_bad_team.team_id in teams
    assert set(p.T1_bad_team.service_ids) <= services
    assert len(p.T1_bad_team.service_ids) == 3
    assert (p.T1_bad_team.metric, p.T1_bad_team.lever_usd_model) == ("mttr_hours", "mttr")
    assert (p.T1_bad_team.multiplier_start, p.T1_bad_team.multiplier_end) == (2.0, 3.0)
    assert p.T2_roi_epic.epic_record_id in issues
    assert p.T2_roi_epic.epic_key != p.T2_roi_epic.decoy_epic_key
    assert p.T2c_cluster_fix.template_id == "tpl_cert_expiry"
    assert p.T3_change_cluster.ci_id in cis
    assert p.T3_change_cluster.owning_team_id in teams
    assert 0.0 < p.T3_change_cluster.source_field_share < 1.0
    assert 0.0 <= p.T4_noisy_service.generated_noise_ratio <= 1.0
    assert p.T5_confounder.team_id in teams
    assert p.T5_confounder.volume_multiplier == 2.8
    for side in (p.T6_outcomes.paid, p.T6_outcomes.unpaid):
        assert side.epic_record_id in issues
        assert side.metric == "incident_count"
    assert (p.T6_outcomes.paid.effect, p.T6_outcomes.unpaid.effect) == (-0.4, 0.0)
    for sid in (
        p.T2_roi_epic.service_id,
        p.T2c_cluster_fix.service_id,
        p.T4_noisy_service.service_id,
    ):
        assert sid in services


def test_ut11_28_overwrite_regenerates_identical_content(
    generated: tuple[Path, TruthManifest], inline_pool: None
) -> None:
    """UT11-28 `generate(..., overwrite=True)` on a generated root clears it (a stray file
    is gone) and regenerates identical content hashes and an identical truth manifest."""
    root, manifest = generated
    hashes = content_hashes(root)
    truth_before = (root / "truth" / "truth.json").read_bytes()
    (root / "stray.txt").write_text("left by a test\n", encoding="utf-8")

    again = generate(_SEED, "tiny", root, overwrite=True, workers=1)

    assert not (root / "stray.txt").exists()
    assert content_hashes(root) == hashes
    assert (root / "truth" / "truth.json").read_bytes() == truth_before
    assert again == manifest


# --- U11-24 main ------------------------------------------------------------------------


def test_ut11_29_valid_arguments_exit_0_with_json_summary(
    tmp_path: Path, inline_pool: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT11-29 valid arguments exit 0 and print one JSON line (`root`, `rows`, `seconds`,
    `params_hash`) on stdout; TH11-07: stdout and stderr carry no made-up person name."""
    root = tmp_path / "root"

    assert main(_args(root)) == 0

    captured = capsys.readouterr()
    names = [f"{first} {last}" for first, last in build_name_list(_SEED)]
    assert not [name for name in names if name in captured.out + captured.err]
    lines = captured.out.splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert set(summary) == _SUMMARY_KEYS
    truth = load_truth(root / "truth")
    assert summary["rows"] == sum(truth.row_counts.values())
    assert summary["params_hash"] == truth.params_hash
    assert Path(summary["root"]) == root.resolve()
    assert summary["seconds"] >= 0


def test_ut11_29_bad_scale_value_exits_3(tmp_path: Path, inline_pool: None) -> None:
    """UT11-29 `--scale huge` is a bad argument value (`SynthUsageError`): exit 3, not 2."""
    root = tmp_path / "root"
    argv = ["--seed", "7", "--scale", "huge", "--root", str(root)]

    assert main(argv) == 3
    assert not root.exists()


def test_ut11_29_non_empty_root_exits_3(tmp_path: Path, inline_pool: None) -> None:
    """UT11-29 a non-empty root without `--overwrite` (`RootNotEmpty`): exit 3."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep.txt").write_text("x\n", encoding="utf-8")

    assert main(_args(root)) == 3
    assert (root / "keep.txt").read_text(encoding="utf-8") == "x\n"


def test_ut11_29_corrupted_root_with_verify_exits_1(
    tmp_path: Path,
    inline_pool: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT11-29 a root corrupted before `--verify` (a lake file lost after the truth was
    written) fails verification (`SchemaViolation`): exit 1, `synth.verify.failed` logged,
    no JSON summary."""
    real_write_truth = synth_data.write_truth

    def write_truth_then_corrupt(root: Path, manifest: TruthManifest, **kw: Path) -> None:
        real_write_truth(root, manifest, **kw)
        next((root / "data" / "raw" / "servicenow" / "problem").rglob("*.parquet")).unlink()

    monkeypatch.setattr(synth_data, "write_truth", write_truth_then_corrupt)
    log = _Recorder()
    monkeypatch.setattr(synth_data, "_log", log)
    root = tmp_path / "root"

    assert main(_args(root, "--verify")) == 1

    assert capsys.readouterr().out == ""
    events = {(level, event): fields for level, event, fields in log.events}
    assert events[("error", "synth.verify.failed")]["n_problems"] >= 1
    assert events[("error", "synth.generate.failed")]["exit_code"] == 1
    assert "synth.generate.completed" not in {event for _, event, _ in log.events}
    assert (root / "truth" / "truth.json").is_file()  # left as is for inspection


def test_ut11_29_unexpected_error_exits_1_without_its_text(
    tmp_path: Path,
    inline_pool: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT11-29 / TH11-07 a non-Herness exception exits 1, and neither stdout nor stderr
    repeats its message (it could carry generated ticket text or names)."""
    planted = "Jane Example reported the checkout outage in ticket text"

    def explode(*_args: object, **_kwargs: object) -> TruthManifest:
        raise ValueError(planted)

    monkeypatch.setattr(synth_data, "generate", explode)

    assert main(_args(tmp_path / "root")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ValueError" in captured.err
    assert planted not in captured.out + captured.err
    assert "Jane Example" not in captured.err


def test_ut11_29_unknown_option_exits_2(
    tmp_path: Path, inline_pool: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT11-29 an unknown option (`--bogus`) is an argparse usage error: exit 2."""
    assert main([*_args(tmp_path / "root"), "--bogus"]) == 2
    assert "--bogus" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--scale", "tiny"],  # missing --seed
        ["--seed"],  # missing option value
    ],
)
def test_rf_argparse_usage_errors_exit_2(argv: list[str], inline_pool: None) -> None:
    """R-46: only what argparse itself detects exits 2."""
    assert main(argv) == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--seed", "seven"],
        ["--workers", "0"],
        ["--start", "2024-13-01"],
        ["--dirty", "filthy"],
        ["--fetch-mode", "hourly"],
        ["--sources", "servicenow,email"],
    ],
)
def test_rf_bad_argument_values_exit_3(tmp_path: Path, extra: list[str], inline_pool: None) -> None:
    """R-46: a bad argument value is a `SynthUsageError` (exit 3); the root stays untouched."""
    root = tmp_path / "root"

    assert main([*_args(root), *extra]) == 3
    assert not root.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["pii-corpus", "--seed", "11", "--n", "10", "--out", "corpus.jsonl"],
        ["api-pages", "--seed", "1", "--source", "jira", "--entity", "issue", "--rows", "5",
         "--out", "pages"],
    ],
)  # fmt: skip
def test_rf_subcommands_parse_and_exit_3_until_t11_15(
    argv: list[str], tmp_path: Path, inline_pool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U11-24 the `pii-corpus` and `api-pages` forms parse; their bodies arrive with T11-15,
    so they exit 3 (`SynthUsageError`) and write nothing."""
    monkeypatch.chdir(tmp_path)

    assert main(argv) == 3
    assert list(tmp_path.iterdir()) == []


def test_rf_default_root_normalizes_scale(
    tmp_path: Path, inline_pool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U11-24 default `--root` is `data/synth/<seed>-<scale>/` with the scale normalized
    (`5m` -> `full`); checked through the refusal of a pre-filled default root."""
    monkeypatch.chdir(tmp_path)
    default = tmp_path / "data" / "synth" / "7-full"
    default.mkdir(parents=True)
    (default / "keep.txt").write_text("x\n", encoding="utf-8")

    assert main(["--seed", "7", "--scale", "5m"]) == 3
    assert (default / "keep.txt").is_file()
