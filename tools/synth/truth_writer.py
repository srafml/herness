"""Truth directory, synth mappings fragment and name directory of a synthetic root (U11-20).

`write_truth` turns the shard parts under `<root>/truth/.parts/` into `truth_labels.parquet`
(`record_id`, `content_hash`, `question`, `answer`, `pii_spans` JSON), `t2_members.parquet`
and `t3_pairs.parquet`, removes `.parts/` and writes `truth.json` last; every file goes
tmp-then-`os.replace`. Truth stays under `<root>/truth`, outside `<root>/data` (TH11-02),
and every path is resolved and checked to lie under `root` (TH11-07).

`content_hash` is spec 03's (`herness.enrich.text.compose_text` and `content_hash`) over
the text redacted by a `Redactor` built from the `synth` profile's redaction config, the
profile's fixed test HMAC key `SYNTH_HMAC_KEY` and the names of `<root>/name_directory.csv`
as the person directory (each `first_name last_name` loaded the way spec 10 loads a
display name: full name, `Last, First`, `Last,First`). The PII span rows carry offsets and
types only, never the planted values.
"""

import csv
import hashlib
import io
import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from herness.core.config import load_config
from herness.core.errors import ConfigError
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.enrich.text import compose_text, content_hash
from herness.eval.truth import TruthManifest
from tools.synth import _shard_io as parts_io
from tools.synth.catalog_rows import Catalog
from tools.synth.shards import CONFIG_DIR, require_under

# The synth profile's fixed test HMAC key (spec 10 §4.3: dotenv backend, fixed test key).
# Derived from a fixed label so committed truth fixtures hash the same on every machine;
# a synth `.env` must carry the same 64 hex as HERNESS_SECRET__REDACT_HMAC_KEY.
SYNTH_HMAC_KEY: Final = hashlib.sha256(b"herness synth profile fixed test key").digest()
LABEL_ROW_GROUP: Final = 1_000_000
NAME_HEADER: Final = ("first_name", "last_name")
LABEL_SCHEMA: Final = pa.schema(
    [
        ("record_id", pa.string()),
        ("content_hash", pa.string()),
        ("question", pa.string()),
        ("answer", pa.string()),
        ("pii_spans", pa.string()),
    ]
)
_DISPLAY_NAMES: Final = "display_names.txt"


def _write_text(path: Path, text: str) -> Path:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def write_name_directory(root: Path, names: Sequence[tuple[str, str]]) -> Path:
    """`<root>/name_directory.csv` (`first_name,last_name`), the made-up PII person names."""
    path = require_under(root, root / "name_directory.csv")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(NAME_HEADER)
    writer.writerows(names)
    return _write_text(path, out.getvalue())


def _read_names(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = "cannot read name_directory.csv for the synth redactor"
        raise ConfigError(msg) from exc
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and set(NAME_HEADER) - set(rows[0]):
        msg = "name_directory.csv lacks first_name,last_name"
        raise ConfigError(msg)
    return [f"{r['first_name']} {r['last_name']}".strip() for r in rows]


def synth_redactor(root: Path, *, work_dir: Path, config_dir: Path = CONFIG_DIR) -> Redactor:
    """The truth-label redactor: `synth` profile redaction config (`paths.data = <root>/data`),
    `SYNTH_HMAC_KEY`, `<root>/name_directory.csv` as directory (`work_dir` holds a temporary
    display-names file). Any construction failure is a `ConfigError`."""
    data = (root / "data").as_posix()
    cfg = load_config("synth", overrides=(f"paths.data={data}",), config_dir=config_dir)
    names = _read_names(root / "name_directory.csv")
    listing = require_under(root, work_dir / _DISPLAY_NAMES)
    try:
        listing.parent.mkdir(parents=True, exist_ok=True)
        _write_text(listing, "".join(name + "\n" for name in names))
        directory = NameDirectory.from_files(None, (), listing)
    except OSError as exc:
        msg = "cannot build the synth redactor name directory"
        raise ConfigError(msg) from exc
    finally:
        listing.unlink(missing_ok=True)
    return Redactor(cfg.security.redaction, SYNTH_HMAC_KEY, directory)


def _hashes(redactor: Redactor, texts: pa.Table) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in texts.to_pylist():
        result = redactor.redact(compose_text(row["short_description"], row["description"]))
        out[row["record_id"]] = content_hash(result.text if result is not None else "")
    return out


def _spans(pii: pa.Table) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in pii.to_pylist():
        span = {k: row[k] for k in ("field", "start", "end", "type")}
        grouped.setdefault(row["record_id"], []).append(span)
    return {
        rid: json.dumps(
            sorted(spans, key=lambda s: (s["field"], s["start"])), separators=(",", ":")
        )
        for rid, spans in grouped.items()
    }


def _shard_labels(redactor: Redactor, parts: Path, label_part: Path) -> pa.Table:
    suffix = label_part.name.removeprefix("labels-")
    labels = pq.read_table(label_part, schema=parts_io.PART_SCHEMAS["labels"])
    texts, pii = (parts / f"{kind}-{suffix}" for kind in ("texts", "pii"))
    hashes = _hashes(redactor, pq.read_table(texts)) if texts.is_file() else {}
    spans = _spans(pq.read_table(pii)) if pii.is_file() else {}
    ids = labels.column("record_id").to_pylist()
    return pa.Table.from_arrays(
        [
            labels.column("record_id"),
            pa.array([hashes.get(rid) for rid in ids], pa.string()),
            labels.column("question"),
            labels.column("answer"),
            pa.array([spans.get(rid, "[]") for rid in ids], pa.string()),
        ],
        schema=LABEL_SCHEMA,
    )


def _write_labels(redactor: Redactor, parts: Path, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with pq.ParquetWriter(tmp, LABEL_SCHEMA, compression="zstd") as writer:
            for part in sorted(parts.glob("labels-*.parquet")):
                table = _shard_labels(redactor, parts, part)
                writer.write_table(table, row_group_size=LABEL_ROW_GROUP)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_truth(
    root: Path, manifest: TruthManifest, *, parts_dir: Path, config_dir: Path = CONFIG_DIR
) -> None:
    """Write `<root>/truth/` from the shard parts in `parts_dir` (under `<root>/truth`);
    `.parts/` is removed and `truth.json` written last."""
    base = root.resolve(strict=False)
    truth = require_under(base, base / "truth")
    parts = require_under(truth, parts_dir)
    if truth.is_relative_to(base / "data"):  # TH11-02; unreachable with the fixed layout
        msg = "truth must stay outside the data root"
        raise ConfigError(msg)
    redactor = synth_redactor(base, work_dir=parts, config_dir=config_dir)
    truth.mkdir(parents=True, exist_ok=True)
    _write_labels(redactor, parts, truth / "truth_labels.parquet")
    parts_io.write_table(truth / "t2_members.parquet", parts_io.read_parts(parts, "members"))
    parts_io.write_table(truth / "t3_pairs.parquet", parts_io.read_parts(parts, "pairs"))
    shutil.rmtree(parts)
    body = json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
    _write_text(truth / "truth.json", body)


def _service_overrides(cat: Catalog) -> list[dict[str, str]]:
    return [
        {
            "service_id": f"servicenow:cmdb_ci_service:{s.sys_id}",
            "jira_project": s.jira_project,
            "jira_component": s.jira_component,
            "role": "delivery",
        }
        for s in cat.services
    ]


def write_synth_mappings(root: Path, cat: Catalog) -> Path:
    """`<root>/synth_mappings.yaml` (consumed through `HERNESS_SYNTH_CONFIG`, spec 10 §4.3):
    only `mappings.custom_fields`, `mappings.enums` and `mappings.service_overrides`."""
    path = require_under(root, root / "synth_mappings.yaml")
    mappings = {
        "custom_fields": {
            "servicenow": {
                "customer_impact_minutes": "u_customer_impact_minutes",
                "acknowledged_at": "u_acknowledged_at",
            },
            "jira": {
                "story_points": "customfield_10016",
                "estimate_cost_usd": "customfield_10050",
                "team": "customfield_10060",
            },
        },
        "enums": {
            "servicenow.incident_state": {"2": "in_progress", "6": "resolved", "7": "closed"},
            "servicenow.change_type": {
                "Standard": "standard",
                "Normal": "normal",
                "Emergency": "emergency",
            },
        },
        "service_overrides": _service_overrides(cat),
    }
    return _write_text(path, yaml.safe_dump({"mappings": mappings}, sort_keys=False))


__all__ = [
    "LABEL_SCHEMA",
    "SYNTH_HMAC_KEY",
    "synth_redactor",
    "write_name_directory",
    "write_synth_mappings",
    "write_truth",
]
