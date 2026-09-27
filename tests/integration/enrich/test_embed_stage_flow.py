"""IT03-03 embed stage twice over one warehouse (impl 03 F03-03, T03-07).

`small_build` (impl 11) is not in the tree yet: a hand-built DuckDB warehouse (minimal
`core.*` + `enrich.text_redacted`), a LanceDB store under `tmp_path` and the tiny
sentence-transformers model on the CPU stand in for it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.make_tiny_st import TINY_ST
from tests.unit.enrich._embed_support import (
    Rec,
    Report,
    add_record,
    embed_warehouse,
    stored,
    use_batch_size,
    use_store,
)

from herness.enrich import embed
from herness.enrich.embed import Encoder
from herness.enrich.embed_stage import run_embed_stage
from herness.enrich.text import content_hash

pytestmark = pytest.mark.integration


class _Spy(Encoder):
    """The real tiny-st encoder, counting the texts it encodes."""

    def __init__(self) -> None:
        super().__init__(TINY_ST, model_name="test/tiny")
        self.inputs: list[str] = []

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        self.inputs.extend(texts)
        return super().encode(texts, batch_size=batch_size)


@pytest.fixture
def encoder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Spy]:
    monkeypatch.setattr(embed, "release_cuda", lambda: None)
    spy = _Spy()
    spy.load("cpu")
    yield spy
    spy.unload()


def _records() -> list[Rec]:
    words = ("disk", "network", "login", "printer", "vpn", "backup", "queue", "email")
    recs = [
        Rec(("incident", "change", "problem")[i % 3], f"R{i:04d}", f"{words[i % 8]} failure {i}")
        for i in range(60)
    ]
    recs += [Rec("incident", "DUP1", "disk failure 0"), Rec("incident", "EMPTY", None)]
    return recs


def _run(wh: object, encoder: Encoder) -> Report:
    report = Report()
    run_embed_stage(wh, encoder=encoder, ctx=FakeJobContext(), report=report)  # type: ignore[arg-type]
    return report


def test_it03_03_embed_twice_second_run_encodes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoder: _Spy
) -> None:
    """IT03-03 embed twice: second run 0 encodes; then one edit and one removal follow."""
    store = use_store(tmp_path, monkeypatch)
    use_batch_size(monkeypatch, 8)
    recs = _records()
    wh = embed_warehouse(tmp_path / "wh.duckdb", recs)

    first = _run(wh, encoder)
    assert (first.embedded, first.cache_hits, first.rows) == (60, 0, 61)
    rows = stored(store)
    assert len(rows) == 61
    for row in rows.values():
        assert row["model"] == encoder.model_id
        assert np.isclose(np.linalg.norm(row["vector"]), 1.0, atol=1e-4)
    assert np.allclose(
        rows["servicenow:incident:DUP1"]["vector"], rows["servicenow:incident:R0000"]["vector"]
    )
    assert len(encoder.inputs) == 60

    encoder.inputs.clear()
    second = _run(wh, encoder)
    assert (second.embedded, second.cache_hits, second.rows) == (0, 0, 0)
    assert encoder.inputs == []
    assert stored(store).keys() == rows.keys()

    wh.execute(
        "UPDATE enrich.text_redacted SET text = 'vpn outage again', content_hash = ? "
        "WHERE record_id = 'servicenow:change:R0001'",
        [content_hash("vpn outage again")],
    )
    wh.execute("DELETE FROM core.problem WHERE record_id = 'servicenow:problem:R0002'")
    add_record(wh, Rec("incident", "NEW1", "disk failure 0"))  # an existing hash: reused
    third = _run(wh, encoder)
    assert encoder.inputs == ["vpn outage again"]
    assert (third.embedded, third.cache_hits, third.rows) == (1, 1, 2)
    after = stored(store)
    assert "servicenow:problem:R0002" not in after
    assert after["servicenow:change:R0001"]["content_hash"] == content_hash("vpn outage again")
    assert len(after) == 61
