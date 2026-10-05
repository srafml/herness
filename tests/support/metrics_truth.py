"""Minimal reader of a synthetic root's truth files for the impl 04 planted-truth tests (T04-22).

Card-local stand-in for spec 11 `tests/support/truth.py` (T11-16, not built yet; carry-over:
T11-16 replaces this module). It reads what `tools/synth/truth_writer.py` writes beside
`data/`: `truth/truth.json` (plants, span) and the plant member files
`truth/t2_members.parquet` (`record_id`, `plant`) and `truth/t3_pairs.parquet`
(`incident_record_id`, `change_record_id`). Only `tests/` reads truth files (R-64).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb

__all__ = ["Truth", "read_truth"]


@dataclasses.dataclass(frozen=True)
class Truth:
    """The truth manifest of one synthetic root: generated span, scale and plants."""

    root: Path
    scale: str
    start: datetime.date
    end: datetime.date
    plants: Mapping[str, Any]

    def plant(self, name: str) -> Mapping[str, Any]:
        """One plant block of `truth.json` (for example `T1_bad_team`)."""
        block = self.plants[name]
        assert isinstance(block, Mapping)
        return block

    def _parquet(self, name: str) -> Path:
        return self.root / "truth" / name

    def members(self, plant: str) -> list[str]:
        """Record IDs of the planted cluster `plant` (`T2` or `T2c`), sorted."""
        path = self._parquet("t2_members.parquet").as_posix()
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT record_id FROM read_parquet(?) WHERE plant = ? ORDER BY record_id",
                [path, plant],
            ).fetchall()
        finally:
            con.close()
        return [str(r[0]) for r in rows]

    def change_pairs(self) -> list[tuple[str, str]]:
        """Planted T3 (incident record ID, change record ID) pairs, sorted."""
        path = self._parquet("t3_pairs.parquet").as_posix()
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT incident_record_id, change_record_id FROM read_parquet(?) ORDER BY ALL",
                [path],
            ).fetchall()
        finally:
            con.close()
        return [(str(a), str(b)) for a, b in rows]


def read_truth(synth_root: Path) -> Truth:
    """The `Truth` of the synthetic root `synth_root` (the folder holding `data/`)."""
    raw = json.loads((synth_root / "truth" / "truth.json").read_text(encoding="utf-8"))
    return Truth(
        root=synth_root,
        scale=str(raw["scale"]),
        start=datetime.date.fromisoformat(raw["start"]),
        end=datetime.date.fromisoformat(raw["end"]),
        plants=raw["plants"],
    )
