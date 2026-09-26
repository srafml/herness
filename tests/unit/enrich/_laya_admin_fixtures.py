"""Shared config, gold and eval.json helpers for T03-33 laya_admin tests (impl 03 §11).

Uses the real shipped `config/decisions.yaml` (question_set_version `qs-2026-10-01.1`) through
`tests.support.config_tree.write_full_config` and `init_config`, so `laya_admin`'s own
`get_config()` and `herness.core.audit`'s internal one see the same cached config (needed for
a real, readable audit line in ST03-13).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
from tests.support.config_tree import write_full_config

from herness.core.config import HernessConfig, init_config
from herness.enrich.labels import GOLD_SCHEMA, LabelStore
from herness.enrich.layout import EnrichPaths

QSV = "qs-2026-10-01.1"
V1 = "laya-20261004-1"
V2 = "laya-20261004-2"
_USER = "ab" * 16


def build_cfg(tmp_path: Path) -> HernessConfig:
    """A real, loadable config (shipped `decisions.yaml`) rooted under `tmp_path`."""
    return init_config(config_dir=write_full_config(tmp_path), env={})


def gold_store(paths: EnrichPaths, qsv: str = QSV) -> LabelStore:
    """A `LabelStore` with a few arbitrary gold rows written (any digest suffices)."""
    store = LabelStore(paths, qsv)
    rows = pa.Table.from_pylist(
        [
            {
                "content_hash": f"{i:064x}",
                "record_id": f"INC-{i}",
                "question": "root_cause",
                "question_fingerprint": "0" * 16,
                "answer": "software_defect",
                "labeled_by": _USER,
                "labeled_at": datetime(2026, 10, 1, tzinfo=UTC),
                "item_id": f"rev_{i}",
                "fold": i % 2,
                "adjudicated": False,
            }
            for i in range(3)
        ],
        schema=GOLD_SCHEMA,
    )
    store.append("gold", rows)
    return store


def write_eval(paths: EnrichPaths, version: str, **fields: object) -> None:
    """A minimal `eval.json` under `<version>/`; `fields` overrides the defaults."""
    doc: dict[str, object] = {
        "version": version,
        "question_set_version": QSV,
        "gold_path": f"data/labels/{QSV}/gold/",
        "gold_sha256": "0" * 64,
        "evaluated_at": "2026-10-04T00:00:00.000000Z",
        "questions": {},
        "macro_metric": 0.5,
    }
    doc.update(fields)
    (paths.laya_dir(version) / "eval.json").write_text(json.dumps(doc), encoding="utf-8")
