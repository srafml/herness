"""Tests for tools.synth.truth_writer (U11-20): UT11-24 plus review-focus checks."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
import yaml

from herness.core.config import load_config
from herness.core.errors import ConfigError
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.enrich.text import compose_text
from herness.eval.truth import TruthManifest, load_truth
from herness.model.settings import MappingsConfig
from tools.synth.catalog import Catalog, build_catalog
from tools.synth.params import SynthParams, load_params
from tools.synth.pii import build_name_list
from tools.synth.shards import AggregateResult, plan_shards, run_shard, worker_context
from tools.synth.truth_writer import (
    SYNTH_HMAC_KEY,
    write_name_directory,
    write_synth_mappings,
    write_truth,
)

pytestmark = pytest.mark.unit

_SEED = 7
_TRUTH_FILES = ("truth.json", "truth_labels.parquet", "t2_members.parquet", "t3_pairs.parquet")
_HASH_LEN = 32


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2026, 6, 1),
        end=date(2026, 8, 31),
        sources=("servicenow", "jira", "monitoring"),
        dirty="default",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(_SEED, params)


def _manifest(cat: Catalog, params: SynthParams, root: Path, agg: AggregateResult) -> TruthManifest:
    p = cat.plants
    side = {"metric": "incident_volume", "effect": -0.4}
    t1: dict[str, object] = {"team_id": p.t1_team, "team_name": "Team"}
    t1["service_ids"] = list(p.t1_services)
    t1 |= {"metric": "mttr", "lever_usd_model": "m", "multiplier_start": 2.0}
    t2 = {"epic_key": "AAA-1", "epic_record_id": "jira:issue:1", "service_id": p.s2}
    t2 |= {"cluster_members_file": "t2_members.parquet", "decoy_epic_key": "AAA-2"}
    t2c = {"service_id": p.s2c, "cluster_members_file": "t2_members.parquet"}
    t3 = {"ci_id": p.c3, "owning_team_id": p.t1_team, "pairs_file": "t3_pairs.parquet"}
    t5 = {"team_id": p.t5_team, "service_id": p.s5, "peak_window": list(p.peak_window)}
    paid = {"epic_key": "AAA-3", "epic_record_id": "jira:issue:3", "service_id": p.s6p, **side}
    unpaid = {"epic_key": "AAA-4", "epic_record_id": "jira:issue:4", "service_id": p.s6u, **side}
    plants = {
        "T1_bad_team": {**t1, "multiplier_end": 3.0},
        "T2_roi_epic": t2,
        "T2c_cluster_fix": {**t2c, "template_id": "tpl_cert_expiry"},
        "T3_change_cluster": {**t3, "source_field_share": 0.3},
        "T4_noisy_service": {"service_id": p.s4, "generated_noise_ratio": 1.0},
        "T5_confounder": {**t5, "volume_multiplier": 2.8},
        "T6_outcomes": {"paid": paid, "unpaid": unpaid, "effective_at": p.effective_at.isoformat()},
    }
    return TruthManifest.model_validate(
        {
            "seed": _SEED,
            "scale": "tiny",
            "generator_version": "2.0.0",
            "params_hash": "sha256:x",
            "start": params.start.isoformat(),
            "end": params.end.isoformat(),
            "question_set_version": "qs1",
            "dataset_root": str(root / "data"),
            "row_counts": agg.row_counts,
            "dirty": agg.dirty.as_dict(),
            "plants": plants,
        }
    )


@pytest.fixture(scope="module")
def built(
    tmp_path_factory: pytest.TempPathFactory, cat: Catalog, params: SynthParams
) -> tuple[Path, TruthManifest]:
    """Tiny generation parts: the incident shards run in-process, then `write_truth`."""
    from tools.synth.dirty import DirtyCounters  # noqa: PLC0415 - test-local import

    root = tmp_path_factory.mktemp("synth_root")
    ctx = worker_context(root, _SEED, params, cat)
    agg = AggregateResult(ctx.parts_dir, DirtyCounters())
    for shard in plan_shards(cat, params):
        if shard.entity == "incident":
            agg.add(run_shard(shard, ctx))
    write_name_directory(root, build_name_list(_SEED))
    manifest = _manifest(cat, params, root, agg)
    write_truth(root, manifest, parts_dir=ctx.parts_dir)
    return root, manifest


def _payloads(root: Path) -> dict[str, dict[str, Any]]:
    """Latest lake payload per incident record id (read independently of the writer)."""
    out: dict[str, tuple[Any, dict[str, Any]]] = {}
    for path in (root / "data" / "raw" / "servicenow" / "incident").rglob("*.parquet"):
        for row in pq.read_table(path).to_pylist():
            if row["_payload"] is None:
                continue
            seen = out.get(row["_record_id"])
            if seen is None or row["_source_updated_at"] > seen[0]:
                out[row["_record_id"]] = (row["_source_updated_at"], json.loads(row["_payload"]))
    return {rid: payload for rid, (_, payload) in out.items()}


def _redactor(root: Path) -> Redactor:
    cfg = load_config("synth", overrides=(f"paths.data={(root / 'data').as_posix()}",))
    listing = root / "names.txt"
    listing.write_text("".join(f"{a} {b}\n" for a, b in build_name_list(_SEED)), "utf-8")
    try:
        return Redactor(
            cfg.security.redaction, SYNTH_HMAC_KEY, NameDirectory.from_files(None, (), listing)
        )
    finally:
        listing.unlink()


def test_ut11_24_truth_files_present_and_json_newest(built: tuple[Path, TruthManifest]) -> None:
    """UT11-24 all truth files present, `.parts/` removed, `truth.json` written last."""
    root, manifest = built
    truth = root / "truth"
    assert all((truth / name).is_file() for name in _TRUTH_FILES)
    assert not (truth / ".parts").exists()
    stamps = {p.name: p.stat().st_mtime_ns for p in truth.iterdir()}
    assert stamps["truth.json"] == max(stamps.values())
    assert load_truth(truth) == manifest
    assert not truth.resolve().is_relative_to((root / "data").resolve())  # TH11-02


def test_ut11_24_content_hash_is_synth_redacted_spec03_text(
    built: tuple[Path, TruthManifest],
) -> None:
    """UT11-24 `content_hash` equals SHA-256[:32] of the synth-redacted spec 03 text."""
    root, _ = built
    labels = pq.read_table(root / "truth" / "truth_labels.parquet").to_pylist()
    payloads, redactor = _payloads(root), _redactor(root)
    assert labels
    assert {r["question"] for r in labels} >= {"root_cause", "owning_team"}
    with_pii = 0
    for row in labels[::5]:
        rec = payloads[row["record_id"]]
        text = compose_text(rec["short_description"]["value"], rec["description"]["value"])
        result = redactor.redact(text)
        assert result is not None
        expected = hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:_HASH_LEN]
        assert row["content_hash"] == expected
        if json.loads(row["pii_spans"]):
            with_pii += 1
            assert result.text != text  # planted PII was redacted before hashing
    assert with_pii > 0


def test_ut11_24_pii_spans_index_the_lake_text(built: tuple[Path, TruthManifest]) -> None:
    """UT11-24 `pii_spans` JSON holds offsets and types only; they index the lake description."""
    root, _ = built
    labels = pq.read_table(root / "truth" / "truth_labels.parquet").to_pylist()
    payloads = _payloads(root)
    spans = {r["record_id"]: json.loads(r["pii_spans"]) for r in labels if r["pii_spans"] != "[]"}
    assert spans
    for rid, items in spans.items():
        text = payloads[rid]["description"]["value"]
        for span in items:
            assert set(span) == {"field", "start", "end", "type"}
            assert 0 <= span["start"] < span["end"] <= len(text)


def test_rf_truth_artifacts_hold_no_planted_pii_value(built: tuple[Path, TruthManifest]) -> None:
    """UT11-24 review focus (TH11-02): no planted PII value appears in any truth artifact."""
    root, _ = built
    labels = pq.read_table(root / "truth" / "truth_labels.parquet").to_pylist()
    payloads = _payloads(root)
    values = {
        payloads[r["record_id"]]["description"]["value"][s["start"] : s["end"]]
        for r in labels
        for s in json.loads(r["pii_spans"])
    }
    assert values
    blobs = [(root / "truth" / "truth.json").read_text("utf-8")]
    for name in ("truth_labels.parquet", "t2_members.parquet", "t3_pairs.parquet"):
        rows = pq.read_table(root / "truth" / name).to_pylist()
        blobs.append(json.dumps(rows))
    assert not [v for v in values for blob in blobs if v in blob]


def test_rf_truth_member_and_pair_files(built: tuple[Path, TruthManifest]) -> None:
    """UT11-24 review focus: T2/T2c members and T3 pairs reach their truth files."""
    root, _ = built
    members = pq.read_table(root / "truth" / "t2_members.parquet").to_pylist()
    pairs = pq.read_table(root / "truth" / "t3_pairs.parquet").to_pylist()
    assert {m["plant"] for m in members} == {"T2", "T2c"}
    assert all(m["record_id"].startswith("servicenow:incident:") for m in members)
    assert pairs
    assert all(p["change_record_id"].startswith("servicenow:change_request:") for p in pairs)


def test_rf_write_truth_without_name_directory_is_config_error(
    tmp_path: Path, built: tuple[Path, TruthManifest]
) -> None:
    """UT11-24 review focus: redactor construction failure (no name directory) -> ConfigError."""
    parts = tmp_path / "truth" / ".parts"
    parts.mkdir(parents=True)
    manifest = built[1]
    with pytest.raises(ConfigError):
        write_truth(tmp_path, manifest, parts_dir=parts)
    assert not (tmp_path / "truth" / "truth.json").exists()


def test_rf_write_truth_rejects_parts_outside_truth(
    tmp_path: Path, built: tuple[Path, TruthManifest]
) -> None:
    """UT11-24 review focus (TH11-07): a parts dir outside `<root>/truth` is refused."""
    from tools.synth.params import SynthUsageError  # noqa: PLC0415 - test-local import

    with pytest.raises(SynthUsageError):
        write_truth(tmp_path, built[1], parts_dir=tmp_path / "elsewhere")


def test_rf_name_directory_and_mappings(tmp_path: Path, cat: Catalog) -> None:
    """UT11-24 review focus: `name_directory.csv` header and rows; `synth_mappings.yaml` holds
    only `mappings.custom_fields/enums/service_overrides` and validates as `MappingsConfig`."""
    names = build_name_list(_SEED)
    path = write_name_directory(tmp_path, names)
    lines = path.read_text("utf-8").splitlines()
    assert lines[0] == "first_name,last_name"
    assert lines[1:] == [f"{a},{b}" for a, b in names]
    doc = yaml.safe_load(write_synth_mappings(tmp_path, cat).read_text("utf-8"))
    assert list(doc) == ["mappings"]
    assert set(doc["mappings"]) == {"custom_fields", "enums", "service_overrides"}
    model = MappingsConfig.model_validate(doc["mappings"], strict=False)
    assert len(model.service_overrides) == len(cat.services)
    first = model.service_overrides[0]
    assert (first.jira_project, first.jira_component) == (
        cat.services[0].jira_project,
        cat.services[0].jira_component,
    )
