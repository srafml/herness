"""Tests for herness.harness.memory.lora (impl 07 U07-92, T07-20): UT07-80.

Pairs are seeded straight into a migrated ops store; the embedder, warehouse and catalog are
fakes from `_lora_env`, the redactor is the real one with a planted name directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._lora_env import (
    BUILD,
    CONFIG_HASH,
    GOLDEN,
    METRICS,
    NOW,
    PARAPHRASE,
    SQL,
    LoraEnv,
    _insert,
    export_dirs,
    fingerprints,
    golden,
    make_deps,
    mid,
    read_lines,
    seed_qa,
    seed_template,
    tree,
)

from herness.core.errors import ConfigError, ModelUnavailable, StoreBusy, ToolInputError
from herness.core.ids import canonical_json
from herness.core.redact import RedactionFailed
from herness.harness.memory import lora
from herness.harness.memory.lora import (
    LINE_MAX_BYTES,
    LORA_SYSTEM_PROMPT,
    SYSTEM_PROMPT_VERSION,
    LoraDeps,
    export_lora,
)
from herness.harness.memory.procedural import wilson_lower_bound
from herness.harness.memory.settings import LoraConfig
from herness.store import warehouse
from herness.store.ops import memory as ops

pytestmark = pytest.mark.unit

_META_KEYS = {"template_id", "fingerprint", "pass_lb", "build_id", "schema_digest"}


def _seed_many(count: int = 40) -> dict[str, str]:
    """`count` active templates with one qa_pair each: {qa memory_id: fingerprint}."""
    out = {}
    for i, fp in enumerate(fingerprints(count)):
        tpl = seed_template(1000 + i, fp)
        out[seed_qa(2000 + i, tpl, f"How many incidents for team {i} this quarter?")] = fp
    return out


def _bucket(fp: str) -> str:
    return "val" if int(hashlib.sha256(fp.encode()).hexdigest()[:8], 16) % 10000 < 1000 else "train"


def _run(le: LoraEnv, gq: Any = None, **kw: Any) -> tuple[Any, Path]:
    report = export_lora(le.root, golden_questions=golden() if gq is None else gq,
                         deps=le.deps, now=NOW, **kw)  # fmt: skip
    return report, le.root / report.export_id


def test_ut07_80_export_writes_disjoint_split_and_manifest(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 export: train/val split by fingerprint hash, disjoint, lines and manifest."""
    le = make_deps(tmp_path)
    seeded = _seed_many()
    report, out = _run(le)
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "train.jsonl", "val.jsonl"]
    train, val = read_lines(out / "train.jsonl"), read_lines(out / "val.jsonl")
    assert train
    assert val
    assert (report.train_count, report.val_count) == (len(train), len(val))
    assert report.train_count + report.val_count == len(seeded) == report.templates
    fps = {name: {ln["meta"]["fingerprint"] for ln in lines}
           for name, lines in (("train", train), ("val", val))}  # fmt: skip
    assert fps["train"].isdisjoint(fps["val"])
    for name, lines in (("train", train), ("val", val)):
        assert [ln["id"] for ln in lines] == sorted(ln["id"] for ln in lines)
        assert all(_bucket(ln["meta"]["fingerprint"]) == name for ln in lines)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    line = train[0]
    assert set(line) == {"id", "messages", "meta"}
    assert set(line["meta"]) == _META_KEYS
    assert seeded[line["id"]] == line["meta"]["fingerprint"]
    assert line["meta"]["pass_lb"] == round(wilson_lower_bound(30, 0), 2)
    assert line["meta"]["build_id"] == BUILD
    assert line["meta"]["schema_digest"] == manifest["schema_digest"]
    assert [m["role"] for m in line["messages"]] == ["system", "user", "assistant"]
    system = line["messages"][0]["content"]
    assert system.startswith(LORA_SYSTEM_PROMPT)
    assert f"Schema digest: {manifest['schema_digest']}" in system
    assert f"Metrics: {', '.join(METRICS)}" in system
    assert line["messages"][2]["content"] == SQL
    assert manifest["export_id"] == report.export_id
    assert manifest["created_at"] == "2026-09-01T12:00:00.000000Z"
    assert (manifest["train_count"], manifest["val_count"]) == (len(train), len(val))
    assert manifest["excluded_golden"] == manifest["excluded_low_pass_lb"] == 0
    assert manifest["excluded_unsafe"] == manifest["excluded_oversize"] == 0
    assert manifest["filters"] == {"min_pass_lb": 0.8, "golden_exclusion_cosine": 0.9,
                                   "val_fraction": 0.1}  # fmt: skip
    assert manifest["system_prompt_version"] == SYSTEM_PROMPT_VERSION == "t2s-v1"
    assert manifest["config_hash"] == report.config_hash == CONFIG_HASH
    assert manifest["golden_exclusion"] == "cosine"
    assert manifest["line_max_bytes"] == LINE_MAX_BYTES
    for name in ("train.jsonl", "val.jsonl"):
        digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
        assert manifest["sha256"][name] == digest
    raw = (out / "manifest.json").read_bytes()
    assert report.manifest_sha256 == hashlib.sha256(raw).hexdigest()
    assert report.out_dir == out
    assert list(manifest) == sorted(manifest)
    assert not [p for p in le.root.iterdir() if p.name.startswith(".tmp-")]


def test_ut07_80_low_pass_lb_and_inactive_templates_excluded(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 exclusion: template pass_lb < min_pass_lb, template not active, qa not active."""
    le = make_deps(tmp_path)
    fp = fingerprints(5)
    keep = seed_qa(20, seed_template(10, fp[0]), "Which services breached SLA?")
    seed_qa(21, seed_template(11, fp[1], passes=3), "Low score question?")
    seed_qa(22, seed_template(12, fp[2], status="expired"), "Expired template question?")
    seed_qa(23, seed_template(13, fp[3]), "Candidate pair?", status="candidate")
    seed_qa(24, "mem_" + "9" * 26, "Orphan pair with a missing template?")
    report, out = _run(le)
    ids = [ln["id"] for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert ids == [keep]
    assert report.excluded_low_pass_lb == 3
    assert report.templates == 1
    stricter, _ = _run(le, min_pass_lb=0.95)
    assert (stricter.train_count + stricter.val_count, stricter.excluded_low_pass_lb) == (0, 4)


def test_ut07_80_golden_exclusion_embeds_goldens_once(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 exclusion: a pair at cosine >= 0.90 to a golden question is dropped; each
    golden question is redacted and embedded once."""
    le = make_deps(tmp_path)
    fp = fingerprints(3)
    seed_qa(20, seed_template(10, fp[0]), PARAPHRASE)
    seed_qa(21, seed_template(11, fp[1]), GOLDEN)
    keep = seed_qa(22, seed_template(12, fp[2]), "Which teams own the most services?")
    report, out = _run(le, gq=[GOLDEN, "Who is Jane Doakes on call for?"])
    assert report.excluded_golden == 2
    ids = [ln["id"] for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert ids == [keep]
    assert le.embedder.calls.count(GOLDEN) == 2  # once as a golden, once as a pair question
    assert not [c for c in le.embedder.calls if "Jane Doakes" in c]
    assert sum("on call for" in c for c in le.embedder.calls) == 1


def test_ut07_80_empty_golden_only_when_explicit(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 empty golden list passed explicitly: manifest `golden_exclusion: "none"`;
    omitting the keyword is a TypeError."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), PARAPHRASE)
    report, out = _run(le, gq=())
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["golden_exclusion"] == "none"
    assert report.excluded_golden == 0
    assert report.train_count + report.val_count == 1
    with pytest.raises(TypeError):
        export_lora(le.root, deps=le.deps, now=NOW)  # type: ignore[call-arg]


def test_ut07_80_output_is_deterministic(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 two exports with a fixed `now`: same JSONL bytes, manifests differ only in
    export_id."""
    le = make_deps(tmp_path)
    _seed_many(25)
    first, out1 = _run(le)
    second, out2 = _run(le)
    assert first.export_id != second.export_id
    for name in ("train.jsonl", "val.jsonl"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()
    m1, m2 = (json.loads((o / "manifest.json").read_text(encoding="utf-8")) for o in (out1, out2))
    m1.pop("export_id")
    m2.pop("export_id")
    assert m1 == m2
    assert export_dirs(le.root) == sorted([out1, out2])


def test_ut07_80_failure_mid_write_leaves_nothing(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-80 atomic: an OSError while writing becomes ConfigError naming the export path
    only, and no file or temp dir is left behind."""
    le = make_deps(tmp_path)
    _seed_many(5)

    def boom(fd: int) -> None:
        raise OSError(28, "disk full")

    monkeypatch.setattr(lora.os, "fsync", boom)
    with pytest.raises(ConfigError) as info:
        _run(le)
    assert str(info.value) == f"lora export write failed: {le.root}"
    assert isinstance(info.value.__cause__, OSError)
    assert tree(le.root) == []
    assert list(le.root.iterdir()) == []


@pytest.mark.parametrize("fault", ["embedder", "warehouse", "store"])
def test_ut07_80_errors_propagate_and_remove_tmp(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """UT07-80 atomic: ModelUnavailable (embedding) and StoreBusy re-raised, tmp removed."""
    le = make_deps(tmp_path)
    _seed_many(3)
    expected: type[Exception] = ModelUnavailable
    if fault == "embedder":
        le.embedder.fail = True
    else:
        expected = StoreBusy

        def busy(*_a: Any, **_k: Any) -> Any:
            msg = "busy"
            raise StoreBusy(msg)

        if fault == "warehouse":
            le = LoraEnv(_replace(le.deps, open_warehouse=busy), le.root, le.embedder,
                         le.warehouse)  # fmt: skip
        else:
            monkeypatch.setattr(lora.ops, "get_memory_items", busy)
    with pytest.raises(expected):
        _run(le)
    assert list(le.root.iterdir()) == []


def _replace(deps: LoraDeps, **changes: Any) -> LoraDeps:
    import dataclasses  # noqa: PLC0415 - local helper

    return dataclasses.replace(deps, **changes)


def test_ut07_80_logs_counts_only(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 `memory.lora.exported` carries the counts and ids, never question or SQL."""
    le = make_deps(tmp_path)
    _seed_many(4)
    seed_qa(90, seed_template(80, "f" * 16), PARAPHRASE)
    with capture_logs() as logs:
        report, _ = _run(le)
    events = [e for e in logs if e["event"] == "memory.lora.exported"]
    assert len(events) == 1
    ev = events[0]
    assert ev["export_id"] == report.export_id
    assert (ev["train_count"], ev["val_count"]) == (report.train_count, report.val_count)
    assert (ev["excluded_golden"], ev["excluded_unsafe"], ev["excluded_oversize"]) == (1, 0, 0)
    assert ev["excluded_low_pass_lb"] == 0
    text = repr(logs)
    assert "incidents" not in text
    assert "SELECT" not in text


def test_ut07_80_schema_digest_tracks_digest_schemas_only(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 schema_digest: 16 hex over core/enrich/metrics/score columns only."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), "Which services breached SLA?")

    def digest() -> str:
        _, out = _run(le)
        return str(json.loads((out / "manifest.json").read_text("utf-8"))["schema_digest"])

    base = digest()
    assert len(base) == 16
    assert int(base, 16) >= 0
    le.warehouse.ddl.append("CREATE TABLE main.other (y INTEGER)")
    assert digest() == base
    le.warehouse.ddl.append("ALTER TABLE core.incident ADD COLUMN team_id VARCHAR")
    assert digest() != base
    assert le.warehouse.opened == 3


def test_ut07_80_val_fraction_and_created_at_default(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 val_fraction from config; created_at from the clock when `now` is None."""
    le = make_deps(tmp_path, LoraConfig(val_fraction=0.5))
    _seed_many(20)
    report = export_lora(le.root, golden_questions=[GOLDEN], deps=le.deps)
    out = le.root / report.export_id
    val = read_lines(out / "val.jsonl")
    expected = sum(
        int(hashlib.sha256(fp.encode()).hexdigest()[:8], 16) % 10000 < 5000
        for fp in fingerprints(20)
    )
    assert len(val) == expected
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["created_at"].endswith("Z")
    assert manifest["filters"]["val_fraction"] == 0.5


def test_ut07_80_export_into_new_subdirectory(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 an out_dir below the root that does not exist yet is created inside the root."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), "Which services breached SLA?")
    report = export_lora(le.root / "nightly", golden_questions=(), deps=le.deps, now=NOW)
    assert report.out_dir == le.root / "nightly" / report.export_id
    assert (report.out_dir / "manifest.json").is_file()


def test_ut07_80_default_collaborators(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UT07-80 LoraDeps defaults: the hardened CURRENT reader and the catalog's metric names."""
    le_fields = dict.fromkeys(("conn_factory", "embedder", "redactor", "scanner"))
    deps = LoraDeps(**le_fields, config=LoraConfig(), export_root=tmp_path,  # type: ignore[arg-type]
                    config_hash=CONFIG_HASH)  # fmt: skip
    assert deps.open_warehouse is warehouse.open_readonly

    class _Catalog:
        def names(self) -> list[str]:
            return ["b", "a"]

    monkeypatch.setattr(lora, "load_catalog", _Catalog)
    assert list(deps.metric_names()) == ["b", "a"]


def test_ut07_80_lines_respect_byte_cap(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 every written line is at most LINE_MAX_BYTES UTF-8 bytes, newline excluded."""
    le = make_deps(tmp_path)
    _seed_many(10)
    _, out = _run(le)
    for name in ("train.jsonl", "val.jsonl"):
        for raw in (out / name).read_bytes().splitlines():
            assert len(raw) <= LINE_MAX_BYTES
    assert os.path.getsize(out / "manifest.json") > 0


def test_ut07_80_pages_through_many_ids(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT07-80 selection pages through memory ids in order (page size forced to 2)."""
    le = make_deps(tmp_path)
    seeded = _seed_many(7)
    monkeypatch.setattr(lora, "_ID_PAGE", 2)
    report, out = _run(le)
    ids = [ln["id"] for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert sorted(ids) == sorted(seeded)
    assert report.templates == 7


def test_ut07_80_invalid_template_reference_excluded(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 exclusion: a qa_pair whose template_id is not a memory id counts as
    excluded_low_pass_lb (no active template); pairs sharing a template stay in one split."""
    le = make_deps(tmp_path)
    seed_qa(20, "tpl-not-an-id", "Which services breached SLA?")
    tpl = seed_template(10, fingerprints(1)[0])
    seed_qa(21, tpl, "Which teams own the most services?")
    seed_qa(22, tpl, "Which services have the most incidents?")
    report, out = _run(le)
    assert (report.train_count + report.val_count, report.excluded_low_pass_lb) == (2, 1)
    assert report.templates == 1
    lines = [ln for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert {ln["meta"]["template_id"] for ln in lines} == {tpl}


def test_ut07_80_bad_embedding_and_golden_string_rejected(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 a zero embedding is ModelUnavailable; a bare string as golden_questions is a
    ToolInputError; neither leaves anything behind."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), "Which services breached SLA?")
    le.embedder.overrides[GOLDEN] = np.zeros(4)
    with pytest.raises(ModelUnavailable):
        _run(le)
    with pytest.raises(ToolInputError):
        _run(le, gq=GOLDEN)
    assert list(le.root.iterdir()) == []


def test_ut07_80_redaction_failure_drops_pair(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 a question the redactor cannot process is dropped as unsafe, never exported raw."""
    le = make_deps(tmp_path)

    class _Failing:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def redact(self, text: str | None) -> Any:
            if text is not None and "unredactable" in text:
                msg = "EMAIL"
                raise RedactionFailed(msg)
            return self.inner.redact(text)

    le = LoraEnv(_replace(le.deps, redactor=_Failing(le.deps.redactor)), le.root, le.embedder,
                 le.warehouse)  # fmt: skip
    fp = fingerprints(2)
    seed_qa(20, seed_template(10, fp[0]), "An unredactable question?")
    keep = seed_qa(21, seed_template(11, fp[1]), "Which services breached SLA?")
    report, out = _run(le, gq=())
    ids = [ln["id"] for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert ids == [keep]
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["excluded_unsafe"] == 1
    assert report.train_count + report.val_count == 1


# ---------------------------------------------------------------- fix round 1 (review m1-m3, m7)


def _independent_digest(ddl: list[str]) -> str:
    """schema_digest recomputed from the U07-92 step 5 definition, independent of lora.py."""
    con = duckdb.connect(":memory:")
    for stmt in ddl:
        con.execute(stmt)
    rows = con.execute(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
    ).fetchall()
    con.close()
    keep = sorted([list(r) for r in rows if r[0] in ("core", "enrich", "metrics", "score")])
    return hashlib.sha256(canonical_json(keep).encode("utf-8")).hexdigest()[:16]


def test_ut07_80_schema_digest_matches_spec_definition(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 schema_digest equals the independently computed spec value and covers all four
    schemas (a `score` change moves it)."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), "Which services breached SLA?")
    _, out = _run(le)
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["schema_digest"] == _independent_digest(le.warehouse.ddl)
    for stmt in (
        "ALTER TABLE score.service_score ADD COLUMN rank INTEGER",
        "ALTER TABLE enrich.incident_topic ADD COLUMN weight DOUBLE",
        "ALTER TABLE metrics.daily_incidents ADD COLUMN team_id VARCHAR",
    ):
        before = manifest["schema_digest"]
        le.warehouse.ddl.append(stmt)
        _, out = _run(le)
        manifest = json.loads((out / "manifest.json").read_text("utf-8"))
        assert manifest["schema_digest"] == _independent_digest(le.warehouse.ddl) != before


def test_ut07_80_template_must_be_sql_template_kind(
    ops_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """UT07-80 exclusion: a qa_pair pointing at an active item that is not a `sql_template`
    (even with template-like data) counts as excluded_low_pass_lb."""
    le = make_deps(tmp_path)
    data = {"fingerprint": "f" * 16, "passes": 30, "fails": 0, "build_id_last_ok": "b"}
    fake = _insert(mid(10), "analysis_recipe", "recipe", data)
    seed_qa(20, fake, "Which services breached SLA?")
    report, _ = _run(le)
    assert (report.train_count + report.val_count, report.excluded_low_pass_lb) == (0, 1)


def test_ut07_80_exported_event_is_info(ops_store: OpsStoreHandle, tmp_path: Path) -> None:
    """UT07-80 `memory.lora.exported` is logged at INFO."""
    le = make_deps(tmp_path)
    seed_qa(20, seed_template(10, fingerprints(1)[0]), "Which services breached SLA?")
    with capture_logs() as logs:
        _run(le)
    (ev,) = [e for e in logs if e["event"] == "memory.lora.exported"]
    assert ev["log_level"] == "info"


@pytest.mark.parametrize("fp", [None, "", 7])
def test_ut07_80_template_without_fingerprint_is_unsafe(
    ops_store: OpsStoreHandle, tmp_path: Path, fp: Any
) -> None:
    """UT07-80 a template with a missing, empty or non-string fingerprint excludes its pairs
    as excluded_unsafe (never the string "None" as a shared split key)."""
    le = make_deps(tmp_path)
    tpl = seed_template(10, "x" * 16)
    data = dict(ops.get_memory_items([tpl])[0]["data"])
    if fp is None:
        data.pop("fingerprint")
    else:
        data["fingerprint"] = fp
    ops.update_memory_item(tpl, data=data)
    seed_qa(20, tpl, "Which services breached SLA?")
    keep = seed_qa(21, seed_template(11, fingerprints(1)[0]), "Which teams own services?")
    _, out = _run(le)
    ids = [ln["id"] for f in ("train", "val") for ln in read_lines(out / f"{f}.jsonl")]
    assert ids == [keep]
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["excluded_unsafe"] == 1
