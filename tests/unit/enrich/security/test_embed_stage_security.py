"""Security tests of the embed stage (impl 03 T03-07): ST03-09 (TH03-07), ST03-15 (TH03-13).

ST03-09: `purge_record` (spec 10) is not in the tree yet; per the program ruling the attack
runs against `lance_filter_in` and the stage's own orphan delete, the `purge_record` half is
a carry-over.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs
from tests.support.build_harness import FakeJobContext
from tests.unit.enrich._embed_support import (
    MODEL_ID,
    FakeEncoder,
    Rec,
    Report,
    add_rows,
    embed_warehouse,
    use_batch_size,
    use_store,
)

from herness.core.errors import SchemaViolation
from herness.enrich.embed_stage import lance_filter_in, run_embed_stage

pytestmark = pytest.mark.unit

ATTACK = "x' OR 1=1 --"


def test_st03_09_injected_record_id_is_rejected_and_nothing_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-09 record_id `x' OR 1=1 --` -> SchemaViolation (not echoed); no row deleted."""
    store = use_store(tmp_path, monkeypatch)
    add_rows(store, [("servicenow:incident:INC1", "disk full", MODEL_ID)])
    table = store.table("ticket_embedding")
    with pytest.raises(SchemaViolation) as info:
        table.delete(lance_filter_in("record_id", [ATTACK]))
    assert "OR 1=1" not in str(info.value)
    assert "OR 1=1" not in repr((info.value.context, info.value.details, info.value.hint))
    assert store.count("ticket_embedding") == 1


def test_st03_09_injected_orphan_id_is_skipped_and_not_deleted_by_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-09 a stored orphan record_id `x' OR 1=1 --`: never in a filter; nothing else lost."""
    store = use_store(tmp_path, monkeypatch)
    add_rows(
        store,
        [(ATTACK, "planted", MODEL_ID), ("servicenow:incident:GONE", "gone ticket", MODEL_ID)],
    )
    wh = embed_warehouse(tmp_path / "wh.duckdb", [Rec("incident", "INC1", "disk full")])
    with capture_logs() as logs:
        run_embed_stage(wh, encoder=FakeEncoder(), ctx=FakeJobContext(), report=Report())  # type: ignore[arg-type]
    ids = {row["record_id"] for row in store.table("ticket_embedding").to_arrow().to_pylist()}
    assert ids == {ATTACK, "servicenow:incident:INC1"}  # only the valid orphan was deleted
    skipped = [e for e in logs if e["event"] == "enrich.embed.ids_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"
    assert (skipped[0]["records"], skipped[0]["orphans"]) == (0, 1)
    assert "OR 1=1" not in repr(logs)


def test_st03_15_encoder_inputs_equal_redacted_text_and_logs_carry_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-15 spy on encoder inputs: exactly `enrich.text_redacted.text`; logs carry counts."""
    use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)  # no config load: its own log events are not the stage's
    recs = [
        Rec("incident", "INC1", "user <PERSON_1> cannot log in"),
        Rec("incident", "INC2", "user <PERSON_1> cannot log in"),
        Rec("change", "CHG1", "rotate cert on <HOST_2>"),
        Rec("problem", "PRB1", "leak in <HOST_3> queue"),
    ]
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)
    encoder = FakeEncoder()
    with capture_logs() as logs:
        run_embed_stage(wh, encoder=encoder, ctx=FakeJobContext(), report=Report())  # type: ignore[arg-type]
    texts = wh.execute("SELECT DISTINCT text FROM enrich.text_redacted").fetchall()
    assert sorted(encoder.inputs) == sorted(text for (text,) in texts)
    assert len(encoder.inputs) == len(set(encoder.inputs))
    assert logs
    for event in logs:
        assert str(event["event"]).startswith(("enrich.embed.", "enrich.gpu."))
        for key, value in event.items():
            if key in {"event", "log_level", "component", "action"}:
                continue
            assert isinstance(value, int | float), (event["event"], key)
